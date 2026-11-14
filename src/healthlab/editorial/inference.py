"""Dispatch bounded editorial calls and record validation results."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

from healthlab.common import JsonObject, decode_model_json
from healthlab.data.pilot import runner_lock
from healthlab.editorial.contracts import (
    CONTEXT_CONTRACTS,
    EditorialDraft,
    canonical_proposal,
    passage_inputs,
    resolve_reference_ids,
)
from healthlab.editorial.prompts import messages
from healthlab.editorial.report import export_report
from healthlab.editorial.review_policy import (
    CitationReview,
    ContextReview,
    SemanticReview,
    validate_review,
)
from healthlab.editorial.runs import allocated_calls, jobs, load
from healthlab.editorial.scope import validate_scope
from healthlab.models import IngestionError
from healthlab.pipeline import audit_writer
from healthlab.provider import Provider
from healthlab.signal.diagnostics import Span, exception_diagnostic
from healthlab.store import RunStore, json_bytes

logger = logging.getLogger(__name__)


def run_calls(
    store: RunStore, run_id: str, provider: Provider, *, role="classify", workers=3, max_calls=180
) -> JsonObject:
    if not 1 <= workers <= 4 or not 1 <= max_calls <= 500:
        raise ValueError("Invalid bounded invocation")
    with runner_lock(store.root):
        run, records = load(store, run_id, provider, for_inference=True)
        stop, count_lock = Event(), Lock()
        dispatched = 0
        invalid_outputs = 0
        pending = [j for j in jobs(store, run) if j["role"] == role and j["status"] == "prepared"]
        all_jobs = jobs(store, run)
        if allocated_calls(run, all_jobs) > run["call_budget"]:
            raise ValueError("Lifetime call budget exceeded")

        def perform(job):
            nonlocal dispatched, invalid_outputs
            with count_lock:
                if stop.is_set() or dispatched >= max_calls:
                    return
                dispatched += 1
            row = records.get(job.get("document_id"))
            span = Span()
            try:
                proposal = (
                    json.loads(store.read(job["proposal_sha256"]))
                    if job.get("proposal_sha256")
                    else None
                )
                evidence = (
                    json.loads(store.read(job["evidence_sha256"]))
                    if job.get("evidence_sha256")
                    else None
                )
                prompt, schema = messages(
                    role, row, run["question"], proposal, evidence, version=run["version"]
                )
                job.update(
                    status="started", charged=True, prompt_sha256=store.put(json_bytes(prompt))
                )
                store.save(job)  # Counts before dispatch; interrupted calls are never auto-retried.
                completion = provider.complete_json(
                    prompt, audit_writer(store, job), schema=schema.model_json_schema()
                )
                job.update(
                    actual_model=completion.actual_model,
                    finish_reason=completion.finish_reason,
                    usage=completion.usage,
                    content_sha256=store.put(completion.content.encode()),
                )
                if completion.actual_model != run["provider"]["model"]:
                    raise IngestionError("editorial_model_identity", "Returned model differs")
                if completion.finish_reason != "stop":
                    raise IngestionError("editorial_incomplete_output", "Incomplete response")
                payload = decode_model_json(completion.content)
                if role == "classify":
                    result = canonical_proposal(
                        resolve_reference_ids(payload, row, version=run["version"]),
                        row,
                        run["question"],
                        version=run["version"],
                    )
                elif role == "scope":
                    units, _ = passage_inputs(row, version=run["version"])
                    result = validate_scope(payload, units)
                elif role == "review":
                    units, _ = passage_inputs(row, version=run["version"])
                    model = (
                        ContextReview
                        if run["version"] in CONTEXT_CONTRACTS
                        else CitationReview
                        if run["version"] == "gold-editorial-13"
                        else SemanticReview
                    )
                    model.model_validate(payload)
                    result = validate_review(
                        payload, proposal, [p["passage_id"] for p in units], run["question"]
                    )
                else:
                    result = EditorialDraft.model_validate(payload).model_dump()
                    allowed = {e["evidence_id"] for e in evidence}
                    for section in result["sections"]:
                        for claim in section["facts"]:
                            if not set(claim["evidence_ids"]) <= allowed:
                                raise ValueError("Unknown draft evidence reference")
                job.update(result_sha256=store.put(json_bytes(result)), status="completed")
            except BaseException as exc:
                job.update(
                    status="failed",
                    error_code=getattr(exc, "code", type(exc).__name__),
                    diagnostic=exception_diagnostic(exc),
                )
                if isinstance(exc, ValueError):
                    with count_lock:
                        invalid_outputs += 1
                        if invalid_outputs >= 3:
                            stop.set()
                if getattr(exc, "code", "") in {
                    "provider_auth",
                    "provider_quota_or_rate",
                    "editorial_model_identity",
                    "provider_envelope",
                }:
                    stop.set()
                if not isinstance(exc, Exception):
                    stop.set()
                    raise
            finally:
                job["timing"] = span.finish()
                store.save(job)
                logger.info(
                    "%s call for %s: %s (%s)",
                    role,
                    job.get("document_id"),
                    job["status"],
                    job.get("error_code") or "no error",
                )

        if pending:
            first = pending[0]
            canary = {
                "policy": "first-assignment-1",
                "role": role,
                "job_id": first["run_id"],
                "status": "started",
                "pending_at_start": len(pending),
                "max_calls": max_calls,
                "workers": workers,
            }
            run.setdefault("batch_canaries", []).append(canary)
            store.save(run)
            try:
                perform(first)
            finally:
                canary["status"] = "passed" if first["status"] == "completed" else "failed"
                canary["error_code"] = first.get("error_code")
                store.save(run)
            if canary["status"] == "passed" and dispatched < max_calls:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    list(pool.map(perform, pending[1:]))
        run["status"] = (
            "completed" if all(j["status"] == "completed" for j in jobs(store, run)) else "partial"
        )
        store.save(run)
        return export_report(store, run_id)
