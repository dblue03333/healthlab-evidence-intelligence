"""Recovery across real manifest reloads, with mock providers and a fake clock."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from healthlab.models import IngestionError
from healthlab.provider import Completion, FakeProvider
from healthlab.signal import provider_failures
from healthlab.signal.triage import load_triage, propose_triage, triage_packet
from healthlab.signal.triage_v5 import coverage_plan
from healthlab.store import json_bytes
from tests.test_signal_triage_v5 import ReadingProvider, case, long_case


@pytest.fixture
def clock(monkeypatch):
    value = [datetime(2026, 10, 19, 8, 0, tzinfo=UTC)]
    monkeypatch.setattr(provider_failures, "now", lambda: value[0])
    return value


def error_body(quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier", delay="60s"):
    return json_bytes(
        {
            "error": {
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": quota_id}],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay},
                ]
            }
        }
    )


class FailingReader(ReadingProvider):
    def __init__(
        self, doc, sid, *, status=429, failure_at=1, code="provider_quota_or_rate", interrupt=False
    ):
        super().__init__(doc, sid)
        self.status, self.failure_at, self.code, self.interrupt = (
            status,
            failure_at,
            code,
            interrupt,
        )

    def complete(self, messages, audit):
        if self.calls + 1 == self.failure_at:
            self.calls += 1
            audit(
                dict(provider="fake", http_status=self.status, error_code=self.code),
                error_body() if self.status else None,
            )
            if self.interrupt:
                raise KeyboardInterrupt
            raise IngestionError(self.code, "Synthetic provider failure")
        return super().complete(messages, audit)


def run(store, triage, provider, **kwargs):
    view = load_triage(store, triage["run_id"])
    options = dict(
        resumable=True,
        total_call_budget=20,
        max_documents=1,
        prompt_version="signal-triage-prompt-6",
        expected_head=view["head"],
    )
    return propose_triage(store, triage["run_id"], provider, **(options | kwargs))


@pytest.mark.parametrize(
    "status,quota,kind,retryable",
    [
        (429, "RequestsPerDayPerProject", "daily_quota", True),
        (429, "RequestsPerMinutePerProject", "rate_limit", True),
        (429, "Unknown", "quota_or_rate_unknown", False),
        (503, "", "unavailable", True),
        (401, "", "auth_or_config", False),
        (400, "", "auth_or_config", False),
    ],
)
def test_error_categories_require_structured_evidence(status, quota, kind, retryable):
    result = provider_failures.failure_details({"http_status": status}, error_body(quota))
    assert (result["kind"], result["retryable"]) == (kind, retryable)
    assert result["retry_delay_seconds"] == 60


def test_retry_headers_and_rpc_delay_are_lower_bounds_not_guaranteed_reset():
    details = provider_failures.failure_details(
        {
            "http_status": 503,
            "retry_after": "Mon, 19 Oct 2026 09:00:00 GMT",
            "finished_at": "2026-10-19T08:00:00+00:00",
        },
        error_body(delay="30.2s"),
    )
    assert details["retry_delay_seconds"] == 3600
    assert (
        provider_failures.failure_details({"http_status": 429}, b"not JSON")["retryable"] is False
    )


def test_quota_mid_long_article_resumes_only_missing_steps_and_preserves_failure(tmp_path, clock):
    store, collection, doc, triage = long_case(tmp_path)
    sid = collection["documents"][0]["snapshot_id"]
    reader = FailingReader(doc, sid, failure_at=2)
    first = run(store, triage, reader)
    failure = copy.deepcopy(first["model_attempts"][0])
    assert first["recovery"]["state"] == "paused" and reader.calls == 2
    assert first["recovery"]["pause"]["kind"] == "daily_quota"
    fresh_reader = ReadingProvider(doc, sid)
    early = run(store, triage, fresh_reader)
    assert early["model_calls"] == 0 and fresh_reader.calls == 0
    clock[0] += timedelta(seconds=61)
    second = run(store, triage, fresh_reader)
    assert second["status"] == "completed"
    required = len(coverage_plan(doc)["chunks"]) + 1
    assert second["recovery"]["total_calls"] == required + 1
    assert fresh_reader.calls == required - 1  # first successful chunk reused
    assert second["model_attempts"][0] == failure
    assert second["model_attempts"][1]["retry_of"] == failure["attempt_id"]
    assert second["model_attempts"][1]["steps"][0]["reused_from_attempt"] == failure["attempt_id"]
    view = load_triage(store, triage["run_id"])
    assert view["decisions"][sid].decision == "include"
    assert all(e["reviewer_type"] == "model" for e in view["events"])
    again = run(store, triage, fresh_reader)
    assert again["model_calls"] == 0 and again["recovery"]["total_calls"] == required + 1
    assert triage_packet(store, triage["run_id"])["recovery"]["state"] == "completed"


def test_durable_budget_is_not_reset_or_silently_increased_by_restart(tmp_path, clock):
    store, collection, doc, triage = case(tmp_path)
    sid = collection["documents"][0]["snapshot_id"]
    run(store, triage, FailingReader(doc, sid, status=503), total_call_budget=1)
    clock[0] += timedelta(hours=1)
    provider = ReadingProvider(doc, sid)
    result = run(store, triage, provider, total_call_budget=1)
    assert result["budget_blocked"] and result["recovery"]["total_calls"] == 1
    assert provider.calls == 0
    with pytest.raises(ValueError, match="Incompatible recovery"):
        run(store, triage, provider, total_call_budget=2)
    assert store.load(triage["run_id"])["recovery"]["total_call_budget"] == 1


def test_whole_article_budget_blocks_before_first_call(tmp_path, clock):
    store, collection, doc, triage = long_case(tmp_path)
    reader = ReadingProvider(doc, collection["documents"][0]["snapshot_id"])
    result = run(store, triage, reader, max_model_calls=1)
    assert result["budget_blocked"] and reader.calls == 0
    assert result["recovery"]["total_calls"] == 0


def test_503_recovery_is_bounded_by_attempt_limit(tmp_path, clock):
    store, collection, doc, triage = case(tmp_path)
    sid = collection["documents"][0]["snapshot_id"]
    first = run(store, triage, FailingReader(doc, sid, status=503), max_attempts=2)
    clock[0] += timedelta(hours=1)
    second = run(store, triage, FailingReader(doc, sid, status=503), max_attempts=2)
    clock[0] += timedelta(hours=1)
    provider = ReadingProvider(doc, sid)
    last = run(store, triage, provider, max_attempts=2)
    assert first["recovery"]["state"] == second["recovery"]["state"] == "paused"
    assert last["recovery"]["pause"]["kind"] == "attempt_budget" and provider.calls == 0


@pytest.mark.parametrize("code", ["provider_timeout", "provider_transport", "provider_interrupted"])
def test_ambiguous_request_outcomes_never_retry_implicitly(tmp_path, clock, code):
    store, collection, doc, triage = case(tmp_path)
    sid = collection["documents"][0]["snapshot_id"]
    first = run(store, triage, FailingReader(doc, sid, status=None, code=code))
    assert first["recovery"]["pause"]["kind"] == "unknown_outcome"
    clock[0] += timedelta(days=3)
    provider = ReadingProvider(doc, sid)
    assert run(store, triage, provider)["model_calls"] == 0
    assert provider.calls == 0


def test_interrupt_is_saved_counted_and_propagated(tmp_path, clock):
    store, collection, doc, triage = case(tmp_path)
    sid = collection["documents"][0]["snapshot_id"]
    with pytest.raises(KeyboardInterrupt):
        run(
            store,
            triage,
            FailingReader(doc, sid, status=None, code="provider_interrupted", interrupt=True),
        )
    state = store.load(triage["run_id"])["recovery"]
    assert len(state["calls"]) == 1 and state["state"] == "blocked"


def test_invalid_model_output_remains_blocked_without_repair(tmp_path, clock):
    store, _, _, triage = case(tmp_path)
    first = run(store, triage, FakeProvider([Completion("not JSON", "fake-test", "stop", {})]))
    assert first["recovery"]["pause"]["kind"] == "invalid_output_or_contract"
    assert not load_triage(store, triage["run_id"])["decisions"]
    assert run(store, triage, FakeProvider([]))["model_calls"] == 0


@pytest.mark.parametrize("content", ["", '{"decision":"exclude"}'])
def test_token_limit_is_distinct_preserves_usage_and_never_auto_retries(tmp_path, clock, content):
    store, _, _, triage = case(tmp_path)
    usage = {"completion_tokens": 8192, "completion_tokens_details": {"reasoning_tokens": 8192}}
    provider = FakeProvider([Completion(content, "fake-test", "length", usage)])
    result = run(store, triage, provider)
    attempt = result["model_attempts"][0]
    assert attempt["error_code"] == "triage_output_token_limit"
    assert result["recovery"]["state"] == "blocked"
    assert result["recovery"]["pause"] == {"kind": "output_token_limit", "retryable": False}
    assert result["recovery"]["total_calls"] == provider.calls == 1
    view = load_triage(store, triage["run_id"])
    assert not view["decisions"]
    response = json.loads(store.read(attempt["steps"][0]["model_response_sha256"]))
    assert response["content"] == content and response["usage"] == usage
    assert run(store, triage, FakeProvider([]))["model_calls"] == 0


@pytest.mark.parametrize("change", ["provider", "prompt", "runtime", "input"])
def test_incompatible_identity_is_rejected_before_network(tmp_path, clock, change):
    store, collection, doc, triage = case(tmp_path)
    sid = collection["documents"][0]["snapshot_id"]
    run(store, triage, FailingReader(doc, sid))
    provider = ReadingProvider(doc, sid)
    kwargs = {}
    if change == "provider":
        provider.identity = lambda: {"model": "other"}
    elif change == "prompt":
        kwargs["prompt_version"] = "signal-triage-prompt-7"
    else:
        saved = store.load(triage["run_id"])
        saved["recovery"]["identity"][
            "runtime_sha256" if change == "runtime" else "input_sha256"
        ] = "changed"
        store.save(saved)
    with pytest.raises(ValueError, match="Incompatible recovery"):
        run(store, triage, provider, **kwargs)
    assert provider.calls == 0


def test_managed_run_cannot_bypass_budget_through_legacy_entrypoint(tmp_path, clock):
    store, collection, doc, triage = case(tmp_path)
    reader = ReadingProvider(doc, collection["documents"][0]["snapshot_id"])
    run(store, triage, reader)
    with pytest.raises(ValueError, match="requires explicit resumable"):
        propose_triage(
            store, triage["run_id"], reader, expected_head=store.load(triage["run_id"])["head"]
        )


def test_lost_process_after_dispatch_is_counted_and_blocks_automatic_resend(tmp_path, clock):
    store, collection, doc, triage = case(tmp_path)
    sid = collection["documents"][0]["snapshot_id"]
    run(store, triage, FailingReader(doc, sid))
    saved = store.load(triage["run_id"])
    saved["recovery"]["calls"][0]["status"] = "started"
    store.save(saved)
    clock[0] += timedelta(days=1)
    reader = ReadingProvider(doc, sid)
    result = run(store, triage, reader)
    assert result["recovery"]["state"] == "blocked" and reader.calls == 0
    assert result["recovery"]["total_calls"] == 1


def test_completed_response_checkpoint_can_replay_after_process_loss(tmp_path, clock):
    store, collection, doc, triage = case(tmp_path)
    sid = collection["documents"][0]["snapshot_id"]
    run(store, triage, ReadingProvider(doc, sid))
    saved = store.load(triage["run_id"])
    saved["head"] = None
    saved["model_attempts"][0]["status"] = "started"
    saved["recovery"]["state"] = "ready"
    store.save(saved)
    reader = ReadingProvider(doc, sid)
    result = run(store, triage, reader)
    assert result["status"] == "completed" and reader.calls == 0
    assert result["recovery"]["total_calls"] == 1
    assert load_triage(store, triage["run_id"])["decisions"][sid].decision == "include"


def test_corrupt_reused_prompt_is_rejected_before_new_requests(tmp_path, clock):
    store, collection, doc, triage = long_case(tmp_path)
    sid = collection["documents"][0]["snapshot_id"]
    run(store, triage, FailingReader(doc, sid, failure_at=2))
    saved = store.load(triage["run_id"])
    saved["model_attempts"][0]["steps"][0]["prompt_sha256"] = store.put(json_bytes([]))
    store.save(saved)
    clock[0] += timedelta(hours=1)
    reader = ReadingProvider(doc, sid)
    result = run(store, triage, reader)
    assert result["recovery"]["state"] == "blocked" and reader.calls == 0
    assert not load_triage(store, triage["run_id"])["decisions"]


def test_imported_cooldown_preserves_evidence_without_api_call(tmp_path, clock):
    from healthlab.signal.storage import collection_storage
    from healthlab.signal.triage_recovery import Recovery

    store, collection, doc, triage = case(tmp_path)
    provider = ReadingProvider(doc, collection["documents"][0]["snapshot_id"])
    storage = collection_storage(store)
    with storage.state.lock():
        saved = store.load(triage["run_id"])
        recovery = Recovery(storage, saved, provider, "signal-triage-prompt-6", 20, 3)
        recovery.defer_from_evidence(
            dict(http_status=429, finished_at=clock[0].isoformat()), error_body()
        )
    result = run(store, triage, provider)
    assert result["recovery"]["total_calls"] == provider.calls == 0
    assert result["recovery"]["pause"]["evidence_sha256"]
    clock[0] += timedelta(seconds=61)
    assert run(store, triage, provider)["status"] == "completed"


def test_native_schema_checkpoint_reuse_through_gemini_mock_transport(tmp_path, clock):
    import json

    import httpx

    from tests.test_gemini import envelope, provider
    from tests.test_signal_triage_v7 import evidence_payload

    store, collection, doc, triage = long_case(tmp_path)
    sid = collection["documents"][0]["snapshot_id"]
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 2:
            return httpx.Response(429, content=error_body(), headers={"Retry-After": "90"})
        data = json.loads(payload["contents"][0]["parts"][0]["text"])
        if data["stage"] == "chunk":
            units = [p for p in data["source_passages"] if "care guideline" in p["quote"]]
            value = dict(
                topic="aging_society" if units else "out_of_scope",
                summary="Synthetic evidence",
                uncertainties="",
                evidence_passage_ids=[u["passage_id"] for u in units],
                event_date_candidate_ids=[d["candidate_id"] for d in data["date_candidates"]],
            )
        else:
            value = evidence_payload(doc, sid)
        return httpx.Response(
            200,
            json=envelope(
                candidates=[
                    {"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(value)}]}}
                ]
            ),
        )

    first = run(store, triage, provider(handler), prompt_version="signal-triage-prompt-7")
    assert first["recovery"]["pause"]["retry_delay_seconds"] == 90
    assert first["recovery"]["pause"]["kind"] == "daily_quota"
    clock[0] += timedelta(seconds=91)
    result = run(store, triage, provider(handler), prompt_version="signal-triage-prompt-7")
    assert result["status"] == "completed"
    assert len(requests) == len(coverage_plan(doc)["chunks"]) + 2
    assert load_triage(store, triage["run_id"])["decisions"][sid].decision == "include"
    assert all(p["generationConfig"]["responseFormat"]["text"]["schema"] for p in requests)
