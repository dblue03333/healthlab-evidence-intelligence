"""Gemini wire contract, billing-safe routing, failures and Signal integration."""

import json

import httpx
import pytest
from pydantic import ValidationError

from healthlab.config import GeminiSettings
from healthlab.gemini import BASE_URL, GeminiProvider
from healthlab.models import ConfigurationError, IngestionError
from healthlab.provider import schema_digest

SECRET = "test-gemini-secret"
SCHEMA = {
    "type": "object",
    "properties": {"decision": {"enum": ["include", "exclude", "needs_review"]}},
    "required": ["decision"],
    "additionalProperties": False,
}
MESSAGES = [
    {"role": "system", "content": "Classify the supplied article."},
    {"role": "user", "content": "Synthetic older-person care article."},
]


def settings(**changes):
    return GeminiSettings(_env_file=None, **({"api_key": SECRET} | changes))


def envelope(**changes):
    return {
        "modelVersion": "gemini-3.8-flash",
        "responseId": "response-test",
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": '{"decision":"include"}'}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 20,
            "candidatesTokenCount": 8,
            "thoughtsTokenCount": 12,
            "cachedContentTokenCount": 5,
            "totalTokenCount": 40,
        },
        **changes,
    }


def provider(handler, **changes):
    return GeminiProvider(
        settings(**changes), client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_native_schema_uses_google_header_and_exact_contract_without_tools_or_query_key():
    events, requests = [], []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=envelope())

    p = provider(handler)
    result = p.complete_json(MESSAGES, lambda e, b: events.append((e, b)), schema=SCHEMA)
    request = requests[0]
    payload = json.loads(request.content)
    assert str(request.url) == f"{BASE_URL}/models/gemini-3.8-flash:generateContent"
    assert request.headers["x-goog-api-key"] == SECRET
    assert SECRET not in str(request.url) and SECRET not in request.content.decode()
    assert payload["systemInstruction"] == {"parts": [{"text": MESSAGES[0]["content"]}]}
    assert payload["contents"] == [{"role": "user", "parts": [{"text": MESSAGES[1]["content"]}]}]
    assert payload["generationConfig"] == {
        "responseFormat": {"text": {"mimeType": "APPLICATION_JSON", "schema": SCHEMA}},
        "candidateCount": 1,
        "temperature": 1.0,
        "maxOutputTokens": 8192,
        "thinkingConfig": {"thinkingLevel": "low", "includeThoughts": False},
    }
    assert "tools" not in payload and "cachedContent" not in payload
    assert events[0][0]["response_schema_sha256"] == schema_digest(SCHEMA)
    assert events[0][0]["reported_usage"]["thoughtsTokenCount"] == 12
    assert result.finish_reason == "stop" and result.actual_model == "gemini-3.8-flash"
    assert result.usage["completion_tokens"] == 20  # Include billed thoughts, not only answer.
    assert result.usage["total_tokens"] == 40 and result.usage["cached_input_tokens"] == 5
    assert SECRET not in json.dumps(events[0][0]) + json.dumps(p.identity())


def test_text_and_json_object_modes_do_not_silently_enable_schema():
    bodies, events = [], []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=envelope())

    p = provider(handler)
    p.complete(MESSAGES, lambda *_: None)
    p.complete_json_object(MESSAGES, lambda e, b: events.append(e), schema=SCHEMA)
    assert "responseFormat" not in bodies[0]["generationConfig"]
    assert bodies[1]["generationConfig"]["responseFormat"] == {
        "text": {"mimeType": "APPLICATION_JSON"}
    }
    assert events[0]["response_format"] == "json_object"
    assert events[0]["response_schema_sha256"] == schema_digest(SCHEMA)


