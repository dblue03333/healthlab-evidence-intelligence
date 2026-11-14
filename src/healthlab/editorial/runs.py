"""Prepare and load pinned editorial runs, including explicit offline recovery."""

import hashlib
import json
from pathlib import Path

from healthlab.data.datasets import read_dataset
from healthlab.data.pilot import runner_lock
from healthlab.data.supervision import supervised_records
from healthlab.editorial.contracts import (
    MAX_BODY_CHARS,
    READ_VERSIONS,
    VERSION,
    canonical_proposal,
    passage_inputs,
    passage_policy,
    resolve_reference_ids,
)
from healthlab.editorial_dates import DATE_POLICY_VERSIONS
from healthlab.editorial_passages import POLICY as PASSAGE_POLICY
from healthlab.evidence import decode_model_json
from healthlab.store import json_bytes

# Bind every module in the editorial workflow and its versioned dependencies.
_RUNTIME_FILES = (
    "editorial/__init__.py",
    "editorial/__main__.py",
    "editorial/cli.py",
    "editorial/contracts.py",
    "editorial/inference.py",
    "editorial/packet.py",
    "editorial/planning.py",
    "editorial/prompts.py",
    "editorial/report.py",
    "editorial/runs.py",
    "editorial_passages.py",
    "editorial_dates.py",
    "editorial_origin.py",
    "editorial_review_policy.py",
    "editorial_scope.py",
    "provider.py",
    "config.py",
)


def fingerprint():
    root = Path(__file__).resolve().parents[1]
    hashes = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in _RUNTIME_FILES
    }
    return hashlib.sha256(json_bytes(hashes)).hexdigest()


def is_current_run(run):
    return run.get("version") == VERSION and run["runtime_sha256"] == fingerprint()


def prepare(source, store, dataset_id, provider, *, call_budget=180):
    records = supervised_records(source, dataset_id)
    _, _, _, gold = read_dataset(source, dataset_id)
    if not records or len(records) + 1 > call_budget or not 1 <= call_budget <= 500:
        raise ValueError("Budget must cover every article plus one draft (maximum 500)")
    records.sort(key=lambda r: r["document_id"])
    for row in records:
        if not row.get("body") or len(row["body"]) > MAX_BODY_CHARS:
            raise ValueError("Full article exceeds supported input; no silent truncation")
        row.setdefault("selection_date", row["publication_date"])
        row.setdefault("selection_date_basis", "publication")
    window = gold.window.model_dump(mode="json")
    run = store.new("editorial", window)
    run.update(
        version=VERSION,
        passage_policy=PASSAGE_POLICY,
        runtime_sha256=fingerprint(),
        dataset_id=dataset_id,
        source_store=str(source.root.resolve()),
        provider=provider.identity(),
        input_sha256=store.put(json_bytes(records)),
        call_budget=call_budget,
        jobs=[],
        status="prepared",
        keyword_prefilter=False,
        human_approval=False,
        prior_call_count=0,
        prepared_review_roles=[],
        draft_planned=False,
        batch_canaries=[],
    )
    for row in records:
        job = store.new("editorial_call", window)
        job.update(role="classify", document_id=row["document_id"], status="prepared")
        store.save(job)
        run["jobs"].append(job["run_id"])
    store.save(run)
    return run


def load(store, run_id, provider=None, *, for_inference=False):
    run = store.load(run_id)
    archived = run.get("version") in READ_VERSIONS
    if not is_current_run(run) and (for_inference or not archived):
        raise ValueError("Editorial runtime changed; archived run is read-only")
    if run.get("passage_policy", passage_policy(run["version"])) != passage_policy(run["version"]):
        raise ValueError("Editorial passage policy differs from its runtime")
    if provider and run["provider"] != provider.identity():
        raise ValueError("Provider identity differs from pinned run")
    records = json.loads(store.read(run["input_sha256"]))
    if len({r["document_id"] for r in records}) != len(records):
        raise ValueError("Duplicate editorial article")
    return run, {r["document_id"]: r for r in records}


