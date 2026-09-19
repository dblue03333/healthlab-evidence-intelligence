"""Synthesize a draft brief exclusively from validated, saved evidence items."""

import hashlib
import json
import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from healthlab.brief import (
    BRIEF_SCHEMA_VERSION,
    BRIEF_VALIDATOR_VERSION,
    SYNTHESIS_PROMPT_VERSION,
    Synthesis,
    brief_version_id,
    parse_synthesis,
    render_brief,
    synthesis_messages,
    validate_claims,
)
from healthlab.evidence import ExtractedEvidence, parse_content, verify_passages
from healthlab.extraction import load_document
from healthlab.models import IngestionError
from healthlab.pipeline import audit_writer, fail, finish
from healthlab.provider import Provider
from healthlab.pubmed import parse_search
from healthlab.store import RunStore, json_bytes


def load_bundle(store, extraction_id):
    extraction = store.load(extraction_id)
    if extraction.get("mode") != "extraction" or extraction.get("status") not in {
        "completed",
        "partial",
        "failed",
    }:
        raise IngestionError("invalid_parent", "Brief requires a finished extraction run")
    ingestion = store.load(extraction["ingestion_run_id"])
    if ingestion.get("mode") not in {"online", "import_probe", "replay"}:
        raise IngestionError("invalid_parent", "Evidence must originate from ingestion")
    if extraction["question"] != ingestion["question"]:
        raise IngestionError("invalid_lineage", "Question differs between extraction and ingestion")
    search = parse_search(store.read(ingestion["search_raw_sha256"]))
    manual_ids = set(ingestion["selection"].get("manual_test_pmids", []))
    snapshots = {d["snapshot_id"]: d for d in ingestion["documents"]}
    warnings, items, excluded = [], [], []
    if extraction["status"] != "completed" or ingestion["status"] != "completed":
        warnings.append(
            "Upstream ingestion/extraction was incomplete; the brief does not cover all selected records."
        )
    for entry in extraction["results"]:
        if entry["status"] != "validated_structure":
            excluded.append(
                {
                    "snapshot_id": entry["snapshot_id"],
                    "pmid": snapshots.get(entry["snapshot_id"], {}).get("pmid"),
                    "reason": entry.get("reason", entry["status"]),
                }
            )
            continue
        if entry["snapshot_id"] not in snapshots:
            raise IngestionError(
                "invalid_lineage", "Evidence references a snapshot outside this ingestion"
            )
        reference = snapshots[entry["snapshot_id"]]
        document = load_document(store, reference)
        if not document.pmid.isdigit():
            raise IngestionError("invalid_lineage", "Cannot construct a citation from invalid PMID")
        if document.pmid in manual_ids:
            excluded.append(
                {
                    "snapshot_id": entry["snapshot_id"],
                    "pmid": document.pmid,
                    "reason": "manual_test_record",
                }
            )
            continue
        raw = json.loads(store.read(entry["evidence_item_id"]))
        if raw["snapshot_id"] != entry["snapshot_id"] or raw["document_id"] != document.document_id:
            raise IngestionError(
                "invalid_lineage", "Evidence identity does not match its source snapshot"
            )
        evidence = ExtractedEvidence.model_validate(raw["evidence"])
        original = parse_content(store.read(raw["content_sha256"]).decode())
        if original != evidence:
            raise IngestionError(
                "invalid_lineage", "Parsed evidence differs from original model output"
            )
        passages = verify_passages(evidence, document, entry["snapshot_id"])
        if passages != raw["passages"]:
            raise IngestionError(
                "invalid_lineage", "Saved passage identities or offsets do not match source"
            )
        if evidence.main_finding is None:
            excluded.append(
                {
                    "snapshot_id": entry["snapshot_id"],
                    "pmid": document.pmid,
                    "reason": "no_extracted_finding",
                    "title": document.title,
                    "publication_types": document.publication_types,
                    "study_type": evidence.study_type,
                }
            )
            continue
        if entry["evidence_item_id"] in {i["evidence_item_id"] for i in items}:
            raise IngestionError("invalid_lineage", "Duplicate evidence item in extraction run")
        extracted_passage_ids = [p["passage_id"] for p in passages]
        # Source context is deterministic, not additional LLM findings. Original
        # EvidenceItems remain immutable; these passages bind to their snapshots.
        for index, section in enumerate(document.abstract_sections):
            if not section.text.strip():
                continue
            context = {
                "snapshot_id": entry["snapshot_id"],
                "section_index": index,
                "start": 0,
                "end": len(section.text),
                "text": section.text,
            }
            pid = hashlib.sha256(json_bytes(context)).hexdigest()
            if pid not in {p["passage_id"] for p in passages}:
                passages.append({"field": f"source_context.{index}", **context, "passage_id": pid})
        items.append(
            {
                "evidence_item_id": entry["evidence_item_id"],
                "snapshot_id": entry["snapshot_id"],
                "document": document.model_dump(),
                "raw_record_sha256": reference["raw_record_sha256"],
                "evidence": evidence.model_dump(),
                "passages": passages,
                "extracted_passage_ids": extracted_passage_ids,
            }
        )
    requests = ingestion.get("source_requests", ingestion["requests"])
    searches = [
        r for r in requests if r.get("endpoint") == "esearch.fcgi" and r.get("http_status") == 200
    ]
    if searches:
        search_time = searches[-1]["started_at"]
        search_params = {
            k: v for k, v in searches[-1]["params"].items() if k not in {"email", "api_key"}
        }
    else:
        provenance = ingestion.get("source_provenance") or {}
        search_time = (
            str(provenance.get("reported_timestamp") or "Unknown")
            + " (legacy artifact time; exact search time unavailable)"
        )
        if provenance.get("artifact_sha256"):
            artifact = json.loads(store.read(provenance["artifact_sha256"]))
            search_params = {
                k: v
                for k, v in artifact["experiment_a_sort_relevance"]["params"].items()
                if k not in {"email", "api_key"}
            }
        else:
            search_params = {
                "query": ingestion["question"]["query"],
                "sort": ingestion["question"]["sort"],
            }
    coverage = {
        "search_observed_at": search_time,
        "search_params": search_params,
        "query_translation": search["query_translation"],
        "matched": search["count"],
        "selected": len(ingestion["selection"]["selected_pmids"]),
        "stored": len(ingestion["documents"]),
        "usable_evidence": len(items),
        "extraction_counts": extraction["counts"],
        "excluded": excluded,
        "manual_test_pmids": sorted(manual_ids),
        "research_candidates": sum(
            p not in manual_ids for p in ingestion["selection"]["selected_pmids"]
        ),
    }
    if excluded:
        warnings.append(
            f"{len(excluded)} extraction records excluded; reasons are recorded in the brief JSON coverage.excluded."
        )
    if manual_ids:
        warnings.append(
            "Manual CP3 test records are not research candidates and are excluded from synthesis."
        )
    return extraction, ingestion, items, coverage, warnings


