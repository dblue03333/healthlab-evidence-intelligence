"""Paper-level extraction of saved snapshots, with explicit cache and failure states."""

import hashlib
import json
import os
import tempfile

from pydantic import ValidationError

from healthlab.evidence import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    VALIDATOR_VERSION,
    ExtractedEvidence,
    build_messages,
    parse_content,
    verify_passages,
)
from healthlab.models import Document, IngestionError
from healthlab.pipeline import audit_writer, finish
from healthlab.provider import Completion, Provider
from healthlab.store import RunStore, json_bytes


def cache_identity(snapshot_id, messages, identity):
    inputs = {
        "snapshot_id": snapshot_id,
        "messages": messages,
        "provider": identity,
        "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "schema": ExtractedEvidence.model_json_schema(),
    }
    return hashlib.sha256(json_bytes(inputs)).hexdigest(), inputs


def cache_path(store, key):
    return store.root / "extraction_cache" / f"{key}.json"


def publish_cache(store, key, result):
    digest = store.put(json_bytes(result))
    path = cache_path(store, key)
    path.parent.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json_bytes({"result_sha256": digest}))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return digest


def read_cache(store, key):
    path = cache_path(store, key)
    if not path.exists():
        return None
    try:
        reference = json.loads(path.read_bytes())
        record = json.loads(store.read(reference["result_sha256"]))
        if record["cache_key"] != key:
            raise ValueError("cache identity mismatch")
        return record
    except (ValueError, KeyError, TypeError, AttributeError):
        raise IngestionError(
            "extraction_cache_invalid", "Invalid extraction cache; use --refresh after inspection"
        ) from None


def load_document(store, reference):
    try:
        snapshot = json.loads(store.read(reference["snapshot_id"]))
        store.read(snapshot["raw_record_sha256"])
        document = Document.model_validate(snapshot["document"])
        if document.document_id != reference["document_id"]:
            raise ValueError("snapshot identity mismatch")
        actual_has_abstract = any(s.text.strip() for s in document.abstract_sections)
        if document.has_abstract != actual_has_abstract:
            raise ValueError("abstract flag mismatch")
        return document
    except (ValueError, KeyError, TypeError, ValidationError):
        raise IngestionError(
            "invalid_snapshot", "Snapshot contract or document identity is invalid"
        ) from None


