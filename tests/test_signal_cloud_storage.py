"""Cloud object adapter contracts using SDK exceptions, without cloud access."""

import hashlib

import pytest

pytest.importorskip("google.cloud.storage")
from google.api_core.exceptions import NotFound, PreconditionFailed

from healthlab.models import IngestionError
from healthlab.signal.cloud_storage import GCSObjectStore


class FakeBucket:
    def __init__(self):
        self.objects = {}
        self.operations = []

    def blob(self, name):
        bucket = self

        class Blob:
            def upload_from_string(self, content, **kwargs):
                bucket.operations.append(("upload", name, kwargs))
                assert kwargs["if_generation_match"] == 0
                if name in bucket.objects:
                    raise PreconditionFailed("Existing object")
                bucket.objects[name] = content

            def download_as_bytes(self, **kwargs):
                bucket.operations.append(("download", name, kwargs))
                if name not in bucket.objects:
                    raise NotFound("Missing")
                return bucket.objects[name]

        return Blob()


def test_gcs_create_only_upload_dedup_and_read_hash_verification():
    bucket = FakeBucket()
    store = GCSObjectStore(bucket)
    content = b"Immutable source snapshot"
    digest = hashlib.sha256(content).hexdigest()
    assert store.put(content) == digest
    assert store.put(content) == digest
    assert store.read(digest) == content
    assert list(bucket.objects) == ["healthlab/signal/objects/" + digest]
    bucket.objects["healthlab/signal/objects/" + digest] = b"corrupt"
    with pytest.raises(IngestionError) as caught:
        store.put(content)
    assert caught.value.code == "cache_corrupt"
    with pytest.raises(IngestionError) as caught:
        store.read(digest)
    assert caught.value.code == "cache_corrupt"


def test_gcs_missing_invalid_and_operational_errors_are_explicit():
    store = GCSObjectStore(FakeBucket())
    with pytest.raises(IngestionError) as caught:
        store.read("0" * 64)
    assert caught.value.code == "cache_missing"
    with pytest.raises(IngestionError) as caught:
        store.read("../outside")
    assert caught.value.code == "invalid_reference"

    class FailingBucket:
        def blob(self, name):
            raise RuntimeError("Synthetic cloud outage")

    with pytest.raises(IngestionError) as caught:
        GCSObjectStore(FailingBucket()).put(b"source")
    assert caught.value.code == "cloud_object_io"


def test_cloud_cli_does_not_fall_back_to_local_or_expose_dsn(tmp_path, monkeypatch, capsys):
    from healthlab.signal.__main__ import main

    monkeypatch.delenv("HEALTHLAB_SIGNAL_DATABASE_URL", raising=False)
    config = tmp_path / "cloud.json"
    config.write_text('{"backend":"gcs_postgres","bucket":"private-example-bucket"}')
    result = main(["--storage-config", str(config), "init-storage"])
    assert result == 1
    output = capsys.readouterr().out
    assert '"error_code": "cloud_configuration"' in output
    assert "postgresql://" not in output
    assert main(["--storage-config", str(config), "check-config"]) == 1
    assert '"error_code": "configuration_or_io"' in capsys.readouterr().out


def test_cloud_artifact_failure_leaves_collection_resumable_instead_of_marking_article_failed(
    tmp_path,
):
    from healthlab.signal.collection import collect
    from healthlab.signal.storage import CollectionStorage, LocalCollectionState
    from healthlab.store import RunStore
    from tests.test_signal_html_sources import article
    from tests.test_signal_recovery import DATES, LISTING, POLICY, client_for, ok, registry

    local = RunStore(tmp_path)

    class FailingObjects:
        def put(self, content):
            raise IngestionError("cloud_object_io", "Synthetic backend failure")

        def read(self, digest):
            return local.read(digest)

    with client_for(lambda r: ok(LISTING if r.url.path == "/" else article())) as client:
        with pytest.raises(IngestionError):
            collect(
                CollectionStorage(FailingObjects(), LocalCollectionState(local)),
                registry(),
                **DATES,
                rss_client=client,
                fetch_policy=POLICY,
            )
    run = local.load(next((tmp_path / "runs").iterdir()).name)
    assert run["status"] == "running" and not run["source_checks"][0].get("committed")
