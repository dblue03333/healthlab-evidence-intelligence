"""Durable sequential workflow with explicit, version-bound recovery."""

import fcntl
import hashlib
import json
import time
from contextlib import contextmanager
from pathlib import Path

from healthlab.extraction import extract_run
from healthlab.models import IngestionError, Question
from healthlab.pipeline import ingest
from healthlab.pubmed import utc_now
from healthlab.store import json_bytes
from healthlab.synthesis import render_saved_brief, synthesize_run

STAGES = ("ingestion", "extraction", "synthesis")


def contract(provider):
    # Bind implementation as well as declared versions: forgotten version bumps
    # must not silently change the meaning of an in-progress workflow.
    modules = (
        "config",
        "models",
        "pubmed",
        "pipeline",
        "evidence",
        "extraction",
        "brief",
        "claim_context",
        "synthesis",
        "provider",
        "orchestration",
        "store",
    )
    return {
        "version": 1,
        "provider": provider.identity(),
        "implementation": {
            name: hashlib.sha256(Path(__file__).with_name(name + ".py").read_bytes()).hexdigest()
            for name in modules
        },
    }


@contextmanager
def workflow_lock(store, run_id):
    # OS releases this advisory lock even after SIGKILL; no stale lock cleanup.
    with (store.root / "runs" / run_id / "workflow.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise IngestionError("workflow_busy", "This workflow is already running") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class StageStore:
    """Link a child before it can issue any external request."""

    def __init__(self, store, workflow, attempt):
        self.store, self.workflow, self.attempt = store, workflow, attempt

    def __getattr__(self, name):
        return getattr(self.store, name)

    def new(self, mode, question):
        child = self.store.new(mode, question)
        self.attempt["run_id"] = child["run_id"]
        self.store.save(self.workflow)
        return child


def verify_objects(store, value):
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and (
                key.endswith("_sha256") or key in {"snapshot_id", "evidence_item_id"}
            ):
                raw = store.read(item)
                try:
                    nested = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                verify_objects(store, nested)
            elif isinstance(item, (dict, list)):
                verify_objects(store, item)
    elif isinstance(value, list):
        for item in value:
            verify_objects(store, item)


def metrics(child):
    entries = child.get("results", []) if child["mode"] == "extraction" else [child]
    usage = {}
    for entry in entries:
        if entry.get("cache_hit"):
            continue
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            number = entry.get("usage", {}).get(key)
            if type(number) is int and number >= 0:
                usage[key] = usage.get(key, 0) + number
    return {
        "api_calls": len(child["requests"]),
        "reported_usage": usage,
        "usage_note": "Reported tokens only; timeout/invalid-envelope usage may be unknown",
        "cache_hits": child.get("counts", {}).get("cache_hits", 0),
    }


def run_workflow(
    store, provider, *, question=None, client_factory=None, resume_id=None, on_started=None
):
    expected = contract(provider)
    if resume_id:
        run = store.load(resume_id)
        if run.get("mode") != "workflow" or run.get("contract") != expected:
            raise IngestionError(
                "resume_contract_changed",
                "Resume requires the same workflow code and model configuration; start a new run",
            )
    else:
        if not isinstance(question, Question):
            raise ValueError("A question is required for a new workflow")
        run = store.new("workflow", question.model_dump(mode="json"))
        run.update(contract=expected, stages={name: [] for name in STAGES})
        store.save(run)
    if on_started:
        on_started(run["run_id"])
    with workflow_lock(store, run["run_id"]):
        # Reload after acquiring the lock; another invocation may have just finished.
        run = store.load(run["run_id"])
        if run["contract"] != expected:
            raise IngestionError("resume_contract_changed", "Workflow contract changed")
        for attempts in run["stages"].values():
            for attempt in attempts:
                if attempt.get("manifest_sha256"):
                    child = store.load(attempt["run_id"])
                    if json_bytes(child) != store.read(attempt["manifest_sha256"]):
                        raise IngestionError("lineage_changed", "A saved child manifest changed")
                    if child["question"] != run["question"]:
                        raise IngestionError(
                            "lineage_changed", "Workflow question differs from saved child"
                        )
                    verify_objects(store, child)
        run.update(status="running", errors=[], warnings=[])
        store.save(run)
        upstream = None
        changed = False
        for stage in STAGES:
            attempts = run["stages"][stage]
            previous = attempts[-1] if attempts else None
            reusable = previous and previous.get("status") == "completed"
            if stage == "extraction" and previous and previous.get("status") == "partial":
                prior_child = store.load(previous["run_id"])
                reusable = not prior_child.get("counts", {}).get("failed", 0)
            if stage == "synthesis" and previous and previous.get("status") == "partial":
                reusable = True
            if stage != "ingestion" and previous and upstream:
                reusable = reusable and previous.get("input_run_id") == upstream["run_id"]
            if stage == "ingestion" and previous and previous.get("status") == "partial":
                reusable = True  # Preserve the original search/snapshot boundary.
            if reusable and not changed:
                upstream = store.load(previous["run_id"])
                continue
            changed = True
            if previous and previous["status"] == "running":
                previous["status"] = "interrupted"
                if previous.get("run_id"):
                    interrupted = store.load(previous["run_id"])
                    previous.update(metrics(interrupted))
            attempt = {
                "status": "running",
                "started_at": utc_now(),
                "input_run_id": upstream["run_id"] if upstream else None,
            }
            attempts.append(attempt)
            run["stage"] = stage
            store.save(run)
            tracked = StageStore(store, run, attempt)
            started = time.monotonic()
            try:
                if stage == "ingestion":
                    client = client_factory()
                    try:
                        child = ingest(tracked, Question.model_validate(run["question"]), client)
                    finally:
                        client.close()
                elif stage == "extraction":
                    child = extract_run(tracked, upstream["run_id"], provider)
                else:
                    child = synthesize_run(tracked, upstream["run_id"], provider)
                attempt.update(
                    status=child["status"],
                    **metrics(child),
                    manifest_sha256=store.put(json_bytes(child)),
                )
                upstream = child
            except (IngestionError, ValueError, OSError) as exc:
                if attempt.get("run_id"):
                    try:
                        attempt.update(metrics(store.load(attempt["run_id"])))
                    except (IngestionError, OSError):
                        pass  # A storage failure may also prevent reading the child.
                attempt.update(
                    status="failed",
                    error={
                        "code": getattr(exc, "code", "stage_error"),
                        "message": str(exc)
                        if isinstance(exc, IngestionError)
                        else "Stage could not complete; inspect child manifest and configuration",
                    },
                )
                upstream = None
            finally:
                attempt.update(
                    elapsed_seconds=round(time.monotonic() - started, 6), finished_at=utc_now()
                )
                store.save(run)
            if attempt["status"] == "failed" and stage != "extraction":
                break
            if upstream is None:
                break
        latest = {name: attempts[-1] for name, attempts in run["stages"].items() if attempts}
        run["brief"] = None
        if upstream and upstream["mode"] == "synthesis" and upstream.get("markdown_sha256"):
            run["brief"] = str(render_saved_brief(store, upstream["run_id"]))
        run["status"] = (
            "completed"
            if len(latest) == 3 and all(a["status"] == "completed" for a in latest.values())
            else "partial"
            if run["brief"] or any(a["status"] in {"completed", "partial"} for a in latest.values())
            else "failed"
        )
        run["counts"] = {}
        for name, attempt in latest.items():
            child = store.load(attempt["run_id"]) if attempt.get("run_id") else None
            run["counts"][name] = child.get("counts") if child else None
            if attempt["status"] != "completed":
                run["warnings"].append(f"{name}: {attempt['status']}")
            errors = [attempt["error"]] if attempt.get("error") else (child or {}).get("errors", [])
            for error in errors:
                run["errors"].append({**error, "stage": name, "run_id": attempt.get("run_id")})
            if attempt["status"] == "failed" and not errors:
                run["errors"].append(
                    {"stage": name, "run_id": attempt.get("run_id"), "code": "stage_failed"}
                )
        run.update(stage="finished", finished_at=utc_now())
        run["metrics"] = {
            "api_calls": sum(
                a.get("api_calls", 0) for attempts in run["stages"].values() for a in attempts
            ),
            "elapsed_seconds": sum(
                a.get("elapsed_seconds", 0) for attempts in run["stages"].values() for a in attempts
            ),
            "note": "Cumulative across attempts; interrupted request usage/duration may be unknown",
        }
        totals = {}
        for attempts in run["stages"].values():
            for attempt in attempts:
                for key, number in attempt.get("reported_usage", {}).items():
                    totals[key] = totals.get(key, 0) + number
        run["metrics"]["reported_usage"] = totals
        store.save(run)
        return run
