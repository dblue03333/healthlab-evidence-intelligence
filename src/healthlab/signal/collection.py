"""Collect RSS summaries and reuse bounded PubMed ingestion. Persist source failures."""

import hashlib
import json
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import date
from html.parser import HTMLParser

import httpx

from healthlab.models import IngestionError, Question
from healthlab.pipeline import ingest
from healthlab.pubmed import utc_now
from healthlab.signal.contracts import Registry, SourceDocument
from healthlab.signal.html_adapter import PARSER_VERSION as HTML_PARSER_VERSION
from healthlab.signal.html_sources import collect_html
from healthlab.signal.source_http import FetchPolicy, HTMLFetcher, source_client
from healthlab.signal.storage import collection_storage
from healthlab.store import json_bytes

COLLECTION_VERSION = "signal-collection-9"


@contextmanager
def pilot_lock(store):
    # Newsletter and collection share the same local lock and journal recovery.
    with collection_storage(store).state.lock():
        yield


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"br", "p", "div"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value):
    parser = PlainText()
    parser.feed(value)
    return " ".join("".join(parser.parts).split())


def parse_rss(raw, source, raw_hash):
    if len(raw) > 2_000_000 or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise IngestionError("rss_invalid", "Feed exceeds limits or contains a DTD")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise IngestionError("rss_invalid", "RSS XML could not be parsed") from None
    if root.tag != "rss" or root.find("channel") is None:
        raise IngestionError("rss_unsupported", "This connector requires RSS 2.0")
    documents, errors, seen = [], [], set()
    items = root.findall("./channel/item")
    for position, item in enumerate(items[: source.max_documents]):
        try:
            url = (item.findtext("link") or "").strip()
            title = plain_text(item.findtext("title") or "")
            summary = plain_text(item.findtext("description") or "")
            doc = SourceDocument(
                document_id="rss:" + hashlib.sha256(url.encode()).hexdigest(),
                source_id=source.source_id,
                url=url,
                title=title,
                publication_date=item.findtext("pubDate"),
                geography=source.geography,
                text=title + ("\n\n" + summary if summary else ""),
                coverage="rss_summary_only",
                raw_sha256=raw_hash,
            )
            if doc.document_id not in seen:
                documents.append(doc)
                seen.add(doc.document_id)
        except ValueError:
            errors.append({"position": position, "code": "rss_item_invalid"})
    return documents, {
        "returned_items": len(items),
        "selected_positions": min(len(items), source.max_documents),
        "limited": len(items) > source.max_documents,
        "item_errors": errors,
        "coverage": "Current feed, bounded sample; not complete website coverage",
    }


def _snapshot(store, document):
    # Batch bytes change even if this particular article has not changed.
    # Use article text+metadata for change detection while retaining original raw provenance.
    body = document.model_dump()
    identity = dict(body)
    identity.pop("raw_sha256")
    content_hash = hashlib.sha256(json_bytes(identity)).hexdigest()
    snapshot_id = store.put(json_bytes(body))
    return {
        "document_id": document.document_id,
        "source_id": document.source_id,
        "snapshot_id": snapshot_id,
        "content_hash": content_hash,
    }


def _configuration(run):
    return hashlib.sha256(
        json_bytes(
            {
                "registry": run["registry"],
                "question": run["question"],
                "fetch_policy": run["fetch_policy"],
                "version": run["parser_version"],
                "html_parser_version": run["html_parser_version"],
                "candidate_history": run.get("candidate_history", {}),
                "replay_of_manifest_sha256": run.get("replay_of_manifest_sha256"),
                "observed_at": run.get("observed_at"),
                **(
                    {"repair_of_manifest_sha256": run["repair_of_manifest_sha256"]}
                    if run.get("repair_of_manifest_sha256")
                    else {}
                ),
            }
        )
    ).hexdigest()


