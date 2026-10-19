"""Failure/recovery contracts for collection, independent of factual evaluation."""

import copy
import hashlib
import json
import uuid
from contextlib import nullcontext
from datetime import date

import httpx
import pytest

from healthlab.models import MANIFEST_VERSION, IngestionError
from healthlab.pubmed import utc_now
from healthlab.signal.backup import export_collection, verify_bundle
from healthlab.signal.collection import (
    collect,
    collection_status,
    load_documents,
    resume_collection,
)
from healthlab.signal.contracts import Registry
from healthlab.signal.source_http import FetchFailure, FetchPolicy, HTMLFetcher
from healthlab.signal.storage import CollectionStorage, LocalCollectionState, atomic_json
from healthlab.store import RunStore
from tests.test_signal_html_sources import article, source

POLICY = FetchPolicy(request_interval=0, backoff_seconds=0)
DATES = {"mindate": date(2026, 10, 3), "maxdate": date(2026, 10, 10)}
LISTING = b'<a href="/news/item/a">First</a><a href="/news/item/b">Second</a>'


def registry():
    return Registry(schema_version="signal-registry-2", sources=[source(limit=5)])


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok(content):
    return httpx.Response(200, content=content, headers={"content-type": "text/html"})


def interrupt_after_first(store):
    requests = []

    def handle(request):
        requests.append(request.url.path)
        if request.url.path == "/news/item/b":
            raise KeyboardInterrupt("Synthetic process interruption")
        return ok(LISTING if request.url.path == "/" else article())

    with client_for(handle) as client, pytest.raises(KeyboardInterrupt):
        collect(store, registry(), **DATES, rss_client=client, fetch_policy=POLICY)
    runs = list((store.root / "runs").iterdir())
    run = store.load(runs[-1].name)
    assert run["status"] == "running"
    return run, requests


def test_resume_uses_frozen_listing_and_completed_articles_without_duplicate_documents(tmp_path):
    store = RunStore(tmp_path)
    run, requests = interrupt_after_first(store)
    assert requests == ["/", "/news/item/a", "/news/item/b"]
    status = collection_status(store, run["run_id"])
    assert status["sources"][0]["checkpointed_articles"] == 1
    assert status["attention_required"]
    new_requests = []

    def handle(request):
        new_requests.append(request.url.path)
        assert request.url.path == "/news/item/b"
        return ok(article())

    with client_for(handle) as client:
        resumed = resume_collection(store, run["run_id"], rss_client=client)
        repeated = resume_collection(store, run["run_id"], rss_client=client)
    assert new_requests == ["/news/item/b"]
    assert resumed == repeated
    assert resumed["status"] == "completed" and len(load_documents(store, resumed)) == 2
    assert len({r["document_id"] for r in resumed["documents"]}) == 2
    assert not collection_status(store, run["run_id"])["attention_required"]


def test_crash_after_response_persistence_reparses_without_refetch(tmp_path, monkeypatch):
    store = RunStore(tmp_path)
    from healthlab.signal.html_adapter import ConfiguredHTMLAdapter

    original = ConfiguredHTMLAdapter.parse_article

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt("After HTTP checkpoint, before parsing")

    monkeypatch.setattr(ConfiguredHTMLAdapter, "parse_article", interrupt)
    with client_for(lambda r: ok(LISTING if r.url.path == "/" else article())) as client:
        with pytest.raises(KeyboardInterrupt):
            collect(store, registry(), **DATES, rss_client=client, fetch_policy=POLICY)
    run_id = next((tmp_path / "runs").iterdir()).name
    monkeypatch.setattr(ConfiguredHTMLAdapter, "parse_article", original)
    requested = []

    def handle(request):
        requested.append(request.url.path)
        assert request.url.path == "/news/item/b"
        return ok(article())

    with client_for(handle) as client:
        run = resume_collection(store, run_id, rss_client=client)
    assert requested == ["/news/item/b"] and run["status"] == "completed"


@pytest.mark.parametrize("damage", ["raw", "checkpoint", "configuration", "parser"])
def test_resume_rejects_corrupt_or_incompatible_saved_data_before_network(tmp_path, damage):
    store = RunStore(tmp_path)
    run, _ = interrupt_after_first(store)
    checkpoint_hash = run["source_checks"][0]["checkpoint_sha256"]
    if damage == "raw":
        checkpoint = json.loads(store.read(checkpoint_hash))
        digest = next(iter(checkpoint["responses"].values()))["raw_sha256"]
        (tmp_path / "objects" / digest).write_bytes(b"corrupt")
    elif damage == "checkpoint":
        (tmp_path / "objects" / checkpoint_hash).unlink()
    elif damage == "configuration":
        run["question"]["maxdate"] = "2026-10-11"
        store.save(run)
    else:
        run["html_parser_version"] = "different-parser"
        store.save(run)
    with client_for(lambda r: pytest.fail("Network must remain unused")) as client:
        with pytest.raises(IngestionError):
            resume_collection(store, run["run_id"], rss_client=client)