@pytest.mark.parametrize(
    "status,code",
    [
        (400, "provider_request"),
        (401, "provider_auth"),
        (402, "provider_credit"),
        (403, "provider_auth"),
        (404, "provider_model_unavailable"),
        (429, "provider_quota_or_rate"),
        (503, "provider_http"),
    ],
)
def test_http_errors_are_audited_redacted_once_without_fallback(status, code):
    requests, events = [], []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": f"Echo {SECRET}"}})

    p = provider(handler)
    with pytest.raises(IngestionError) as err:
        p.complete_json(MESSAGES, lambda e, b: events.append((e, b)), schema=SCHEMA)
    assert err.value.code == code and len(requests) == len(events) == 1
    assert events[0][0]["body_redacted"] is True
    assert SECRET.encode() not in events[0][1] and SECRET not in str(err.value)


def test_redirect_is_not_followed_with_credential():
    requests, events = [], []

    def handler(request):
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://untrusted.example/collect"})

    # Even an injected client configured to follow redirects is overridden per request.
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    p = GeminiProvider(settings(), client=client)
    with pytest.raises(IngestionError, match="307"):
        p.complete(MESSAGES, lambda e, b: events.append(e))
    assert len(requests) == len(events) == 1


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError, KeyboardInterrupt])
def test_transport_and_interruption_preserve_one_attempt_without_sensitive_exception(failure):
    events = []

    def handler(request):
        raise failure("sensitive URL and secret")

    p = provider(handler)
    expected = KeyboardInterrupt if failure is KeyboardInterrupt else IngestionError
    with pytest.raises(expected) as err:
        p.complete(MESSAGES, lambda e, b: events.append((e, b)))
    assert len(events) == 1 and events[0][1] is None
    expected_code = {
        httpx.ReadTimeout: "provider_timeout",
        httpx.ConnectError: "provider_transport",
        KeyboardInterrupt: "provider_interrupted",
    }[failure]
    assert events[0][0]["error_code"] == expected_code
    if expected is IngestionError:
        assert "sensitive" not in str(err.value)


@pytest.mark.parametrize("parts", [[], [{"thought": True, "text": "Not a final answer"}]])
def test_max_tokens_keeps_usage_and_never_promotes_reasoning_to_answer(parts):
    body = envelope(candidates=[{"finishReason": "MAX_TOKENS", "content": {"parts": parts}}])
    p = provider(lambda _: httpx.Response(200, json=body))
    result = p.complete(MESSAGES, lambda *_: None)
    assert result.content == "" and result.finish_reason == "length"
    assert result.usage["reasoning_tokens"] == 12 and result.usage["total_tokens"] == 40


def test_final_text_is_separate_from_thoughts_and_multiple_text_parts_are_combined():
    body = envelope(
        candidates=[
            {
                "finishReason": "STOP",
                "content": {
                    "parts": [
                        {"thought": True, "text": "Not an answer"},
                        {"text": '{"decision":', "thoughtSignature": "opaque"},
                        {"text": '"include"}'},
                    ]
                },
            }
        ]
    )
    p = provider(lambda _: httpx.Response(200, json=body))
    result = p.complete(MESSAGES, lambda *_: None)
    assert result.content == '{"decision":"include"}'


@pytest.mark.parametrize("finish", ["SAFETY", "RECITATION", "OTHER", "MALFORMED_FUNCTION_CALL"])
def test_non_stop_candidate_never_becomes_success(finish):
    body = envelope(candidates=[{"finishReason": finish}])
    result = provider(lambda _: httpx.Response(200, json=body)).complete(MESSAGES, lambda *_: None)
    assert result.finish_reason != "stop" and result.content == ""


def test_blocked_prompt_retains_raw_and_usage():
    events = []
    body = envelope(candidates=[], promptFeedback={"blockReason": "SAFETY"})
    p = provider(lambda _: httpx.Response(200, json=body))
    with pytest.raises(IngestionError) as err:
        p.complete(MESSAGES, lambda e, b: events.append((e, b)))
    assert err.value.code == "provider_blocked" and events[0][0]["reported_usage"]
    assert json.loads(events[0][1])["promptFeedback"]["blockReason"] == "SAFETY"


