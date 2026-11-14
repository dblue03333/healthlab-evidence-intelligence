"""Second-model recall check: did the pipeline drop an article that is on topic?

A model from a different family (GLM-5.2 on FPT by default) reads every article the run
did not release, with the pipeline's own scope prompt and criteria, unchanged. An article
it rates "substantive" without outside knowledge is shown to the approver as "possibly
missed". Nothing is released by this check: the approver promotes or ignores each flag,
and the approval records which flags were promoted, so the decisions become labels.

This file is deliberately outside the editorial runtime fingerprint; adding or changing it
never turns a finished editorial run into a read-only archive.

Results are saved after every article. A rerun calls only articles without a valid result.

Usage (repo root):
    python -m healthlab.editorial.recall --store STORE check RUN_ID
    python -m healthlab.editorial.recall --store STORE status RUN_ID
"""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from healthlab.common import JsonObject, configure_logging, decode_model_json, utc_now
from healthlab.config import FPTSettings
from healthlab.data.pilot import runner_lock
from healthlab.editorial.contracts import passage_inputs
from healthlab.editorial.packet import evidence_packet
from healthlab.editorial.prompts import messages
from healthlab.editorial.runs import jobs, load
from healthlab.editorial.scope import validate_scope
from healthlab.models import IngestionError
from healthlab.pipeline import audit_writer
from healthlab.provider import FPTProvider, Provider
from healthlab.store import RunStore, json_bytes

VERSION = "recall-check-1"
MODE = "editorial_recall"
DEFAULT_MODEL = "GLM-5.2"


def recall_provider(model: None = None, client: None = None) -> Provider:
    """FPT with the recall model. Thinking is left to the provider, as in the model trial."""
    settings = FPTSettings().model_copy(
        update={
            "model": model or os.environ.get("HEALTHLAB_RECALL_MODEL") or DEFAULT_MODEL,
            "thinking_mode": "provider_default",
        }
    )
    return FPTProvider(settings, client)


def flagged(assessment: JsonObject) -> bool:
    return assessment["verdict"] == "substantive" and not assessment["requires_outside_knowledge"]


def targets(store: RunStore, run: JsonObject) -> list[str]:
    """Articles the run did not release; refuse while the run is still unfinished."""
    calls = [j for j in jobs(store, run) if j["role"] != "draft"]
    if not calls or any(j["status"] != "completed" for j in calls):
        raise ValueError("Editorial run is unfinished or has failed calls; finish it first")
    packet = evidence_packet(store, run["run_id"])
    return [a["document_id"] for a in packet["articles"] if not a["eligible_for_unapproved_draft"]]


def check(store: RunStore, run_id: str, provider: Provider, *, max_calls=None) -> JsonObject:
    """Run the recall check, resuming a previous one. Returns the recall record."""
    with runner_lock(store.root):
        run, records = load(store, run_id)
        wanted = targets(store, run)
        if run.get("recall_check_id"):
            record = store.load(run["recall_check_id"])
            if record["provider"] != provider.identity():
                raise ValueError("Recall check exists with another model; it cannot be mixed")
        else:
            record = store.new(MODE, run["question"])
            record.update(
                version=VERSION,
                editorial_run_id=run_id,
                editorial_version=run["version"],
                provider=provider.identity(),
                targets=wanted,
                results={},
                calls=0,
            )
            store.save(record)
            run["recall_check_id"] = record["run_id"]
            store.save(run)
        if record["targets"] != wanted:
            raise ValueError("The run's held-back articles changed since the recall check began")
        # Every target at most twice: one attempt plus one rerun after a failure.
        budget = 2 * len(wanted) if max_calls is None else max_calls
        audit = audit_writer(store, record)
        for document_id in wanted:
            if record["results"].get(document_id, {}).get("status") == "completed":
                continue
            if record["calls"] >= budget:
                break
            row = records[document_id]
            prompt, schema = messages("scope", row, run["question"], version=run["version"])
            record["calls"] += 1
            result: JsonObject = {"status": "started", "attempted_at": utc_now()}
            record["results"][document_id] = result
            store.save(record)  # Counted before dispatch, like editorial calls.
            try:
                completion = provider.complete_json(
                    prompt, audit, schema=schema.model_json_schema()
                )
                result.update(
                    actual_model=completion.actual_model,
                    finish_reason=completion.finish_reason,
                    usage=completion.usage,
                    content_sha256=store.put(completion.content.encode()),
                )
                if completion.actual_model != record["provider"]["model"]:
                    raise IngestionError("recall_model_identity", "Returned model differs")
                if completion.finish_reason != "stop":
                    raise IngestionError("recall_incomplete_output", "Incomplete response")
                units, _ = passage_inputs(row, version=run["version"])
                assessment = validate_scope(decode_model_json(completion.content), units)
            except (IngestionError, ValueError) as exc:
                code = getattr(exc, "code", type(exc).__name__)
                result.update(status="failed", error_code=code, error=str(exc)[:300])
            else:
                result.update(
                    status="completed",
                    verdict=assessment["verdict"],
                    flagged=flagged(assessment),
                    assessment_sha256=store.put(json_bytes(assessment)),
                )
            store.save(record)
        done = sum(r.get("status") == "completed" for r in record["results"].values())
        record["status"] = "completed" if done == len(wanted) else "partial"
        record["finished_at"] = utc_now()
        store.save(record)
    return record


def summary(store: RunStore, run_id: str) -> JsonObject | None:
    """What the approver sees: progress and each flagged article with its passages."""
    run, records = load(store, run_id)
    if not run.get("recall_check_id"):
        return None
    record = store.load(run["recall_check_id"])
    results = record["results"]
    flags = []
    for document_id in record["targets"]:
        result = results.get(document_id, {})
        if result.get("status") != "completed" or not result["flagged"]:
            continue
        assessment = json.loads(store.read(result["assessment_sha256"]))
        row = records[document_id]
        flags.append(
            {
                "document_id": document_id,
                "title": row["title"],
                "url": row["url"],
                "ageing_context": assessment["ageing_context"],
                "substantive_detail": assessment["substantive_detail"],
                "rationale": assessment["rationale"],
                "passages": assessment["source_passages"],
            }
        )
    return {
        "recall_check_id": record["run_id"],
        "model": record["provider"]["model"],
        "targets": len(record["targets"]),
        "completed": sum(r.get("status") == "completed" for r in results.values()),
        "failed": sum(r.get("status") == "failed" for r in results.values()),
        "flags": flags,
    }


def main(argv=None):
    load_dotenv()
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--store", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "status"):
        commands.add_parser(name).add_argument("run_id")
    args = parser.parse_args(argv)
    store = RunStore(args.store)
    try:
        if args.command == "check":
            provider = recall_provider()
            try:
                check(store, args.run_id, provider)
            finally:
                provider.close()
        result = summary(store, args.run_id)
        if result is None:
            raise ValueError("No recall check has been run for this editorial run")
        result["flags"] = [{k: f[k] for k in ("document_id", "title")} for f in result["flags"]]
    except (IngestionError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
