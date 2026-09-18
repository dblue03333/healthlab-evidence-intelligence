"""Sequential ingestion, local CP3 import, and network-free replay."""

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from healthlab.models import PARSER_VERSION, IngestionError, Question
from healthlab.pubmed import parse_records, parse_search, utc_now
from healthlab.store import RunStore, json_bytes


def audit_writer(store, run):
    def audit(event, body):
        if body is not None:
            event["body_sha256"] = store.put(body)
        run["requests"].append(event)
        store.save(run)

    return audit


def fail(store, run, exc):
    run["status"] = "failed"
    run["errors"].append({"stage": run["stage"], "code": exc.code, "message": str(exc)})
    run["finished_at"] = utc_now()
    # If storage itself failed, this attempt can also fail. The CLI reports it;
    # we must never claim a durable failure manifest when the disk is unavailable.
    store.save(run)
    return run


def finish(store, run):
    run["finished_at"] = utc_now()
    store.save(run)
    return run


def normalize(store, run, raw):
    run["stage"] = "parse"
    store.save(run)
    documents, report = parse_records(raw)
    requested = run["selection"]["selected_pmids"]
    returned = {d.pmid for d in documents}
    missing = [p for p in requested if p not in returned]
    unexpected = sorted(returned - set(requested))
    raw_records = {}
    # Preserve per-record metadata not yet represented by the normalizer.
    for node in ET.fromstring(raw).findall("PubmedArticle"):
        pmid = node.findtext("./MedlineCitation/PMID", "").strip()
        node.tail = None
        canonical = ET.canonicalize(ET.tostring(node, encoding="unicode")).encode("utf-8")
        raw_records.setdefault(pmid, canonical)
    by_id = {d.pmid: d for d in documents}
    for pmid in requested:
        if pmid not in by_id:
            continue
        doc = by_id[pmid]
        raw_digest = store.put(raw_records[pmid])
        snapshot = {
            "parser_version": PARSER_VERSION,
            "document": doc.model_dump(),
            "raw_record_sha256": raw_digest,
        }
        snapshot_id = store.put(json_bytes(snapshot))
        run["documents"].append(
            {
                "document_id": doc.document_id,
                "pmid": pmid,
                "snapshot_id": snapshot_id,
                "raw_record_sha256": raw_digest,
                "extraction_eligibility": doc.extraction_eligibility,
                "skip_reason": doc.skip_reason,
            }
        )
    run["parse_report"] = {
        **report,
        "missing_pmids": missing,
        "unexpected_pmids": unexpected,
    }
    if missing or unexpected or report["unsupported_tags"]:
        run["warnings"].append("Fetch response does not fully match supported requested records")
        run["status"] = "partial"
    else:
        run["status"] = "completed"
    run["counts"] = {
        "matched": run["search"]["count"],
        "selected": len(requested),
        "stored": len(run["documents"]),
        "eligible": sum(d["extraction_eligibility"] == "eligible" for d in run["documents"]),
        "skipped": sum(d["extraction_eligibility"] == "skipped" for d in run["documents"]),
    }
    run["stage"] = "finished"
    return finish(store, run)


def ingest(store: RunStore, question: Question, client):
    run = store.new("online", question.model_dump(mode="json"))
    try:
        run["stage"] = "search"
        store.save(run)
        raw = client.request("esearch.fcgi", question.search_params(), audit_writer(store, run))
        run["search_raw_sha256"] = store.put(raw)
        run["search"] = parse_search(raw)
        run["selection"] = {
            "policy": "top_5_unique_no_backfill",
            "selected_pmids": run["search"]["selected_pmids"],
            "manual_test_pmids": [],
        }
        store.save(run)
        selected = run["selection"]["selected_pmids"]
        if not selected:
            run.update(
                status="completed",
                stage="finished",
                outcome="no_results",
                counts={
                    "matched": 0,
                    "selected": 0,
                    "stored": 0,
                    "eligible": 0,
                    "skipped": 0,
                },
            )
            return finish(store, run)
        run["stage"] = "fetch"
        store.save(run)
        params = {"db": "pubmed", "id": ",".join(selected), "retmode": "xml"}
        raw = client.request("efetch.fcgi", params, audit_writer(store, run))
        run["fetch_raw_sha256"] = store.put(raw)
        store.save(run)
        return normalize(store, run, raw)
    except IngestionError as exc:
        return fail(store, run, exc)
    except OSError:
        return fail(store, run, IngestionError("storage_error", "Could not persist run data"))