@pytest.mark.parametrize(
    "change",
    [
        {"modelVersion": "other-model"},
        {"modelVersion": None},
        {"candidates": []},
        {"candidates": [{"finishReason": "STOP"}]},
        {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"functionCall": {}}]}}]},
        {
            "candidates": [
                {"finishReason": "STOP", "content": {"parts": [{"text": "{}", "thought": "false"}]}}
            ]
        },
        {"usageMetadata": {"totalTokenCount": -1}},
        {"usageMetadata": {"totalTokenCount": True}},
        {"usageMetadata": []},
    ],
)
def test_bad_envelopes_are_retained_and_rejected(change):
    events = []
    p = provider(lambda _: httpx.Response(200, json=envelope(**change)))
    with pytest.raises(IngestionError) as err:
        p.complete(MESSAGES, lambda e, b: events.append((e, b)))
    expected = (
        "provider_model_mismatch"
        if change.get("modelVersion") == "other-model"
        else "provider_envelope"
    )
    assert err.value.code == expected and len(events) == 1 and events[0][1]


def test_cache_only_constructs_without_key_or_network(monkeypatch):
    monkeypatch.setattr(
        httpx.Client, "__init__", lambda *a, **k: pytest.fail("HTTP client created")
    )
    p = GeminiProvider(settings(api_key=""), cache_only=True)
    with pytest.raises(IngestionError) as err:
        p.complete(MESSAGES, lambda *_: None)
    assert err.value.code == "cache_miss"


@pytest.mark.parametrize("model", ["gemini-flash-latest", "gemini-3.8-flash/other", "", "auto"])
def test_only_pinned_supported_model_ids_can_be_configured(model):
    with pytest.raises(ValidationError):
        settings(model=model)


@pytest.mark.parametrize("key", ["", "your_placeholder", " key-with-spaces "])
def test_bad_key_fails_locally_without_disclosing_value(key):
    with pytest.raises(ConfigurationError) as err:
        GeminiProvider(settings(api_key=key))
    assert err.value.code == "config_gemini_api_key"
    if key:
        assert key not in str(err.value)


