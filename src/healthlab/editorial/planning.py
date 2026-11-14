"""Select and persist review, scope and draft jobs within the run budget."""

import hashlib
import json
import logging

from healthlab.common import decode_model_json
from healthlab.data.pilot import runner_lock
from healthlab.editorial.contracts import CONTEXT_CONTRACTS, passage_inputs
from healthlab.editorial.packet import evidence_packet
from healthlab.editorial.prompts import messages
from healthlab.editorial.review_policy import ContextReview, ai_review_required, validate_review
from healthlab.editorial.runs import allocated_calls, jobs, load
from healthlab.editorial.scope import validate_scope
from healthlab.store import json_bytes

logger = logging.getLogger(__name__)


def plan_review(store, run_id, *, excluded_sample=16, all_articles=False):
    with runner_lock(store.root):
        run, records = load(store, run_id, for_inference=True)
        if not ai_review_required(run["version"]):
            raise ValueError(f"{run['version']} has no AI review step; plan scope, then draft")
        if "review" in run["prepared_review_roles"]:
            return run
        if any(j["role"] == "classify" and j["status"] == "prepared" for j in jobs(store, run)):
            raise ValueError("Finish attempting all articles before planning review")
        relevant, excluded = [], []
        for job in jobs(store, run):
            if job["role"] != "classify" or job["status"] != "completed":
                continue
            p = json.loads(store.read(job["result_sha256"]))
            (excluded if p["relevance"] == "irrelevant" else relevant).append(job)
        # Deterministic sample; negatives are checked too, never called human gold labels.
        slots = run["call_budget"] - allocated_calls(run, jobs(store, run)) - 1
        if slots < 1:
            raise ValueError("No review budget remains")
        if all_articles and slots < len(relevant) + len(excluded):
            raise ValueError("Budget cannot cover full review and one reserved draft")
        negative_count = min(excluded_sample, max(0, slots // 2), len(excluded))
        relevant.sort(
            key=lambda j: (
                not json.loads(store.read(j["result_sha256"]))["newsletter_candidate"],
                j["document_id"],
            )
        )
        selected = (
            relevant[: max(0, slots - negative_count)]
            + sorted(excluded, key=lambda j: j["document_id"])[:negative_count]
        )
        if all_articles:
            selected = sorted(relevant + excluded, key=lambda j: j["document_id"])
        run["review_scope"] = {
            "relevant_available": len(relevant),
            "relevant_selected": len(relevant)
            if all_articles
            else min(len(relevant), max(0, slots - negative_count)),
            "irrelevant_sample": len(excluded) if all_articles else negative_count,
            "selection": "all valid proposals, stable document ID; failed classifications remain unresolved"
            if all_articles
            else "dated newsletter candidates first, then stable document ID; bounded sample, not full accuracy evaluation",
            "selected_document_ids": [j["document_id"] for j in selected],
            "all_articles": all_articles,
        }
        for original in selected:
            job = store.new("editorial_call", run["question"])
            job.update(
                role="review",
                document_id=original["document_id"],
                status="prepared",
                proposal_sha256=original["result_sha256"],
            )
            store.save(job)
            run["jobs"].append(job["run_id"])
        run["prepared_review_roles"].append("review")
        store.save(run)
        return run


def plan_scope(store, run_id, *, all_articles=False):
    """Plan independent full-source scope checks within the same lifetime budget."""
    with runner_lock(store.root):
        run, records = load(store, run_id, for_inference=True)
        if "scope" in run["prepared_review_roles"]:
            return run
        calls = jobs(store, run)
        if any(j["role"] == "classify" and j["status"] == "prepared" for j in calls):
            raise ValueError("Attempt classifications before planning scope assessment")
        selected = (
            sorted(records)
            if all_articles
            else sorted(
                j["document_id"]
                for j in calls
                if j["role"] == "classify"
                and j["status"] == "completed"
                and json.loads(store.read(j["result_sha256"]))["relevance"] != "irrelevant"
            )
        )
        if allocated_calls(run, calls) + len(selected) + 1 > run["call_budget"]:
            raise ValueError("Budget cannot cover scope assessment and one reserved draft")
        for identifier in selected:
            job = store.new("editorial_call", run["question"])
            job.update(role="scope", document_id=identifier, status="prepared")
            store.save(job)
            run["jobs"].append(job["run_id"])
        run["scope_plan"] = {
            "all_articles": all_articles,
            "selected_document_ids": selected,
            "prior_proposal_supplied": False,
        }
        run["prepared_review_roles"].append("scope")
        store.save(run)
        return run


def plan_draft(store, run_id):
    with runner_lock(store.root):
        run, _ = load(store, run_id, for_inference=True)
        if run["draft_planned"]:
            return run
        packet = evidence_packet(store, run_id)
        if not packet["draft_evidence"]:
            return run  # No invented stories to fill a quota.
        if allocated_calls(run, jobs(store, run)) >= run["call_budget"]:
            raise ValueError("Lifetime call budget exhausted")
        job = store.new("editorial_call", run["question"])
        job.update(
            role="draft",
            status="prepared",
            evidence_sha256=store.put(json_bytes(packet["draft_evidence"])),
        )
        store.save(job)
        run["jobs"].append(job["run_id"])
        run["draft_planned"] = True
        store.save(run)
        return run


def adopt_reviewed(store, run_id, parent_id):
    """Adopt the parent's review and scope results where the prompt is byte-identical.

    Versions without an AI review step adopt scope results only.

    Runs after adopt_previous. Each parent call is re-validated under this run's rules and
    adopted only if this run would send exactly the same prompt; any other selected article
    gets a prepared job that needs a new call. Parent charges were already counted.
    """
    with runner_lock(store.root):
        run, records = load(store, run_id, for_inference=True)
        if run.get("parent_run_id") != parent_id or run.get("adoption_status") != "completed":
            raise ValueError("Adopt the parent's classifications first")
        if run["prepared_review_roles"]:
            raise ValueError("Review or scope is already planned for this run")
        calls = jobs(store, run)
        if any(j["role"] == "classify" and j["status"] == "prepared" for j in calls):
            raise ValueError("Finish attempting all articles before adopting review")
        parent = store.load(parent_id)
        if parent["input_sha256"] != run["input_sha256"] or parent["provider"] != run["provider"]:
            raise ValueError("Parent inputs/provider differ")
        proposals = {
            j["document_id"]: j["result_sha256"]
            for j in calls
            if j["role"] == "classify" and j["status"] == "completed"
        }
        previous = {
            (j["role"], j["document_id"]): j
            for j in jobs(store, parent)
            if j["role"] in {"review", "scope"}
        }
        selections = {
            "review": parent.get("review_scope", {}).get("selected_document_ids", []),
            "scope": parent.get("scope_plan", {}).get("selected_document_ids", []),
        }
        roles = ("review", "scope") if ai_review_required(run["version"]) else ("scope",)
        counts = {role: [0, 0] for role in roles}
        new_jobs = []
        for role in roles:
            for identifier in selections[role]:
                if role == "review" and identifier not in proposals:
                    continue  # No valid proposal here; the article stays unresolved.
                job = store.new("editorial_call", run["question"])
                job.update(role=role, document_id=identifier, status="prepared")
                if role == "review":
                    job["proposal_sha256"] = proposals[identifier]
                old = previous.get((role, identifier))
                result = _adoptable(store, run, records[identifier], job, old)
                if result is not None:
                    job.update(
                        status="completed",
                        reused_from=old["run_id"],
                        revalidation_route="identical_prompt",
                        prompt_sha256=old["prompt_sha256"],
                        result_sha256=store.put(json_bytes(result)),
                        charged=False,
                    )
                counts[role][result is None] += 1
                new_jobs.append(job)
        if allocated_calls(run, calls + new_jobs) + 1 > run["call_budget"]:
            raise ValueError("Budget cannot cover new review calls and one reserved draft")
        for job in new_jobs:
            store.save(job)
            run["jobs"].append(job["run_id"])
        if "review" in roles:
            run["review_scope"] = dict(parent.get("review_scope", {}), adopted_from=parent_id)
        run["scope_plan"] = dict(parent.get("scope_plan", {}), adopted_from=parent_id)
        run["prepared_review_roles"] = list(roles)
        store.save(run)
        return {
            "run_id": run_id,
            "parent_run_id": parent_id,
            **{f"{role}_adopted": n[0] for role, n in counts.items()},
            **{f"{role}_new_calls_needed": n[1] for role, n in counts.items()},
        }


def _adoptable(store, run, row, job, old):
    if (
        not old
        or old["status"] != "completed"
        or old.get("finish_reason") != "stop"
        or old.get("actual_model") != run["provider"]["model"]
        or not old.get("prompt_sha256")
        or not old.get("content_sha256")
    ):
        return None
    proposal = json.loads(store.read(job["proposal_sha256"])) if job["role"] == "review" else None
    prompt, _ = messages(job["role"], row, run["question"], proposal, version=run["version"])
    if hashlib.sha256(json_bytes(prompt)).hexdigest() != old["prompt_sha256"]:
        return None
    try:
        payload = decode_model_json(store.read(old["content_sha256"]).decode())
        units, _ = passage_inputs(row, version=run["version"])
        if job["role"] == "scope":
            return validate_scope(payload, units)
        if run["version"] in CONTEXT_CONTRACTS:
            ContextReview.model_validate(payload)
        return validate_review(payload, proposal, [u["passage_id"] for u in units], run["question"])
    except Exception as exc:
        # Any decode or validation failure means the old answer cannot be reused as is.
        # Log only the type: validation messages can quote model or source text.
        logger.debug("Recorded %s answer not reusable: %s", job["role"], type(exc).__name__)
        return None  # Keep the call unresolved rather than guess.