def extract_run(
    store: RunStore,
    ingestion_run_id: str,
    provider: Provider,
    *,
    refresh=False,
    max_documents: int | None = None,
):
    parent = store.load(ingestion_run_id)
    if parent["mode"] not in {"online", "import_probe", "replay"} or parent["status"] not in {
        "completed",
        "partial",
    }:
        raise IngestionError(
            "invalid_parent", "Extraction requires a completed/partial ingestion run"
        )
    if max_documents is not None and max_documents < 1:
        raise ValueError("max_documents must be positive")
    run = store.new("extraction", parent["question"])
    run.update(
        ingestion_run_id=ingestion_run_id,
        stage="extract",
        provider=provider.identity(),
        prompt_version=PROMPT_VERSION,
        schema_version=SCHEMA_VERSION,
        validator_version=VALIDATOR_VERSION,
        refresh=refresh,
        max_documents=max_documents,
        input_documents=parent["documents"],
        results=[],
    )
    if parent["status"] == "partial":
        run["warnings"].append("Input ingestion was partial; inspect its parse report")
    store.save(run)
    processed = 0
    for reference in parent["documents"]:
        entry = {
            "snapshot_id": reference["snapshot_id"],
            "document_id": reference["document_id"],
            "status": "pending",
            "schema_valid": None,
            "passage_match": None,
            "semantic_support": "not_evaluated",
        }
        run["results"].append(entry)
        try:
            document = load_document(store, reference)
            if not document.has_abstract:
                entry.update(status="skipped", reason="missing_abstract")
                continue
            if max_documents is not None and processed >= max_documents:
                entry.update(status="skipped", reason="document_limit")
                continue
            processed += 1
            messages = build_messages(document)
            key, inputs = cache_identity(reference["snapshot_id"], messages, provider.identity())
            entry.update(cache_key=key, input_sha256=store.put(json_bytes(inputs)))
            store.save(run)
            cached = None if refresh else read_cache(store, key)
            if cached:
                completion = Completion(**cached["completion"])
                entry.update(cache_hit=True, source_extraction_run_id=cached["source_run_id"])
                # Validate the original content, not a cached PASS flag.
                store.read(cached["content_sha256"])
                entry["content_sha256"] = cached["content_sha256"]
                if store.read(entry["content_sha256"]).decode() != completion.content:
                    raise IngestionError("extraction_cache_invalid", "Cached content mismatch")
            else:

                def audit(event, body):
                    audit_writer(store, run)(
                        {**event, "snapshot_id": reference["snapshot_id"]}, body
                    )

                completion = provider.complete(messages, audit)
                entry.update(cache_hit=False, content_sha256=store.put(completion.content.encode()))
            entry.update(
                actual_model=completion.actual_model,
                finish_reason=completion.finish_reason,
                usage=completion.usage,
            )
            if completion.finish_reason != "stop":
                code = (
                    "output_truncated"
                    if completion.finish_reason == "length"
                    else "finish_reason_rejected"
                )
                raise IngestionError(
                    code, "Completion did not finish normally; raw output retained"
                )
            evidence = parse_content(completion.content)
            entry["schema_valid"] = True
            passages = verify_passages(evidence, document, reference["snapshot_id"])
            entry["passage_match"] = True
            result = {
                "cache_key": key,
                "source_run_id": cached["source_run_id"] if cached else run["run_id"],
                "snapshot_id": reference["snapshot_id"],
                "document_id": document.document_id,
                "completion": vars(completion),
                "content_sha256": entry["content_sha256"],
                "evidence": evidence.model_dump(),
                "passages": passages,
                "schema_valid": True,
                "passage_match": True,
                "semantic_support": "not_evaluated",
                "source_limitations": ["abstract_only"],
                "provider": provider.identity(),
                "prompt_version": PROMPT_VERSION,
                "schema_version": SCHEMA_VERSION,
                "validator_version": VALIDATOR_VERSION,
            }
            entry.update(
                status="validated_structure", evidence_item_id=store.put(json_bytes(result))
            )
            if not cached:
                publish_cache(store, key, result)
        except IngestionError as exc:
            if exc.code == "schema_invalid":
                entry["schema_valid"] = False
            if exc.code == "passage_not_found":
                entry["passage_match"] = False
            entry.update(status="failed", error={"code": exc.code, "message": str(exc)})
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            entry.update(
                status="failed",
                error={"code": "invalid_cached_contract", "message": type(exc).__name__},
            )
        except OSError:
            entry.update(
                status="failed",
                error={"code": "storage_error", "message": "Could not persist extraction"},
            )
        finally:
            store.save(run)
    failed = sum(e["status"] == "failed" for e in run["results"])
    validated = sum(e["status"] == "validated_structure" for e in run["results"])
    limited = any(e.get("reason") == "document_limit" for e in run["results"])
    run["errors"] = [
        {"snapshot_id": e["snapshot_id"], **e["error"]}
        for e in run["results"]
        if e["status"] == "failed"
    ]
    if limited:
        run["warnings"].append("Eligible snapshots were skipped due to max_documents")
    run["counts"] = {
        "input": len(parent["documents"]),
        "validated_structure": validated,
        "failed": failed,
        "skipped": sum(e["status"] == "skipped" for e in run["results"]),
        "cache_hits": sum(e.get("cache_hit", False) for e in run["results"]),
    }
    run["status"] = (
        "failed"
        if failed and not validated
        else ("partial" if failed or limited or parent["status"] == "partial" else "completed")
    )
    run["stage"] = "finished"
    return finish(store, run)