def collect(
    store,
    registry,
    *,
    mindate,
    maxdate,
    rss_client=None,
    pubmed_client=None,
    fetch_policy=None,
    on_started=None,
    remember_candidates=False,
    operation_id=None,
):
    if mindate > maxdate:
        raise ValueError("mindate must not be later than maxdate")
    if remember_candidates and any(s.kind != "html" for s in registry.sources if s.enabled):
        raise ValueError("Candidate history supports HTML/API sources only")
    storage = collection_storage(store)
    owned = rss_client is None
    rss_client = rss_client or source_client()
    try:
        with storage.state.lock():
            history = {}
            if remember_candidates:
                from healthlab.signal.history import candidate_history

                history = candidate_history(storage, registry, mindate, maxdate)
            run = storage.new(
                "signal_collection", {"mindate": str(mindate), "maxdate": str(maxdate)}
            )
            run.update(
                source_checks=[],
                registry=registry.model_dump(),
                stage="collection",
                parser_version=COLLECTION_VERSION,
                html_parser_version=HTML_PARSER_VERSION,
                fetch_policy=(fetch_policy or FetchPolicy()).as_dict(),
                candidate_history=history,
            )
            if operation_id is not None:
                run["operation_id"] = operation_id
            run["configuration_hash"] = _configuration(run)
            storage.save(run)
            if on_started:
                on_started(run["run_id"])
            return _collect_locked(
                storage, store, run, registry, mindate, maxdate, rss_client, pubmed_client
            )
    finally:
        if owned:
            rss_client.close()


def resume_collection(store, run_id, *, rss_client=None):
    """Resume interrupted HTML collection only; finished collections are immutable.

    The listing and successful responses are frozen. Start a new collect command
    for fresh discovery, a changed configuration, or a finished partial run.
    """
    storage = collection_storage(store)
    with storage.state.lock():
        run = storage.load(run_id)
        if (
            run.get("mode") != "signal_collection"
            or run.get("parser_version") != COLLECTION_VERSION
        ):
            raise IngestionError("collection_resume_version", "Unsupported collection version")
        if run.get("configuration_hash") != _configuration(run):
            raise IngestionError("collection_resume_configuration", "Saved configuration differs")
        if run.get("html_parser_version") != HTML_PARSER_VERSION:
            raise IngestionError("collection_resume_parser", "HTML parser version differs")
        registry = Registry.model_validate(run["registry"])
        if any(s.kind != "html" for s in registry.sources if s.enabled):
            raise ValueError("Resume currently supports HTML collections only")
        # Include historical discovery evidence, not just this run's responses,
        # before permitting any new request after an interruption.
        from healthlab.signal.backup import object_references, references

        pending, verified = list(references(run)), set()
        while pending:
            digest = pending.pop()
            if digest not in verified:
                pending.extend(object_references(storage.read(digest)))
                verified.add(digest)
        load_documents(storage, run)
        if run["status"] != "running":
            return run
        owned = rss_client is None and not run.get("replay_of_manifest_sha256")
        rss_client = source_client() if owned else rss_client
        try:
            return _collect_locked(
                storage,
                store,
                run,
                registry,
                date.fromisoformat(run["question"]["mindate"]),
                date.fromisoformat(run["question"]["maxdate"]),
                rss_client,
                None,
            )
        finally:
            if owned:
                rss_client.close()


