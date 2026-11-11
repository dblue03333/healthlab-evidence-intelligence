"""Failure evidence, portable recovery and source boundary acceptance."""

import json
from pathlib import Path

import httpx
import pytest

from healthlab.data import backup, power
from healthlab.signal.backup import export_collection, verify_bundle
from healthlab.signal.source_http import FetchFailure, FetchPolicy, HTMLFetcher
from healthlab.store import RunStore
from tests.test_data_pilot import execute, transport


def test_failure_body_is_bounded_exported_and_never_a_success(tmp_path):
    store = RunStore(tmp_path / "store")
    checkpoint = {"responses": {}, "http_attempts": []}
    snapshots = []
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(503, content=b"x" * 200_000))
    ) as client:
        fetcher = HTMLFetcher(client, store, FetchPolicy(max_attempts=1, request_interval=0))
        with pytest.raises(FetchFailure):
            fetcher.fetch(
                "https://example.org/", checkpoint, lambda: snapshots.append(json.dumps(checkpoint))
            )
    event = checkpoint["http_attempts"][0]
    assert not checkpoint["responses"]
    assert len(store.read(event["diagnostic_sha256"])) == 65_536
    assert event["diagnostic_may_be_truncated"] and event["status"] == "failed"
    assert event["timing"]["monotonic_seconds"] >= 0
    assert json.loads(snapshots[-1])["http_attempts"][0] == event


def test_unknown_failure_preserves_frames_without_secrets_and_consumes_budget(tmp_path):
    checkpoint = {"responses": {}, "http_attempts": []}

    def fail(_):
        raise RuntimeError("secret-account-token")

    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        fetcher = HTMLFetcher(
            client, RunStore(tmp_path), FetchPolicy(max_attempts=1, request_interval=0)
        )
        with pytest.raises(RuntimeError):
            fetcher.fetch("https://example.org/", checkpoint, lambda: None)
        event = checkpoint["http_attempts"][0]
        assert event["status"] == "interrupted"
        assert event["diagnostic"]["exception_chain"][0]["frames"]
        assert "secret-account-token" not in json.dumps(event)
        with pytest.raises(FetchFailure, match="inspect saved request"):
            fetcher.fetch("https://example.org/", checkpoint, lambda: None)
        assert len(checkpoint["http_attempts"]) == 1


def test_failed_response_diagnostics_follow_portable_collection_closure(tmp_path):
    from healthlab.signal.collection import collect
    from tests.test_data_pilot import AS_OF, POLICY, registry

    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(503, text="upstream failure"))
    ) as client:
        store = RunStore(tmp_path / "store")
        run = collect(
            store, registry(), mindate=AS_OF, maxdate=AS_OF, fetch_policy=POLICY, rss_client=client
        )
    check = run["source_checks"][0]
    cp = json.loads(store.read(check["checkpoint_sha256"]))
    ref = cp["http_attempts"][0]["diagnostic_sha256"]
    bundle = tmp_path / "bundle"
    export_collection(store, run["run_id"], bundle)
    assert verify_bundle(bundle)["verified"]
    assert RunStore(bundle).read(ref) == b"upstream failure"


def test_verified_backup_restores_latest_without_modifying_original(tmp_path):
    store = RunStore(tmp_path / "store")
    client, _ = transport()
    with client:
        result = execute(store, client)
    before = {str(p): p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    parent = tmp_path / "external"
    parent.mkdir()
    with pytest.raises(ValueError, match="independent"):
        backup.create_snapshot(store, parent)
    snapshot = backup.create_snapshot(store, parent, require_independent=False)
    assert snapshot["latest_restored"]["dataset_id"] == result["dataset_id"]
    path = Path(snapshot["destination"])
    assert backup.verify_snapshot(path)["verified"]
    assert before == {str(p): p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    victim = next((path / "store/objects").iterdir())
    victim.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="integrity"):
        backup.verify_snapshot(path)


def test_pilot_measures_stages_and_records_unexpected_failure_sites(tmp_path, monkeypatch):
    from healthlab.data import pilot

    store = RunStore(tmp_path)
    client, _ = transport()

    def crash(*args):
        raise RuntimeError("secret-account-token")

    real = pilot._dataset
    monkeypatch.setattr(pilot, "_dataset", crash)
    with client, pytest.raises(RuntimeError):
        execute(store, client)
    path = next((tmp_path / "pilot").glob("*/*/*/journal.json"))
    journal = json.loads(path.read_bytes())
    assert journal["stage_timings"][-1]["stage"] == "dataset"
    assert journal["stage_timings"][-1]["status"] == "interrupted"
    assert journal["errors"][-1]["diagnostic"]["exception_chain"][0]["type"] == "RuntimeError"
    assert "secret-account-token" not in json.dumps(journal)
    monkeypatch.setattr(pilot, "_dataset", real)
    client, requests = transport()
    with client:
        result = execute(store, client)
    assert result["status"] == "ready" and not requests
    assert all(e["monotonic_seconds"] >= 0 for e in result["metrics"]["stage_timings"])


def test_sleep_assertion_scoped_to_cli_and_released_on_failure(monkeypatch):
    calls = []

    class Process:
        def terminate(self):
            calls.append("terminate")

        def wait(self, **kwargs):
            calls.append("wait")

    monkeypatch.setattr(power.sys, "platform", "darwin")
    monkeypatch.setattr(
        power.subprocess, "Popen", lambda args, **kw: calls.append(args) or Process()
    )
    with pytest.raises(RuntimeError), power.awake_for_cli():
        raise RuntimeError()
    assert calls[0][:3] == ["/usr/bin/caffeinate", "-is", "-w"]
    assert calls[-2:] == ["terminate", "wait"]


@pytest.mark.parametrize("status,retryable", [(429, True), (401, False)])
def test_broken_failure_body_keeps_http_classification_and_cooldown(tmp_path, status, retryable):
    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            yield b"partial failure"
            raise httpx.ReadTimeout("sensitive exception text")

    checkpoint = {"responses": {}, "http_attempts": []}
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, headers={"retry-after": "120"}, stream=Broken())

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        fetcher = HTMLFetcher(client, RunStore(tmp_path), FetchPolicy(request_interval=0))
        with pytest.raises(FetchFailure) as caught:
            fetcher.fetch("https://example.org/", checkpoint, lambda: None)
    event = checkpoint["http_attempts"][0]
    assert len(requests) == 1 and caught.value.retryable == retryable
    assert event["error_code"] == "html_http"
    assert event["retry_delay_seconds"] == 120
    assert event["diagnostic_may_be_truncated"]
    assert event["diagnostic_body_error"] == "ReadTimeout"