def jobs(store, run):
    return [store.load(i) for i in run["jobs"]]


def allocated_calls(run, calls):
    return run.get("prior_call_count", 0) + sum(not j.get("reused_from") for j in calls)


def adopt_previous(store, run_id, parent_id, *, probes=()):
    """Explicit offline revalidation; preserve old failures and never synthesize fields.

    Only a single named wrapper around an otherwise valid complete proposal can be
    unwrapped. No coercion, filling missing fields or quote repair is performed.
    """
    with runner_lock(store.root):
        run, records = load(store, run_id, for_inference=True)
        parent = store.load(parent_id)
        if run["input_sha256"] != parent["input_sha256"] or run["provider"] != parent["provider"]:
            raise ValueError("Parent inputs/provider differ")
        resuming = run.get("parent_run_id") == parent_id and run.get("adoption_status") == "started"
        if run.get("parent_run_id") and not resuming:
            raise ValueError("Revalidation already recorded")
        previous = jobs(store, parent) + [store.load(i) for i in probes]
        prior = sum(j["status"] != "prepared" for j in previous)
        if resuming and run["prior_call_count"] != prior:
            raise ValueError("Interrupted revalidation must resume with the same prior calls")
        reused = {}
        for old in previous:
            if (
                not old.get("content_sha256")
                or old.get("finish_reason") != "stop"
                or old.get("actual_model") != run["provider"]["model"]
            ):
                continue
            try:
                raw = decode_model_json(store.read(old["content_sha256"]).decode())
                route = "original_closed_schema"
                if set(raw) in ({"json_object"}, {"json"}, {"article"}, {"schema"}):
                    key = next(iter(raw))
                    if not isinstance(raw[key], dict):
                        continue
                    raw = raw[key]
                    route = "explicit_single_wrapper_" + key
                row = records[old["document_id"]]
                # Live output cites passages by ID; resolve it exactly as inference does.
                if "relevance_passage_id" in raw:
                    if (run["version"] in DATE_POLICY_VERSIONS) != (
                        parent["version"] in DATE_POLICY_VERSIONS
                    ) and passage_inputs(row, version=run["version"]) != passage_inputs(
                        row, version=parent["version"]
                    ):
                        continue  # The historical response used a different ID catalog.
                    raw = resolve_reference_ids(raw, row, version=run["version"])
                result = canonical_proposal(raw, row, run["question"], version=run["version"])
                reused[old["document_id"]] = (old, result, route)
            except Exception:
                continue  # Retain original failure; never guess missing content.
        current = jobs(store, run)
        # Only an undispatched classification can take a reused result; a job that
        # already reached the provider keeps its own charge and outcome.
        adopt = [
            j
            for j in current
            if j["role"] == "classify"
            and j["status"] == "prepared"
            and j.get("document_id") in reused
        ]
        adopted = {j["run_id"] for j in adopt}
        planned = prior + sum(
            not j.get("reused_from") and j["run_id"] not in adopted for j in current
        )
        if planned > run["call_budget"]:
            raise ValueError("Recovery would exceed original call budget")
        # Record the historical charges before any reused result is saved, so a
        # rejected or interrupted adoption can never leave them uncounted.
        run.update(
            parent_run_id=parent_id,
            prior_call_count=prior,
            adoption_status="started",
            status="prepared",
        )
        store.save(run)
        for new in adopt:
            old, result, route = reused[new["document_id"]]
            new.update(
                status="completed",
                reused_from=old["run_id"],
                revalidation_route=route,
                result_sha256=store.put(json_bytes(result)),
                charged=False,
            )
            store.save(new)
        run["adoption_status"] = "completed"
        store.save(run)
        return {
            "run_id": run_id,
            "parent_run_id": parent_id,
            "prior_calls_charged": run["prior_call_count"],
            "reused": len(reused),
            "new_primary_calls_planned": len(records) - len(reused),
        }