def _collect_locked(
    store, legacy_store, run, registry, mindate, maxdate, rss_client, pubmed_client
):
    index = store.state.load_index()
    for source in registry.sources:
        if not source.enabled:
            continue
        check = next((c for c in run["source_checks"] if c["source_id"] == source.source_id), None)
        if check and check.get("committed"):
            continue
        if check is None:
            check = {
                "source_id": source.source_id,
                "required": source.required,
                "checked_at": utc_now(),
                "status": "running",
                "errors": [],
            }
            run["source_checks"].append(check)
        store.save(run)
        documents = []
        try:
            if source.kind == "rss":
                # No redirects or arbitrarily large response bodies.
                with rss_client.stream("GET", source.url) as response:
                    if response.status_code != 200:
                        raise IngestionError("rss_http", "RSS returned a non-200 status")
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 2_000_000:
                            raise IngestionError("rss_limit", "Feed exceeds 2 MB")
                        chunks.append(chunk)
                raw = b"".join(chunks)
                raw_hash = store.put(raw)
                check["raw_sha256"] = raw_hash
                documents, metadata = parse_rss(raw, source, raw_hash)
                check.update(metadata)
                check["status"] = "partial" if metadata["item_errors"] else "completed"
            elif source.kind == "html":
                fetcher = HTMLFetcher(
                    rss_client,
                    store,
                    FetchPolicy(**run["fetch_policy"]),
                    max_bytes=source.html_max_response_bytes,
                    cache_only=bool(run.get("replay_of_manifest_sha256")),
                )
                checkpoint = (
                    json.loads(store.read(check["checkpoint_sha256"]))
                    if check.get("checkpoint_sha256")
                    else None
                )

                def save_checkpoint(value):
                    check["checkpoint_sha256"] = store.put(json_bytes(value))
                    store.save(run)

                documents, metadata = collect_html(
                    store,
                    source,
                    rss_client,
                    mindate=mindate,
                    maxdate=maxdate,
                    checkpoint=checkpoint,
                    on_checkpoint=save_checkpoint,
                    fetcher=fetcher,
                    historical_candidates=run.get("candidate_history", {}).get(
                        source.source_id, []
                    ),
                )
                check.update(metadata)
                check["status"] = (
                    "partial"
                    if metadata["item_errors"] or not metadata["coverage_complete"]
                    else "completed"
                )
            elif source.kind == "pubmed":
                if pubmed_client is None:
                    raise IngestionError("pubmed_config", "PubMed client is not configured")
                parent = ingest(
                    legacy_store,
                    Question(
                        raw_question=source.query,
                        query=source.query,
                        sort="pub_date",
                        mindate=mindate,
                        maxdate=maxdate,
                    ),
                    pubmed_client,
                )
                check.update(
                    status=parent["status"],
                    ingestion_run_id=parent["run_id"],
                    errors=parent["errors"],
                    coverage="Top five PubMed records in publication window; not exhaustive",
                    limited=parent.get("counts", {}).get("matched", 0) > 5,
                )
                for reference in parent["documents"]:
                    snapshot = json.loads(store.read(reference["snapshot_id"]))
                    doc = snapshot["document"]
                    text = "\n\n".join(
                        [doc["title"] or ""] + [s["text"] for s in doc["abstract_sections"]]
                    ).strip()
                    if not doc["has_abstract"]:
                        check.setdefault("missing_abstracts", []).append(reference["pmid"])
                        continue
                    documents.append(
                        SourceDocument(
                            document_id=doc["document_id"],
                            source_id=source.source_id,
                            url=f"https://pubmed.ncbi.nlm.nih.gov/{reference['pmid']}/",
                            title=doc["title"] or "Untitled PubMed record",
                            publication_date=json.dumps(doc["pub_date"]),
                            geography=source.geography,
                            text=text,
                            coverage="abstract_only",
                            raw_sha256=reference["raw_record_sha256"],
                        )
                    )
            else:
                raise IngestionError("manual_source", "Manual source needs an audited import")
            for document in documents:
                reference = _snapshot(store, document)
                key = source.source_id + ":" + document.document_id
                indexed = index.get(key)
                observation = next(
                    (c for c in check.get("article_checks", []) if c["url"] == document.url), {}
                )
                observed_at = observation.get(
                    "observed_at", run.get("observed_at", run["created_at"])
                )
                # The run's creation time is its writer generation. Finishing
                # an older interrupted run must not supersede a newer live run,
                # even when its remaining HTTP response arrives later.
                observed_at = min(observed_at, run["created_at"])
                previous = (
                    indexed
                    if not indexed or indexed.get("collected_at", "") <= observed_at
                    else None
                )
                changed = previous is None or previous["content_hash"] != reference["content_hash"]
                if not changed:
                    # Keep the first snapshot's raw lineage for identical document content.
                    previous_document = SourceDocument.model_validate_json(
                        store.read(previous["snapshot_id"])
                    )
                    store.read(previous_document.raw_sha256)
                    reference["snapshot_id"] = previous["snapshot_id"]
                reference["change"] = (
                    "new" if previous is None else ("updated" if changed else "unchanged")
                )
                reference["previous_snapshot_id"] = previous["snapshot_id"] if previous else None
                run["documents"].append(reference)
                # Resuming an older run must not roll back a newer observation.
                if not indexed or indexed.get("collected_at", "") <= observed_at:
                    index[key] = reference | {
                        "collected_at": observed_at,
                        "run_id": run["run_id"],
                    }
        except (httpx.HTTPError, IngestionError, ValueError) as exc:
            if isinstance(exc, IngestionError) and exc.code in {
                "cache_corrupt",
                "cache_missing",
                "invalid_reference",
                "cloud_object_io",
                "cloud_state_io",
                "invalid_run",
            }:
                raise
            # Do not surface raw HTTP exception URLs/configuration values.
            check["status"] = "failed"
            check["errors"].append(
                {"code": exc.code if isinstance(exc, IngestionError) else "source_error"}
            )
        check["committed"] = True
        store.state.commit(run, index)
    checks = run["source_checks"]
    run["status"] = (
        "completed"
        if checks and all(c["status"] == "completed" for c in checks)
        else "partial"
        if run["documents"]
        else "failed"
    )
    run.update(stage="finished", finished_at=utc_now())
    store.save(run)
    return run


