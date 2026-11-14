"""Build, validate and read immutable Bronze/Silver/Gold releases offline."""

import hashlib
import json
from collections import Counter
from typing import Any
from urllib.parse import parse_qs, urlsplit

from healthlab.common import JsonObject, utc_now
from healthlab.data.contracts import (
    Bronze,
    Dataset,
    Gold,
    NewsletterObservation,
    Observation,
    Silver,
    SourceQuality,
    Window,
)
from healthlab.models import IngestionError
from healthlab.signal.backup import object_references
from healthlab.signal.collection import load_documents
from healthlab.signal.contracts import Registry, Source
from healthlab.signal.html_adapter import ConfiguredHTMLAdapter, iso_day
from healthlab.signal.storage import CollectionStorage, collection_storage
from healthlab.store import RunStore, json_bytes


def encoded(value: Any) -> bytes:
    return json_bytes(value.model_dump(mode="json"))


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def verify_objects(storage: CollectionStorage, root: str) -> set[str]:
    """Verify the complete reference closure, including raw historical evidence."""
    pending, checked = [root], set()
    while pending:
        ref = pending.pop()
        if ref not in checked:
            pending.extend(object_references(storage.read(ref)))
            checked.add(ref)
    return checked


def document_identity(source: Source, url: str) -> str:
    # The Ministry may publish one article in multiple categories. cid is a
    # navigation context, while aid identifies the document across categories.
    identity = url
    if source.html_discovery and source.html_discovery.moh_portal_categories:
        identity = "moh-article:" + parse_qs(urlsplit(url).query)["aid"][0]
    return source.source_id + ":" + hashlib.sha256(identity.encode()).hexdigest()


def _response_raw(source: Source, url: str, checkpoint: JsonObject) -> str | None:
    response = checkpoint.get("responses", {}).get(url)
    if response:
        return response["raw_sha256"]
    if source.html_discovery and source.html_discovery.moh_portal_categories:
        aid = parse_qs(urlsplit(url).query).get("aid", [None])[0]
        for attempt in reversed(checkpoint.get("http_attempts", [])):
            if (
                attempt.get("request_json", {}).get("maBaiViet") == aid
                and attempt.get("status") == "completed"
            ):
                return attempt["raw_sha256"]
    return None


def _observation(
    storage: CollectionStorage,
    source: Source,
    check: JsonObject,
    checkpoint: JsonObject,
    reference: dict[str, str] | JsonObject | None,
    documents: JsonObject,
    window: Window,
) -> Observation | NewsletterObservation:
    if (
        not ConfiguredHTMLAdapter(source.resolved_html_rules)
        .classify_url(check["url"], source.url or "")  # urljoin treats both alike.
        .accepted
    ):
        raise ValueError("Observation URL is outside its configured source scope")
    parsed = (
        json.loads(storage.read(check["extraction_sha256"]))
        if check.get("extraction_sha256")
        else {}
    )
    raw = check.get("raw_sha256") or _response_raw(source, check["url"], checkpoint)
    if parsed and (parsed.get("raw_sha256") != raw or parsed.get("url") != check["url"]):
        raise ValueError("Extraction lineage differs from article observation")
    evidence = [
        dict(e, raw_sha256=e.get("raw_sha256") or raw)
        for e in parsed.get("publication_date_evidence", [])
        or check.get("publication_date_evidence", [])
    ]
    displayed = source.resolved_html_rules.window_date_policy == "source_displayed"
    newsletter = window.basis == "configured_source_calendar_date"
    selection: JsonObject = {}
    if newsletter:
        selection = {
            "selection_date": check.get("selection_date")
            if displayed
            else check.get("publication_date"),
            "selection_date_basis": check.get("selection_date_basis", "listing_displayed")
            if displayed
            else "publication",
            "selection_date_evidence": [
                dict(e, raw_sha256=e.get("raw_sha256") or raw)
                for e in (
                    parsed.get("selection_date_evidence", [])
                    or check.get("selection_date_evidence", [])
                )
            ]
            if displayed
            else evidence,
        }
    record = (NewsletterObservation if newsletter else Observation)(
        **selection,
        document_id=document_identity(source, check["url"]),
        source_id=source.source_id,
        url=check["url"],
        outcome=check["status"],
        title=parsed.get("title"),
        body=parsed.get("body"),
        publication_date=check.get("publication_date"),
        modified_date_values=parsed.get("modified_date_values", []),
        publication_date_evidence=evidence,
        passages=parsed.get("passages", []),
        raw_sha256=raw,
        extraction_sha256=check.get("extraction_sha256"),
        snapshot_id=reference["snapshot_id"] if reference else None,
        previous_snapshot_id=reference.get("previous_snapshot_id") if reference else None,
        error_code=check.get("error_code"),
        content_coverage=check.get("content_coverage"),
        recovered_from_history=bool(check.get("historical_candidate")),
    )
    if record.outcome == "included":
        # The window basis chose the model above, so this is the newsletter case.
        if isinstance(record, NewsletterObservation):
            selected_day, selected_evidence = record.selection_date, record.selection_date_evidence
        else:
            selected_day, selected_evidence = record.publication_date, evidence
        if not reference or not parsed or not selected_evidence or not selected_day:
            raise ValueError("Included article lacks document or publication evidence")
        doc = documents[reference["snapshot_id"]]
        if (
            doc.title != record.title
            or record.title is None
            or record.body is None
            or doc.text != record.title + "\n\n" + record.body
            or doc.publication_date
            != (record.publication_date.isoformat() if record.publication_date else None)
            or parsed.get("publication_date") != doc.publication_date
            or (displayed and parsed.get("selection_date") != selected_day.isoformat())
            or not window.mindate <= selected_day <= window.maxdate
            or {iso_day(e["value"]) for e in selected_evidence} != {selected_day}
        ):
            raise ValueError("Included article does not match its normalized text/date evidence")
    elif reference:
        raise ValueError("Unaccepted article unexpectedly has a selected snapshot")
    if parsed:
        record.content_version = hashlib.sha256(
            json_bytes(
                {
                    "document_id": record.document_id,
                    "title": record.title,
                    "body": record.body,
                    "publication_date": check.get("publication_date"),
                    "modified_date_values": record.modified_date_values,
                }
            )
        ).hexdigest()
    return record