def replay(store: RunStore, parent_id: str):
    parent = store.load(parent_id)
    if parent.get("mode") not in {"online", "replay", "import_probe"}:
        raise IngestionError("invalid_parent", "Replay requires an ingestion run")
    question = Question.model_validate(parent["question"])
    run = store.new("replay", question.model_dump(mode="json"))
    run["parent_run_id"] = parent_id
    run["source_requests"] = parent.get("source_requests", parent["requests"])
    run["source_provenance"] = parent.get("source_provenance")
    try:
        run["stage"] = "replay"
        if "search_raw_sha256" not in parent or "selection" not in parent:
            raise IngestionError(
                "cache_incomplete", "Replay requires a valid saved search and selection"
            )
        run["search_raw_sha256"] = parent["search_raw_sha256"]
        run["search"] = parse_search(store.read(run["search_raw_sha256"]))
        run["selection"] = parent["selection"]
        if not run["selection"]["selected_pmids"]:
            if run["search"]["count"] != 0:
                raise IngestionError("cache_incomplete", "Empty selection with nonempty search")
            run.update(
                status="completed",
                stage="finished",
                outcome="no_results",
                counts={
                    "matched": 0,
                    "selected": 0,
                    "stored": 0,
                    "eligible": 0,
                    "skipped": 0,
                },
            )
            return finish(store, run)
        if "fetch_raw_sha256" not in parent:
            raise IngestionError(
                "cache_incomplete",
                "No successful fetch cached; replay does not call network",
            )
        run["fetch_raw_sha256"] = parent["fetch_raw_sha256"]
        return normalize(store, run, store.read(run["fetch_raw_sha256"]))
    except IngestionError as exc:
        return fail(store, run, exc)


def import_probe(store: RunStore, artifact_path: Path, xml_path: Path):
    """Import the actual CP3 selection (including manual edge case), not a fake online run."""
    artifact_bytes = artifact_path.read_bytes()
    xml = xml_path.read_bytes()
    try:
        data = json.loads(artifact_bytes)
        search = data["experiment_a_sort_relevance"]
        fetch = data["experiment_c_fetch"]
        params = search["params"]
        question = Question(raw_question=data["query"], query=params["term"], sort=params["sort"])
        raw = json_bytes(search["raw_response"])
        parsed_search = parse_search(raw)
        selected, candidates, manual = (
            fetch["total_requested"],
            fetch["search_candidates_pmids"],
            fetch["manual_test_pmids"],
        )
        if (
            not isinstance(selected, list)
            or len(selected) != len(set(selected))
            or any(not isinstance(p, str) or not p.isdigit() for p in selected)
            or selected != candidates + manual
            or fetch["fetch_params"]["id"].split(",") != selected
            or any(p not in parsed_search["ordered_pmids"] for p in candidates)
            or search["pmids"] != parsed_search["ordered_pmids"]
        ):
            raise ValueError("inconsistent probe selection")
        if params.get("mindate") or params.get("maxdate"):
            raise ValueError("import expects the unfiltered CP3 relevance experiment")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise IngestionError("invalid_probe", "Expected consistent CP3 pipeline artifact") from exc
    run = store.new("import_probe", question.model_dump(mode="json"))
    run["source_provenance"] = {
        "kind": "user_supplied_cp3_artifact",
        "artifact_sha256": store.put(artifact_bytes),
        "reported_timestamp": data.get("timestamp"),
        "request_timestamps": "not_recorded_in_original_artifact",
    }
    run["search_raw_sha256"] = store.put(raw)
    run["fetch_raw_sha256"] = store.put(xml)
    run["search"] = parsed_search
    run["selection"] = {
        "policy": "imported_cp3_selection",
        "selected_pmids": selected,
        "search_candidates_pmids": candidates,
        "manual_test_pmids": manual,
    }
    store.save(run)
    try:
        return normalize(store, run, xml)
    except IngestionError as exc:
        return fail(store, run, exc)
