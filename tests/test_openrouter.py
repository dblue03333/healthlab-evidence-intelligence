import json

import httpx
import pytest

from healthlab.config import OpenRouterSettings
from healthlab.extraction import cache_identity
from healthlab.models import ConfigurationError, IngestionError
from healthlab.openrouter import OpenRouterProvider


def settings(**kwargs):
    return OpenRouterSettings(
        _env_file=None,
        api_key="test-router-secret",
        model="qwen/qwen3-32b",
        provider_slug="deepinfra/fp8",
        **kwargs,
    )


def envelope(**changes):
    return {
        "id": "generation-test",
        "model": "qwen/qwen3-32b",
        "provider": "DeepInfra",
        "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
        "usage": {"total_tokens": 12, "cost": 0.001},
        **changes,
    }


def test_pinned_wire_identity_actual_metadata_cost_and_no_key_in_audit():
    events = []
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=envelope())

    p = OpenRouterProvider(settings(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = p.complete([], lambda e, b: events.append((e, b)))
    assert seen[0]["provider"] == {
        "only": ["deepinfra/fp8"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }
    assert "models" not in seen[0] and "response_format" not in seen[0]
    assert result.usage["cost"] == 0.001
    assert (
        events[0][0]["actual_provider"] == "DeepInfra"
        and events[0][0]["generation_id"] == "generation-test"
    )
    assert "test-router-secret" not in json.dumps(events[0][0])
    assert "test-router-secret" not in json.dumps(p.identity())


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "provider_auth"),
        (402, "provider_credit"),
        (429, "provider_quota_or_rate"),
        (503, "provider_http"),
    ],
)
def test_errors_do_not_retry_or_fallback(status, code):
    events = []
    p = OpenRouterProvider(
        settings(),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(status, content=b"test-router-secret")
            )
        ),
    )
    with pytest.raises(IngestionError) as err:
        p.complete([], lambda e, b: events.append((e, b)))
    assert err.value.code == code and len(events) == 1
    assert b"test-router-secret" not in events[0][1]


def test_timeout_is_audited_once():
    events = []

    def handler(request):
        raise httpx.ReadTimeout("sensitive URL", request=request)

    p = OpenRouterProvider(settings(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(IngestionError) as err:
        p.complete([], lambda e, b: events.append((e, b)))
    assert err.value.code == "provider_timeout" and len(events) == 1
    assert "sensitive" not in str(err.value)


def test_wrong_model_is_retained_but_rejected():
    events = []
    p = OpenRouterProvider(
        settings(),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=envelope(model="other/model"))
            )
        ),
    )
    with pytest.raises(IngestionError) as err:
        p.complete([], lambda e, b: events.append((e, b)))
    assert err.value.code == "provider_model_mismatch"
    assert events[0][0]["actual_model"] == "other/model"


def test_reasoning_only_output_preserves_usage():
    p = OpenRouterProvider(
        settings(),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json=envelope(
                        choices=[{"finish_reason": "length", "message": {"content": None}}]
                    ),
                )
            )
        ),
    )
    result = p.complete([], lambda *_: None)
    assert (
        result.finish_reason == "length"
        and result.content == ""
        and result.usage["total_tokens"] == 12
    )


def test_cache_only_needs_no_key_or_http(monkeypatch):
    cfg = settings()
    cfg.api_key = ""
    monkeypatch.setattr(httpx.Client, "__init__", lambda *a, **k: pytest.fail("network client"))
    p = OpenRouterProvider(cfg, cache_only=True)
    with pytest.raises(IngestionError) as err:
        p.complete([], lambda *_: None)
    assert err.value.code == "cache_miss"


def test_routing_changes_cache_identity():
    p = OpenRouterProvider(settings(), lazy=True)
    first = cache_identity("snapshot", [], p.identity())[0]
    p.settings.provider_slug = "other/endpoint"
    assert cache_identity("snapshot", [], p.identity())[0] != first


@pytest.mark.parametrize(
    "model", ["", "openrouter/auto", "openrouter/free", "qwen/qwen3-32b:nitro"]
)
def test_explicit_model_required(model):
    cfg = settings()
    cfg.model = model
    with pytest.raises(ConfigurationError):
        OpenRouterProvider(cfg, lazy=True)


def test_key_spending_limit_403_is_credit_failure():
    events = []
    p = OpenRouterProvider(
        settings(),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    403, json={"error": {"message": "Key limit exceeded (total limit)."}}
                )
            )
        ),
    )
    with pytest.raises(IngestionError) as err:
        p.complete([], lambda e, b: events.append((e, b)))
    assert err.value.code == "provider_credit"
    assert len(events) == 1