def assemble(
    storage: CollectionStorage, collection: JsonObject, collection_digest: str
) -> tuple[Dataset, Bronze, Silver, Gold]:
    """Deterministic transforms; no writes, network, model or editorial selection."""
    if collection.get("mode") != "signal_collection" or collection["status"] == "running":
        raise ValueError("Dataset requires a finished collection")
    registry = Registry.model_validate(collection["registry"])
    enabled = [s for s in registry.sources if s.enabled]
    if not enabled or any(s.kind != "html" for s in enabled):
        raise ValueError("Dataset requires enabled HTML/API sources; PubMed is out of scope")
    window = Window.model_validate(collection["question"])
    if any(s.resolved_html_rules.window_date_policy == "source_displayed" for s in enabled):
        window.basis = "configured_source_calendar_date"
    documents = load_documents(storage, collection)
    refs = {
        (documents[r["snapshot_id"]].source_id, documents[r["snapshot_id"]].url): r
        for r in collection["documents"]
    }
    if len(refs) != len(collection["documents"]):
        raise ValueError("Duplicate selected document identity")
    bronze = Bronze(
        collection_manifest_sha256=collection_digest,
        collection_id=collection["run_id"],
        capture_kind=(
            "replay"
            if collection.get("replay_of_manifest_sha256")
            else "repair"
            if collection.get("repair_of_manifest_sha256")
            else "live"
        ),
        window=window,
        # Preserve the captured schema bytes; new optional Source fields must
        # not silently reinterpret an older immutable Bronze release.
        registry=collection["registry"],
        collector_version=collection["parser_version"],
        parser_version=collection["html_parser_version"],
    )
    records, qualities, seen_refs = [], [], set()
    source_checks = {c["source_id"]: c for c in collection["source_checks"]}
    if len(source_checks) != len(collection["source_checks"]) or set(source_checks) != {
        s.source_id for s in enabled
    }:
        raise ValueError("Source checks do not match the enabled registry")
    for source in enabled:
        check = source_checks[source.source_id]
        checkpoint = (
            json.loads(storage.read(check["checkpoint_sha256"]))
            if check.get("checkpoint_sha256")
            else {}
        )
        article_checks = {c["url"]: c for c in check.get("article_checks", [])}
        discoveries = {c["url"]: c for c in check.get("candidate_observations", [])}
        if len(article_checks) != len(check.get("article_checks", [])):
            raise ValueError("Duplicate article outcomes")
        candidates = set(check.get("candidate_urls", [])) | set(article_checks)
        # Listing pages remain the authority even if a malformed manifest omits
        # a candidate from its selected/deferred arrays.
        for page in check.get("listing_pages", []):
            candidates.update(page.get("candidate_urls", []))
        candidates.update(check.get("deferred_urls", []))
        rows = []
        for url in sorted(candidates):
            article_check = article_checks.get(
                url, {"url": url, "status": "deferred", "error_code": "candidate_unprocessed"}
            )
            if url not in article_checks:
                discovered = discoveries.get(url, {})
                displayed = source.resolved_html_rules.window_date_policy == "source_displayed"
                values = discovered.get("selection_dates" if displayed else "publication_dates", [])
                article_check = dict(
                    article_check,
                    **{
                        "selection_date" if displayed else "publication_date": values[0]
                        if len(values) == 1
                        else None,
                        "selection_date_evidence"
                        if displayed
                        else "publication_date_evidence": discovered.get(
                            "selection_date_evidence" if displayed else "publication_date_evidence",
                            [],
                        ),
                    },
                )
            reference = refs.get((source.source_id, url))
            row = _observation(
                storage, source, article_check, checkpoint, reference, documents, window
            )
            rows.append(row)
            if reference:
                seen_refs.add((source.source_id, url))
        # A native identity collision is never silently resolved by keeping the
        # last URL. It requires canonicalization in the connector.
        if len({r.document_id for r in rows}) != len(rows):
            raise ValueError("Multiple URLs have the same source document identity")
        counts = dict(sorted(Counter(r.outcome for r in rows).items()))
        issues = (
            list(check.get("errors", []))
            + list(check.get("listing_gaps", []))
            + list(check.get("item_errors", []))
        )
        issues.extend(
            {"url": r.url, "code": r.error_code or r.outcome}
            for r in rows
            if r.outcome in {"deferred", "failed", "needs_review"}
        )
        issues = [json.loads(v) for v in sorted({json.dumps(i, sort_keys=True) for i in issues})]
        complete = bool(
            check.get("coverage_complete") and check["status"] == "completed" and not issues
        )
        if not complete and not issues:
            issues.append({"code": "source_coverage_incomplete"})
        qualities.append(
            SourceQuality(
                source_id=source.source_id,
                organization=source.organization,
                url=source.url,
                scope=source.usage_policy,
                required=source.required,
                status=check["status"],
                coverage_complete=complete,
                outcomes=counts,
                candidates=len(rows),
                recovered_from_history=sum(r.recovered_from_history for r in rows),
                listing_pages=len(check.get("listing_pages", [])),
                http_attempts=len(checkpoint.get("http_attempts", [])),
                response_bytes=sum(
                    a.get("response_bytes", 0) for a in checkpoint.get("http_attempts", [])
                ),
                issues=issues,
            )
        )
        records.extend(rows)
    if set(refs) != seen_refs:
        raise ValueError("Selected documents are missing from the candidate ledger")
    silver = Silver(
        schema_version="healthlab-silver-2"
        if window.basis == "configured_source_calendar_date"
        else "healthlab-silver-1",
        bronze_sha256=digest(bronze),
        records=records,
        sources=qualities,
    )
    gold = Gold(
        silver_sha256=digest(silver),
        window=window,
        status="ready" if all(q.coverage_complete for q in qualities) else "partial",
        document_ids=[r.document_id for r in records if r.outcome == "included"],
    )
    dataset = Dataset(
        bronze_sha256=digest(bronze), silver_sha256=digest(silver), gold_sha256=digest(gold)
    )
    return dataset, bronze, silver, gold