def test_finished_partial_collection_is_not_mutated_by_resume(tmp_path):
    store = RunStore(tmp_path)
    with client_for(lambda r: ok(LISTING) if r.url.path == "/" else httpx.Response(404)) as client:
        run = collect(store, registry(), **DATES, rss_client=client, fetch_policy=POLICY)
    before = (tmp_path / "runs" / run["run_id"] / "manifest.json").read_bytes()
    with client_for(lambda r: pytest.fail("Finished runs need a new collection")) as client:
        assert resume_collection(store, run["run_id"], rss_client=client) == run
    assert (tmp_path / "runs" / run["run_id"] / "manifest.json").read_bytes() == before


def test_pending_run_index_commit_recovers_before_next_reader(tmp_path, monkeypatch):
    store = RunStore(tmp_path)
    state = LocalCollectionState(store)
    with state.lock():
        run = state.new("signal_collection", {})
        run["documents"] = [{"marker": "committed"}]
        original_save = state.save
        monkeypatch.setattr(
            state, "save", lambda r: (_ for _ in ()).throw(OSError("Disk interrupted"))
        )
        with pytest.raises(OSError):
            state.commit(run, {"document": {"marker": "same-transaction"}})
        assert (tmp_path / "collection-pending.json").exists()
        monkeypatch.setattr(state, "save", original_save)
    recovered = LocalCollectionState(store)
    with recovered.lock():
        assert recovered.load(run["run_id"])["documents"] == [{"marker": "committed"}]
        assert recovered.load_index() == {"document": {"marker": "same-transaction"}}
        assert not (tmp_path / "collection-pending.json").exists()


def test_corrupt_journal_blocks_state_access_instead_of_discarding_it(tmp_path):
    state = LocalCollectionState(RunStore(tmp_path))
    atomic_json(tmp_path / "collection-pending.json", {"bundle_sha256": "0" * 64})
    with pytest.raises(IngestionError):
        with state.lock():
            pytest.fail("Must not acquire usable state with missing journal object")
    assert (tmp_path / "collection-pending.json").exists()


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_status_retries_are_bounded_and_audited(tmp_path, status):
    count, waits = [], []

    def handle(request):
        count.append(str(request.url))
        return httpx.Response(status) if len(count) < 3 else ok(b"saved HTML")

    checkpoint = {"responses": {}, "http_attempts": []}
    saved = []
    with client_for(handle) as client:
        fetcher = HTMLFetcher(client, RunStore(tmp_path), FetchPolicy(), sleep=waits.append)
        raw, digest = fetcher.fetch(
            "https://www.who.int/a", checkpoint, lambda: saved.append(copy.deepcopy(checkpoint))
        )
    assert raw == b"saved HTML" and hashlib.sha256(raw).hexdigest() == digest
    assert len(count) == 3 and len(saved) == 6
    assert [a["status"] for a in checkpoint["http_attempts"]] == ["failed", "failed", "completed"]
    assert waits == [0.25, 0.5, 0.25, 1.0, 0.25]


@pytest.mark.parametrize("status", [301, 401, 403, 404])
def test_permanent_http_errors_are_not_retried(tmp_path, status):
    checkpoint = {"responses": {}, "http_attempts": []}
    with client_for(lambda r: httpx.Response(status)) as client:
        with pytest.raises(FetchFailure):
            HTMLFetcher(client, RunStore(tmp_path), POLICY).fetch(
                "https://www.who.int/a", checkpoint, lambda: None
            )
    assert len(checkpoint["http_attempts"]) == 1


def test_transport_exhaustion_and_interruption_do_not_reset_attempt_budget(tmp_path):
    checkpoint = {
        "responses": {},
        "http_attempts": [{"url": "https://www.who.int/a", "attempt": 1, "status": "started"}],
    }
    calls = []

    def handle(request):
        calls.append(1)
        raise httpx.ReadTimeout("Synthetic timeout")

    with client_for(handle) as client:
        fetcher = HTMLFetcher(client, RunStore(tmp_path), POLICY)
        for _ in range(2):
            with pytest.raises(FetchFailure):
                fetcher.fetch("https://www.who.int/a", checkpoint, lambda: None)
    assert len(calls) == 2 and len(checkpoint["http_attempts"]) == 3


