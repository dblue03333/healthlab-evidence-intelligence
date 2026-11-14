"""Versioned FPT thinking options at the actual wire/cache/CLI boundaries."""

import json
from contextlib import nullcontext

import httpx
import pytest
from pydantic import ValidationError

from healthlab import editorial as ed
from healthlab.config import FPTSettings
from healthlab.editorial import cli as editorial_cli
from healthlab.models import IngestionError
from healthlab.provider import FPTProvider

SCHEMA = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


@pytest.mark.parametrize("mode", ["disabled", "enabled", "provider_default"])
@pytest.mark.parametrize("method", ["complete", "complete_json", "complete_json_object"])
def test_configured_request_mode_matches_wire_audit_and_identity(mode, method):
    seen, events = [], []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "DeepSeek-V4-Flash",
                "usage": {"total_tokens": 10},
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = FPTProvider(
        FPTSettings(_env_file=None, api_key="fixture-secret", thinking_mode=mode), client=client
    )
    kwargs = {} if method == "complete" else {"schema": SCHEMA}
    result = getattr(provider, method)([], lambda e, b: events.append((e, b)), **kwargs)
    options = {} if mode == "provider_default" else {"thinking": {"type": mode}}
    assert {k: seen[0][k] for k in options} == options
    assert ("thinking" in seen[0]) == bool(options)
    assert provider.identity()["request_options"] == events[0][0]["request_options"] == options
    assert provider.identity()["adapter_version"] == "fpt-chat-2"
    assert result.content == "{}" and len(seen) == len(events) == 1
    assert "fixture-secret" not in (
        json.dumps(provider.identity()) + json.dumps(events[0][0]) + events[0][1].decode()
    )
    client.close()


def test_default_env_validation_and_cli_override(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("FPT_THINKING_MODE", raising=False)
    assert FPTSettings(_env_file=None).thinking_mode == "disabled"
    monkeypatch.setenv("FPT_THINKING_MODE", "enabled")
    assert FPTSettings(_env_file=None).thinking_mode == "enabled"
    with pytest.raises(ValidationError):
        FPTSettings(_env_file=None, thinking_mode="silently_guess")
    monkeypatch.setattr(editorial_cli, "awake_for_cli", lambda: nullcontext())
    seen = []

    def prepare(source, store, dataset, provider, **kwargs):
        seen.append(provider.identity())
        return {"articles": 0, "classified": 0}

    monkeypatch.setattr(editorial_cli, "prepare", prepare)
    assert (
        ed.main(
            [
                "--store",
                str(tmp_path / "store"),
                "--fpt-thinking-mode",
                "disabled",
                "prepare",
                "a" * 64,
                "--source-store",
                str(tmp_path / "source"),
            ]
        )
        == 0
    )
    assert seen[0]["request_options"] == {"thinking": {"type": "disabled"}}
    capsys.readouterr()


@pytest.mark.parametrize("content", [None, ""])
def test_reasoning_only_response_is_still_rejected_once(content):
    seen, events = [], []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "model": "DeepSeek-V4-Flash",
                "usage": {"total_tokens": 12},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": content,
                            "reasoning_content": "{}",
                        },
                    }
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    p = FPTProvider(FPTSettings(_env_file=None, api_key="fixture-secret"), client=client)
    with pytest.raises(IngestionError) as exc:
        p.complete_json([], lambda e, b: events.append((e, b)), schema=SCHEMA)
    assert exc.value.code == "provider_envelope" and len(seen) == len(events) == 1
    assert json.loads(events[0][1])["usage"]["total_tokens"] == 12
    client.close()