def build_dataset(store: RunStore, collection_id: str) -> JsonObject:
    storage = collection_storage(store)
    with storage.state.lock():
        collection = storage.load(collection_id)
        frozen = storage.put(json_bytes(collection))
        verify_objects(storage, frozen)
        layers = assemble(storage, collection, frozen)
        for layer in reversed(layers):
            storage.put(encoded(layer))
        dataset_id = digest(layers[0])
        # Publication is a final atomic manifest save. Objects written before a
        # crash are harmless; rebuilding produces the same dataset ID.
        run = storage.new("signal_dataset", collection["question"])
        run.update(
            status="completed",
            stage="finished",
            finished_at=utc_now(),
            dataset_sha256=dataset_id,
            dataset_status=layers[3].status,
            collection_manifest_sha256=frozen,
            collection_id=collection_id,
        )
        storage.save(run)
    return summary(dataset_id, *layers[1:])


def read_dataset(store: RunStore, dataset_id: str) -> tuple[Dataset, Bronze, Silver, Gold]:
    storage = collection_storage(store)
    verify_objects(storage, dataset_id)
    dataset = Dataset.model_validate_json(storage.read(dataset_id))
    bronze = Bronze.model_validate_json(storage.read(dataset.bronze_sha256))
    collection = json.loads(storage.read(bronze.collection_manifest_sha256))
    # Recompute the version-1 contract from its frozen inputs. Hash validity by
    # itself cannot prove that Gold membership or coverage is correct.
    expected = assemble(storage, collection, bronze.collection_manifest_sha256)
    for actual, layer in zip(
        (dataset_id, dataset.bronze_sha256, dataset.silver_sha256, dataset.gold_sha256),
        expected,
        strict=True,
    ):
        if actual != digest(layer):
            raise IngestionError(
                "dataset_lineage", "Dataset layers do not match their frozen inputs"
            )
    return expected


def gold_records(store: RunStore, dataset_id: str, *, allow_partial=False) -> list[JsonObject]:
    _, _, silver, gold = read_dataset(store, dataset_id)
    if gold.status != "ready" and not allow_partial:
        raise IngestionError("dataset_partial", "Dataset has unresolved source coverage gaps")
    selected = set(gold.document_ids)
    return [r.model_dump(mode="json") for r in silver.records if r.document_id in selected]


def summary(dataset_id: str, bronze: Bronze, silver: Silver, gold: Gold) -> JsonObject:
    return {
        "dataset_id": dataset_id,
        "collection_id": bronze.collection_id,
        "status": gold.status,
        "window": gold.window.model_dump(mode="json"),
        "capture_kind": bronze.capture_kind,
        "documents": len(gold.document_ids),
        "sources": [q.model_dump(mode="json") for q in silver.sources],
        "coverage_basis": gold.coverage_basis,
        "independent_recall": None,
        "semantic_review": gold.semantic_review,
    }