def test_long_retry_after_is_not_shortened_to_retry_early(tmp_path):
    checkpoint = {"responses": {}, "http_attempts": []}
    waits = []
    with client_for(lambda r: httpx.Response(429, headers={"retry-after": "3600"})) as client:
        fetcher = HTMLFetcher(client, RunStore(tmp_path), FetchPolicy(), sleep=waits.append)
        with pytest.raises(FetchFailure):
            fetcher.fetch("https://www.who.int/a", checkpoint, lambda: None)
    assert len(checkpoint["http_attempts"]) == 1 and waits == [0.25]


class MemoryObjects:
    def __init__(self):
        self.objects = {}

    def put(self, content):
        digest = hashlib.sha256(content).hexdigest()
        self.objects.setdefault(digest, content)
        return digest

    def read(self, digest):
        content = self.objects[digest]
        assert hashlib.sha256(content).hexdigest() == digest
        return content


class MemoryState:
    def __init__(self):
        self.runs, self.index = {}, {}

    def lock(self):
        return nullcontext()

    def new(self, mode, question):
        run = {
            "run_id": uuid.uuid4().hex,
            "manifest_version": MANIFEST_VERSION,
            "mode": mode,
            "question": question,
            "created_at": utc_now(),
            "status": "running",
            "requests": [],
            "documents": [],
            "errors": [],
            "warnings": [],
        }
        self.save(run)
        return run

    def save(self, run):
        self.runs[run["run_id"]] = copy.deepcopy(run)

    def load(self, run_id):
        return copy.deepcopy(self.runs[run_id])

    def load_index(self):
        return copy.deepcopy(self.index)

    def commit(self, run, index):
        self.save(run)
        self.index = copy.deepcopy(index)


def test_html_collection_runs_without_a_filesystem_storage_attribute():
    storage = CollectionStorage(MemoryObjects(), MemoryState())
    assert not hasattr(storage, "root")
    with client_for(lambda r: ok(LISTING if r.url.path == "/" else article())) as client:
        run = collect(storage, registry(), **DATES, rss_client=client, fetch_policy=POLICY)
    assert run["status"] == "completed" and len(load_documents(storage, run)) == 2
    assert resume_collection(storage, run["run_id"]) == run


def test_export_is_self_contained_hash_verified_and_usable_after_original_removed(tmp_path):
    original = tmp_path / "original"
    store = RunStore(original)
    interrupted, _ = interrupt_after_first(store)
    destination = tmp_path / "export"
    exported = export_collection(store, interrupted["run_id"], destination)
    assert exported["verified"] and verify_bundle(destination)["verified"]
    original.rename(tmp_path / "unavailable-original")
    moved_store = RunStore(destination)
    with client_for(
        lambda r: (
            ok(article())
            if r.url.path == "/news/item/b"
            else pytest.fail("Should use saved responses")
        )
    ) as client:
        resumed = resume_collection(moved_store, interrupted["run_id"], rss_client=client)
    assert resumed["status"] == "completed" and len(load_documents(moved_store, resumed)) == 2
    # Bundle inventory described the original backup snapshot, not subsequent writes.
    with pytest.raises(IngestionError):
        verify_bundle(destination)


def test_export_missing_object_fails_without_publishing_destination(tmp_path):
    store = RunStore(tmp_path / "source")
    interrupted, _ = interrupt_after_first(store)
    (store.root / "objects" / interrupted["source_checks"][0]["checkpoint_sha256"]).unlink()
    destination = tmp_path / "backup"
    with pytest.raises(IngestionError):
        export_collection(store, interrupted["run_id"], destination)
    assert not destination.exists()


def test_bundle_detects_corrupted_artifact(tmp_path):
    store = RunStore(tmp_path / "source")
    interrupted, _ = interrupt_after_first(store)
    destination = tmp_path / "backup"
    export_collection(store, interrupted["run_id"], destination)
    digest = json.loads((destination / "bundle.json").read_bytes())["objects"][0]
    (destination / "objects" / digest).write_bytes(b"damaged")
    with pytest.raises(IngestionError):
        verify_bundle(destination)


