"""Validate a real assignment before opening parallel editorial calls."""

import json
from threading import Barrier, current_thread, main_thread

import pytest

from healthlab import editorial as ed
from healthlab.models import IngestionError
from healthlab.provider import Completion
from tests.editorial_legacy import resolve_v12
from tests.test_editorial import proposal, setup_run
from tests.test_editorial_passages import CITY, reference_payload, row


def three_jobs(tmp_path, monkeypatch, responses):
    store, run, provider = setup_run(tmp_path, monkeypatch, responses)
    article = json.loads(store.read(run["input_sha256"]))[0]
    rows = [article]
    for did in ("second", "third"):
        rows.append(dict(article, document_id=did))
        job = store.new("editorial_call", run["question"])
        job.update(role="classify", document_id=did, status="prepared")
        store.save(job)
        run["jobs"].append(job["run_id"])
    run["input_sha256"] = store.put(ed.json_bytes(rows))
    store.save(run)
    return store, run, provider


@pytest.mark.parametrize(
    "failure",
    [
        IngestionError("provider_envelope", "empty final content"),
        IngestionError("provider_auth", "auth"),
        IngestionError("provider_timeout", "timeout"),
        Completion("{}", "wrong-model", "stop", {}),
        Completion("", "fake-test", "length", {"total_tokens": 20}),
        Completion("not JSON", "fake-test", "stop", {}),
        Completion("{}", "fake-test", "stop", {}),
        proposal(relevance_quote="Unknown source passage"),
    ],
)
def test_failed_canary_never_opens_parallel_work(tmp_path, monkeypatch, failure):
    # Use an explicit unknown passage ID for source validation failure.
    if isinstance(failure, dict):
        failure = dict(failure, relevance_passage_id="P999")
        failure.pop("relevance_quote")
        failure["facts"] = []
    store, run, provider = three_jobs(tmp_path, monkeypatch, [failure])
    ed.run_calls(store, run["run_id"], provider, workers=4)
    js = ed.jobs(store, store.load(run["run_id"]))
    assert provider.calls == 1 and js[0]["status"] == "failed"
    assert all(j["status"] == "prepared" and not j.get("charged") for j in js[1:])
    c = store.load(run["run_id"])["batch_canaries"][-1]
    assert c["status"] == "failed" and c["job_id"] == js[0]["run_id"]


def test_completed_canary_precedes_parallel_work_and_is_not_an_extra_call(tmp_path, monkeypatch):
    store, run, provider = three_jobs(tmp_path, monkeypatch, [proposal()] * 3)
    complete = provider.complete
    barrier = Barrier(2)

    def gated(messages, audit):
        if current_thread() is main_thread():
            assert provider.calls == 0
        else:
            assert store.load(run["jobs"][0])["status"] == "completed"
            assert store.load(run["run_id"])["batch_canaries"][-1]["status"] == "passed"
            barrier.wait(timeout=5)
        return complete(messages, audit)

    monkeypatch.setattr(provider, "complete", gated)
    result = ed.run_calls(store, run["run_id"], provider, workers=2, max_calls=3)
    assert provider.calls == result["calls_dispatched"] == 3
    assert result["classified"] == 3
    assert len(result["batch_canaries"]) == 1
    ed.run_calls(store, run["run_id"], provider, workers=2)
    assert provider.calls == 3 and len(store.load(run["run_id"])["batch_canaries"]) == 1


def test_canary_uses_max_calls_slot_and_rechecks_only_new_pending_work(tmp_path, monkeypatch):
    store, run, provider = three_jobs(tmp_path, monkeypatch, [proposal()] * 3)
    ed.run_calls(store, run["run_id"], provider, workers=4, max_calls=1)
    assert provider.calls == 1
    js = ed.jobs(store, store.load(run["run_id"]))
    assert [j["status"] for j in js] == ["completed", "prepared", "prepared"]
    ed.run_calls(store, run["run_id"], provider, workers=4, max_calls=2)
    assert provider.calls == 3
    assert [c["job_id"] for c in store.load(run["run_id"])["batch_canaries"]] == run["jobs"][:2]


def test_version11_keeps_policy2_and_is_read_only_after_runtime_bump(tmp_path, monkeypatch):
    article = row(CITY)
    window = {"mindate": "2026-11-01", "maxdate": "2026-11-07"}
    assert ed.passage_policy("gold-editorial-11") == "editorial-passages-2"
    for role in ("classify", "scope"):
        assert (
            ed.messages(role, article, window, version="gold-editorial-11")[0]
            == ed.messages(role, article, window, version="gold-editorial-12")[0]
        )
    p = ed.resolve_reference_ids(reference_payload(article), article, version="gold-editorial-11")
    assert p == resolve_v12(reference_payload(article), article)
    store, run, provider = setup_run(tmp_path, monkeypatch, [])
    run.update(version="gold-editorial-11", runtime_sha256="old")
    store.save(run)
    ed.load(store, run["run_id"])
    with pytest.raises(ValueError, match="read-only"):
        ed.load(store, run["run_id"], provider, for_inference=True)
