"""Opt-in real PostgreSQL tests. Use only a disposable test database."""

import copy
import os
import uuid

import pytest

from healthlab.models import IngestionError
from healthlab.signal.backup import export_collection
from healthlab.signal.cloud_storage import PostgresCollectionState, import_bundle
from healthlab.signal.collection import collect, load_documents, resume_collection
from healthlab.signal.storage import CollectionStorage
from healthlab.store import RunStore
from tests.test_signal_html_sources import article
from tests.test_signal_recovery import (
    DATES,
    LISTING,
    POLICY,
    MemoryObjects,
    client_for,
    interrupt_after_first,
    ok,
    registry,
)

pytest.importorskip("psycopg")
DSN = os.environ.get("HEALTHLAB_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="Set HEALTHLAB_TEST_POSTGRES_DSN to a disposable database"
)


def state_for(namespace=None, location="gs://test-bucket/healthlab/signal"):
    state = PostgresCollectionState(DSN, namespace or "test_" + uuid.uuid4().hex, location)
    state.initialize()
    return state


def test_real_postgres_commit_rollback_shared_lock_and_binding():
    state = state_for()
    contender = PostgresCollectionState(DSN, state.namespace, state.object_location)
    with state.lock():
        run = state.new("signal_collection", {})
        run["stage"] = "before"
        state.commit(run, {"first": {"snapshot_id": "a" * 64}})
        with pytest.raises(IngestionError) as caught:
            with contender.lock():
                pytest.fail("Second writer must not acquire the namespace")
        assert caught.value.code == "collection_busy"
        modified = copy.deepcopy(run)
        modified["stage"] = "should-rollback"
        with pytest.raises(IngestionError):
            # NULL document key violates the real PG constraint after run UPDATE.
            state.commit(modified, {None: {"snapshot_id": "b" * 64}})
        assert state.load(run["run_id"])["stage"] == "before"
        assert state.load_index() == {"first": {"snapshot_id": "a" * 64}}
    with contender.lock():
        assert contender.load(run["run_id"])["stage"] == "before"
    with pytest.raises(IngestionError) as caught:
        state_for(state.namespace, "gs://different-bucket/objects")
    assert caught.value.code == "cloud_schema"


def test_real_postgres_collection_import_and_resume_keep_snapshots(tmp_path):
    source = RunStore(tmp_path / "source")
    interrupted, _ = interrupt_after_first(source)
    bundle = tmp_path / "bundle"
    export_collection(source, interrupted["run_id"], bundle)
    storage = CollectionStorage(MemoryObjects(), state_for())
    first = import_bundle(storage, bundle)
    assert first["verified"] and import_bundle(storage, bundle) == first
    requests = []

    def handle(request):
        requests.append(request.url.path)
        assert request.url.path == "/news/item/b"
        return ok(article())

    with client_for(handle) as client:
        resumed = resume_collection(storage, interrupted["run_id"], rss_client=client)
    assert requests == ["/news/item/b"] and resumed["status"] == "completed"
    assert len(load_documents(storage, resumed)) == 2
    with pytest.raises(IngestionError) as caught:
        import_bundle(storage, bundle)
    assert caught.value.code == "migration_conflict"


def test_real_postgres_namespace_isolation_and_normal_collection():
    objects = MemoryObjects()
    storage = CollectionStorage(objects, state_for())
    other = CollectionStorage(objects, state_for())
    with client_for(lambda r: ok(LISTING if r.url.path == "/" else article())) as client:
        run = collect(storage, registry(), **DATES, rss_client=client, fetch_policy=POLICY)
    assert run["status"] == "completed"
    with other.state.lock():
        assert other.state.load_index() == {}
        with pytest.raises(IngestionError):
            other.load(run["run_id"])


def test_lost_postgres_writer_connection_cannot_continue_unlocked():
    state = state_for()
    with pytest.raises(IngestionError):
        with state.lock():
            run = state.new("signal_collection", {})
            state.connection.close()
            state.save(run)
    contender = PostgresCollectionState(DSN, state.namespace, state.object_location)
    with contender.lock():
        assert contender.load(run["run_id"])["status"] == "running"


def test_real_postgres_dataset_history_and_portable_export(tmp_path):
    from healthlab.data.bundle import export_dataset, verify_export
    from healthlab.data.datasets import build_dataset, gold_records

    storage = CollectionStorage(MemoryObjects(), state_for())
    with client_for(lambda r: ok(LISTING if r.url.path == "/" else article())) as client:
        first = collect(
            storage,
            registry(),
            **DATES,
            rss_client=client,
            fetch_policy=POLICY,
            remember_candidates=True,
        )
    # The second listing drops one link. Shared PG history must recover it just
    # as the local filesystem adapter does, without relying on store.root.
    with client_for(
        lambda r: ok(b'<a href="/news/item/a">First</a>' if r.url.path == "/" else article())
    ) as client:
        second = collect(
            storage,
            registry(),
            **DATES,
            rss_client=client,
            fetch_policy=POLICY,
            remember_candidates=True,
        )
    assert set(storage.state.list_runs("signal_collection")) == {first["run_id"], second["run_id"]}
    result = build_dataset(storage, second["run_id"])
    assert result["status"] == "ready" and len(gold_records(storage, result["dataset_id"])) == 2
    assert result["sources"][0]["recovered_from_history"] == 1
    assert len(storage.state.list_runs("signal_dataset")) == 1
    export_dataset(storage, result["dataset_id"], tmp_path / "export")
    assert verify_export(tmp_path / "export")["verified"]


def test_real_postgres_silver_v2_preserves_displayed_date_through_replay_and_export(tmp_path):
    from healthlab.data.bundle import export_dataset, verify_export
    from healthlab.data.datasets import build_dataset, gold_records, read_dataset
    from healthlab.signal.collection import replay_collection
    from tests.test_data_pipeline import capture
    from tests.test_signal_discovery import HOST, article, source

    storage = CollectionStorage(MemoryObjects(), state_for())
    src = source()
    src.html_rules.window_date_policy = "source_displayed"
    content = article() + '<meta property="article:modified_time" content="2026-10-14">'
    run, _ = capture(
        storage,
        '<a href="/article/a">Report</a>',
        src=src,
        pages={HOST + "/article/a": content},
    )
    replay = replay_collection(storage, run["run_id"])
    result = build_dataset(storage, replay["run_id"])
    assert result["status"] == "ready"
    _, _, silver, _ = read_dataset(storage, result["dataset_id"])
    assert silver.schema_version == "healthlab-silver-2"
    row = gold_records(storage, result["dataset_id"])[0]
    assert row["publication_date"] is None
    assert row["selection_date"] == "2026-10-14"
    assert row["selection_date_basis"] == "article_modified"
    assert row["selection_date_evidence"][0]["raw_sha256"] == row["raw_sha256"]
    export_dataset(storage, result["dataset_id"], tmp_path / "v2-export")
    assert verify_export(tmp_path / "v2-export")["verified"]