def write_preview(path: Path, text: str):
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def synthesize_run(store: RunStore, extraction_id: str, provider: Provider):
    parent = store.load(extraction_id)
    if parent.get("mode") != "extraction":
        raise IngestionError("invalid_parent", "Brief requires an extraction run ID")
    run = store.new("synthesis", parent["question"])
    run.update(
        extraction_run_id=extraction_id,
        stage="load_evidence",
        provider=provider.identity(),
        prompt_version=SYNTHESIS_PROMPT_VERSION,
        schema_version=BRIEF_SCHEMA_VERSION,
        validator_version=BRIEF_VALIDATOR_VERSION,
        review_status="AI-generated — not reviewed",
    )
    store.save(run)
    try:
        extraction, ingestion, items, coverage, warnings = load_bundle(store, extraction_id)
        run.update(
            ingestion_run_id=ingestion["run_id"],
            warnings=warnings,
            evidence_item_ids=[i["evidence_item_id"] for i in items],
        )
        if items:
            messages = synthesis_messages(parent["question"], items)
            run.update(
                stage="synthesis",
                input_sha256=store.put(
                    json_bytes(
                        {
                            "question": parent["question"],
                            "messages": messages,
                            "coverage": coverage,
                            "provider": provider.identity(),
                            "schema_version": BRIEF_SCHEMA_VERSION,
                            "prompt_version": SYNTHESIS_PROMPT_VERSION,
                        }
                    )
                ),
            )
            store.save(run)
            completion = provider.complete(messages, audit_writer(store, run))
            run.update(
                content_sha256=store.put(completion.content.encode()),
                actual_model=completion.actual_model,
                finish_reason=completion.finish_reason,
                usage=completion.usage,
                stage="validate_claims",
            )
            store.save(run)
            if completion.finish_reason != "stop":
                raise IngestionError("synthesis_incomplete", "Synthesis did not finish normally")
            synthesis = parse_synthesis(completion.content)
        else:
            synthesis = Synthesis(
                claims=[], short_answer_claim_ids=[], evidence_sufficiency="insufficient"
            )
            run["outcome"] = "no_usable_evidence"
        validation = validate_claims(synthesis, items)
        run["validation"] = validation
        version = brief_version_id(run["run_id"], synthesis)
        brief = {
            "brief_version_id": version,
            "run_id": run["run_id"],
            "created_at": run["created_at"],
            "schema_version": BRIEF_SCHEMA_VERSION,
            "prompt_version": SYNTHESIS_PROMPT_VERSION,
            "validator_version": BRIEF_VALIDATOR_VERSION,
            "ingestion_run_id": ingestion["run_id"],
            "extraction_run_id": extraction_id,
            "question": parent["question"],
            "coverage": coverage,
            "warnings": warnings,
            "validation": validation,
            "review_status": "AI-generated — not reviewed",
            "synthesis": synthesis.model_dump(),
            "evidence_items": items,
        }
        run.update(
            stage="render", brief_version_id=version, brief_sha256=store.put(json_bytes(brief))
        )
        rendered = render_brief(brief)
        run["markdown_sha256"] = store.put(rendered.encode())
        write_preview(store.root / "runs" / run["run_id"] / "brief.md", rendered)
        run.update(
            status="partial"
            if extraction["status"] != "completed" or ingestion["status"] != "completed"
            else "completed",
            stage="finished",
            counts={
                "claims": len(synthesis.claims),
                "evidence_items": len(items),
                "excluded_records": len(coverage["excluded"]),
            },
        )
        return finish(store, run)
    except IngestionError as exc:
        return fail(store, run, exc)
    except (ValueError, KeyError, TypeError, AttributeError, ValidationError):
        return fail(
            store,
            run,
            IngestionError("invalid_evidence_contract", "Invalid stored evidence/lineage contract"),
        )
    except OSError:
        return fail(
            store, run, IngestionError("storage_error", "Could not persist brief artifacts")
        )