def replay_collection(store, run_id, *, registry=None, on_started=None):
    return _reprocess_collection(store, run_id, registry=registry, on_started=on_started)


def repair_collection(
    store, run_id, *, registry=None, on_started=None, rss_client=None, operation_id=None
):
    """Explicit new run: reuse successes, fetch cache misses with a new bounded budget."""
    return _reprocess_collection(
        store,
        run_id,
        registry=registry,
        on_started=on_started,
        repair=True,
        rss_client=rss_client,
        operation_id=operation_id,
    )


def _reprocess_collection(
    store,
    run_id,
    *,
    registry=None,
    on_started=None,
    repair=False,
    rss_client=None,
    operation_id=None,
):
    """Reparse a frozen capture into a NEW collection. Cache misses remain gaps.

    The old manifest and source responses are immutable. This deliberately uses
    the current parser, with fresh result checkpoints, and makes no HTTP client.
    """
    from healthlab.signal.backup import object_references, references
    from healthlab.signal.html_sources import checkpoint_identity

    storage = collection_storage(store)
    with storage.state.lock():
        previous = storage.load(run_id)
        if previous.get("mode") != "signal_collection" or previous["status"] == "running":
            raise ValueError("Replay requires a finished collection")
        registry = registry or Registry.model_validate(previous["registry"])
        if any(s.kind != "html" for s in registry.sources if s.enabled):
            raise ValueError("Replay supports HTML/API sources only")
        pending, verified = list(references(previous)), set()
        while pending:
            digest = pending.pop()
            if digest not in verified:
                pending.extend(object_references(storage.read(digest)))
                verified.add(digest)
        mindate, maxdate = (
            date.fromisoformat(previous["question"][k]) for k in ("mindate", "maxdate")
        )
        run = storage.new("signal_collection", previous["question"])
        run.update(
            source_checks=[],
            registry=registry.model_dump(),
            stage="collection",
            parser_version=COLLECTION_VERSION,
            html_parser_version=HTML_PARSER_VERSION,
            fetch_policy=previous["fetch_policy"],
            candidate_history=previous.get("candidate_history", {}),
            observed_at=previous.get("observed_at", previous["created_at"]),
        )
        if operation_id is not None:
            run["operation_id"] = operation_id
        run["repair_of_manifest_sha256" if repair else "replay_of_manifest_sha256"] = storage.put(
            json_bytes(previous)
        )
        for source in registry.sources:
            if not source.enabled:
                continue
            check = next(
                (c for c in previous["source_checks"] if c["source_id"] == source.source_id), {}
            )
            checkpoint = (
                json.loads(storage.read(check["checkpoint_sha256"]))
                if check.get("checkpoint_sha256")
                else {}
            )
            if repair:
                # Explicit repair grants a new bounded session only for an
                # unfinished public WebForms inventory. Completed captures replay.
                if not checkpoint.get("hcm_search_state", {}).get("completed"):
                    checkpoint.pop("hcm_search_state", None)
                gates = dict(checkpoint.get("retry_gates", {}))
                for attempt in checkpoint.get("http_attempts", []):
                    if attempt.get("retry_not_before"):
                        gates[attempt.get("request_key", attempt["url"])] = {
                            "retry_not_before": attempt["retry_not_before"]
                        }
                checkpoint["retry_gates"] = gates
                checkpoint["http_attempts"] = []
            for response in checkpoint.get("responses", {}).values():
                response.setdefault("captured_at", run["observed_at"])
            checkpoint.update(
                version="html-checkpoint-1",
                identity=checkpoint_identity(source, mindate, maxdate),
                results={},
                responses=checkpoint.get("responses", {}),
                http_attempts=checkpoint.get("http_attempts", []),
            )
            run["source_checks"].append(
                {
                    "source_id": source.source_id,
                    "required": source.required,
                    "checked_at": utc_now(),
                    "status": "running",
                    "errors": [],
                    "checkpoint_sha256": storage.put(json_bytes(checkpoint)),
                }
            )
        run["configuration_hash"] = _configuration(run)
        storage.save(run)
        if on_started:
            on_started(run["run_id"])
        owned = repair and rss_client is None
        client = source_client() if owned else rss_client
        try:
            return _collect_locked(storage, store, run, registry, mindate, maxdate, client, None)
        finally:
            if owned:
                client.close()