def test_native_schema_integration_replays_full_chunk_coverage_and_gates_undated_include(tmp_path):
    from healthlab.signal.triage import load_triage, propose_triage
    from tests.test_signal_triage_v5 import long_case
    from tests.test_signal_triage_v7 import evidence_payload

    store, collection, doc, triage = long_case(tmp_path)
    sid = collection["documents"][0]["snapshot_id"]
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        data = json.loads(payload["contents"][0]["parts"][0]["text"])
        if data["stage"] == "chunk":
            units = [p for p in data["source_passages"] if "care guideline" in p["quote"]]
            value = dict(
                topic="aging_society" if units else "out_of_scope",
                summary="Selected literal evidence" if units else "General context",
                uncertainties="",
                evidence_passage_ids=[u["passage_id"] for u in units],
                event_date_candidate_ids=[d["candidate_id"] for d in data["date_candidates"]],
            )
        else:
            value = evidence_payload(doc, sid, event_date_candidate_id=None)
        return httpx.Response(
            200,
            json=envelope(
                candidates=[
                    {"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(value)}]}}
                ]
            ),
        )

    p = provider(handler)
    result = propose_triage(
        store,
        triage["run_id"],
        p,
        expected_head=None,
        max_documents=1,
        prompt_version="signal-triage-prompt-7",
    )
    view = load_triage(store, triage["run_id"])
    assert result["status"] == "completed" and len(requests) > 2
    assert view["decisions"][sid].decision == "needs_review"  # Gate, not model accuracy.
    assert view["decisions"][sid].freshness == "unknown"
    assert all("schema" in r["generationConfig"]["responseFormat"]["text"] for r in requests)
    assert all(e["reviewer_type"] == "model" for e in view["events"])
    assert view["run"]["requests"][0]["provider"] == "gemini"


def test_cli_explicit_gemini_route_and_default_fpt_are_independent(monkeypatch):
    from healthlab.signal import __main__ as cli

    constructed = []
    monkeypatch.setattr(cli, "GeminiSettings", lambda: "gemini-settings")
    monkeypatch.setattr(cli, "FPTSettings", lambda: "fpt-settings")
    monkeypatch.setattr(cli, "GeminiProvider", lambda c: constructed.append(c) or "gemini")
    monkeypatch.setattr(cli, "FPTProvider", lambda c: constructed.append(c) or "fpt")
    assert cli.signal_provider("gemini") == "gemini"
    assert cli.signal_provider("fpt") == "fpt"
    assert constructed == ["gemini-settings", "fpt-settings"]
    with pytest.raises(ValueError, match="Unknown"):
        cli.signal_provider("automatic-fallback")


@pytest.mark.parametrize("fault", [None, "invented_quote", "incomplete", "quota"])
def test_structured_draft_retains_schema_and_rejects_bad_evidence_without_retry(tmp_path, fault):
    from healthlab.signal.contracts import Draft
    from healthlab.signal.newsletter import draft_issue
    from tests.test_signal import collected, content, triaged

    store, collection = collected(tmp_path)
    triage_id = triaged(store, collection)  # Synthetic fixture, never a real human label.
    value = content(collection["documents"][0]["snapshot_id"])
    if fault == "invented_quote":
        value["signals"][0]["supporting_passage"] = "Invented evidence"
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if fault == "quota":
            return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}})
        return httpx.Response(
            200,
            json=envelope(
                candidates=[
                    {
                        "finishReason": "MAX_TOKENS" if fault == "incomplete" else "STOP",
                        "content": {"parts": [{"text": json.dumps(value)}]},
                    }
                ]
            ),
        )

    run = draft_issue(
        store, collection["run_id"], provider(handler), triage_id=triage_id, structured_output=True
    )
    assert run["status"] == ("completed" if fault is None else "failed")
    assert len(requests) == len(run["requests"]) == 1
    assert run["response_format"] == "json_schema"
    assert json.loads(store.read(run["response_schema_sha256"])) == Draft.model_json_schema()
    assert (
        requests[0]["generationConfig"]["responseFormat"]["text"]["schema"]
        == Draft.model_json_schema()
    )
    assert "approval_sha256" not in run


def test_structured_empty_draft_does_not_call_or_require_provider(tmp_path):
    from healthlab.signal.newsletter import draft_issue
    from tests.test_signal import collected, triaged

    store, collection = collected(tmp_path)
    run = draft_issue(
        store,
        collection["run_id"],
        triage_id=triaged(store, collection, include=False),
        structured_output=True,
    )
    assert run["status"] == "completed" and run["requests"] == []
    assert "response_schema_sha256" not in run


def test_cli_gemini_options_are_opt_in_and_do_not_call_for_help(capsys):
    from healthlab.signal.__main__ import main

    for arguments in (["triage", "propose", "--help"], ["draft", "--help"]):
        with pytest.raises(SystemExit) as err:
            main(arguments)
        output = capsys.readouterr().out
        assert err.value.code == 0 and "gemini" in output
        if arguments[0] == "draft":
            assert "--structured-output" in output


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"role": "assistant", "content": "No initial user"}],
        [{"role": "tool", "content": "Not supported"}],
        [{"role": "user", "content": [{"text": "Not plain text"}]}],
        [{"role": "user", "content": "Input"}, {"role": "system", "content": "Late override"}],
    ],
)
def test_invalid_messages_fail_locally_before_http(messages):
    p = provider(lambda _: pytest.fail("Unexpected network call"))
    with pytest.raises(ValueError):
        p.complete(messages, lambda *_: pytest.fail("No request should have started"))


def test_billing_status_is_never_inferred_from_key_in_check_config(tmp_path, capsys, monkeypatch):
    from healthlab.signal.__main__ import main

    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    main(["--store", str(tmp_path), "check-config"])
    output = capsys.readouterr().out
    value = json.loads(output)
    assert value["gemini_api_key_configured"] is True
    assert value["gemini_billing_tier"] == "not_verifiable_from_api_key"
    assert SECRET not in output