def test_resume_does_not_rollback_newer_document_index(tmp_path):
    store = RunStore(tmp_path)
    old, _ = interrupt_after_first(store)
    newer_article = article().replace(b"Healthy ageing", b"Updated ageing")
    # Change actual article text so that both runs have distinct snapshots.
    newer_article = newer_article.replace(b"<h1>", b"<h1>New version ")
    with client_for(lambda r: ok(LISTING if r.url.path == "/" else newer_article)) as client:
        newer = collect(store, registry(), **DATES, rss_client=client, fetch_policy=POLICY)
    with client_for(lambda r: ok(article())) as client:
        resumed = resume_collection(store, old["run_id"], rss_client=client)
    state = LocalCollectionState(store)
    with state.lock():
        index = state.load_index()
    assert resumed["status"] == "completed"
    assert {ref["snapshot_id"] for ref in index.values()} == {
        ref["snapshot_id"] for ref in newer["documents"]
    }
    assert all(ref["run_id"] == newer["run_id"] for ref in index.values())


def test_journal_recovers_after_manifest_written_but_index_write_failed(tmp_path, monkeypatch):
    from healthlab.signal import storage as storage_module

    store = RunStore(tmp_path)
    state = LocalCollectionState(store)
    with state.lock():
        run = state.new("signal_collection", {})
        run["stage"] = "published"
        original_write = storage_module.atomic_json

        def fail_index(path, value):
            if path.name == "signal-index.json":
                raise OSError("Synthetic crash between writes")
            return original_write(path, value)

        monkeypatch.setattr(storage_module, "atomic_json", fail_index)
        with pytest.raises(OSError):
            state.commit(run, {"key": {"snapshot_id": "a" * 64}})
        assert store.load(run["run_id"])["stage"] == "published"
        assert not (tmp_path / "signal-index.json").exists()
        monkeypatch.setattr(storage_module, "atomic_json", original_write)
    with state.lock():
        assert state.load_index() == {"key": {"snapshot_id": "a" * 64}}
        assert not (tmp_path / "collection-pending.json").exists()


def test_retry_after_survives_interruption_before_result_checkpoint(tmp_path):
    checkpoint = {"responses": {}, "http_attempts": []}

    def save():
        if checkpoint["http_attempts"][-1]["status"] == "failed":
            raise KeyboardInterrupt("After retry audit was persisted")

    with client_for(lambda r: httpx.Response(429, headers={"retry-after": "3600"})) as client:
        with pytest.raises(KeyboardInterrupt):
            HTMLFetcher(client, RunStore(tmp_path), POLICY).fetch(
                "https://www.who.int/a", checkpoint, save
            )
    with client_for(lambda r: pytest.fail("Must respect saved Retry-After")) as client:
        with pytest.raises(FetchFailure):
            HTMLFetcher(client, RunStore(tmp_path), POLICY).fetch(
                "https://www.who.int/a", checkpoint, lambda: None
            )
    assert len(checkpoint["http_attempts"]) == 1


def test_export_keeps_previous_snapshots_and_does_not_overwrite_destination(tmp_path):
    store = RunStore(tmp_path / "source")
    with client_for(lambda r: ok(LISTING if r.url.path == "/" else article())) as client:
        first = collect(store, registry(), **DATES, rss_client=client, fetch_policy=POLICY)
    with client_for(
        lambda r: ok(LISTING if r.url.path == "/" else article().replace(b"<h1>", b"<h1>Updated "))
    ) as client:
        second = collect(store, registry(), **DATES, rss_client=client, fetch_policy=POLICY)
    destination = tmp_path / "backup"
    export_collection(store, second["run_id"], destination)
    assert verify_bundle(destination)["verified"]
    copied_store = RunStore(destination)
    for ref in first["documents"]:
        assert copied_store.read(ref["snapshot_id"]) == store.read(ref["snapshot_id"])
    inventory_before = (destination / "bundle.json").read_bytes()
    with pytest.raises(ValueError):
        export_collection(store, second["run_id"], destination)
    assert (destination / "bundle.json").read_bytes() == inventory_before


def test_collection_cli_status_export_verify_and_resume_are_offline(tmp_path, capsys):
    from healthlab.signal.__main__ import main

    store = RunStore(tmp_path / "store")
    with client_for(lambda r: ok(LISTING if r.url.path == "/" else article())) as client:
        run = collect(store, registry(), **DATES, rss_client=client, fetch_policy=POLICY)
    common = ["--store", str(store.root)]
    assert main([*common, "collection-status", run["run_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    destination = tmp_path / "bundle"
    assert main([*common, "export-collection", run["run_id"], "--out", str(destination)]) == 0
    assert json.loads(capsys.readouterr().out)["verified"]
    assert main([*common, "verify-bundle", str(destination)]) == 0
    assert json.loads(capsys.readouterr().out)["verified"]
    assert main([*common, "resume-collection", run["run_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == run["run_id"]