def collection_status(store, run_id):
    storage = collection_storage(store)
    # Run saves are atomic snapshots. Do not block behind the collection writer's
    # long-lived lock; this report does not read or claim a transaction with index.
    run = storage.load(run_id)
    if run.get("mode") != "signal_collection":
        raise ValueError("Expected collection run")
    sources = []
    for check in run["source_checks"]:
        checkpoint = (
            json.loads(storage.read(check["checkpoint_sha256"]))
            if check.get("checkpoint_sha256")
            else {}
        )
        sources.append(
            {
                "source_id": check["source_id"],
                "status": check["status"],
                "required": check["required"],
                "counts": check.get("counts", {}),
                "checkpointed_articles": len(checkpoint.get("results", {})),
                "http_attempts": len(checkpoint.get("http_attempts", [])),
                "deferred_urls": check.get("deferred_urls", []),
                "errors": check.get("errors", [])
                + check.get("listing_gaps", [])
                + check.get("item_errors", []),
                "deferred_listing_urls": check.get("deferred_listing_urls", []),
                "historical_candidates_revisited": check.get("historical_candidates_revisited", []),
                "coverage_complete": check.get("coverage_complete", False),
            }
        )
    return {
        "run_id": run_id,
        "status": run["status"],
        "stage": run["stage"],
        "documents": len(run["documents"]),
        "sources": sources,
        "resume_supported": run.get("parser_version") == COLLECTION_VERSION
        and all(s["kind"] == "html" for s in run["registry"]["sources"] if s["enabled"]),
        "attention_required": run["status"] != "completed",
    }


def load_documents(store, collection):
    if collection.get("mode") != "signal_collection":
        raise ValueError("Expected a Signal collection run")
    documents = {}
    for reference in collection["documents"]:
        document = SourceDocument.model_validate_json(store.read(reference["snapshot_id"]))
        store.read(document.raw_sha256)
        if (
            document.document_id != reference["document_id"]
            or document.source_id != reference["source_id"]
        ):
            raise ValueError("Document lineage mismatch")
        documents[reference["snapshot_id"]] = document
    return documents