def render_saved_brief(store: RunStore, synthesis_id: str):
    run = store.load(synthesis_id)
    if (
        run.get("mode") != "synthesis"
        or run.get("status") not in {"completed", "partial"}
        or "brief_sha256" not in run
        or "markdown_sha256" not in run
    ):
        raise IngestionError("invalid_parent", "No validated brief is available in this run")
    brief = json.loads(store.read(run["brief_sha256"]))
    # Verify live source objects too. An HTML/Markdown preview is never the authority.
    _, _, items, _, _ = load_bundle(store, brief["extraction_run_id"])
    current = {i["evidence_item_id"]: i for i in items}
    for saved in brief["evidence_items"]:
        actual = current.get(saved["evidence_item_id"])
        if (
            actual is None
            or any(
                saved[key] != actual[key]
                for key in ("snapshot_id", "document", "raw_record_sha256", "evidence")
            )
            or any(p not in actual["passages"] for p in saved["passages"])
        ):
            raise IngestionError("invalid_lineage", "Brief inputs differ from saved extraction")
    if run["validator_version"] != brief["validator_version"]:
        raise IngestionError("invalid_lineage", "Brief validator differs from its run")
    validate_claims(
        Synthesis.model_validate(brief["synthesis"]),
        brief["evidence_items"],
        validator_version=brief["validator_version"],
    )
    content = store.read(run["markdown_sha256"]).decode()
    path = store.root / "runs" / synthesis_id / "brief.md"
    write_preview(path, content)
    return path
