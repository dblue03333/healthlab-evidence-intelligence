"""Small provider interface; no silent retry or fallback between models."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx

from healthlab.config import FPTSettings
from healthlab.models import IngestionError
from healthlab.pubmed import utc_now

Audit = Callable[[dict, bytes | None], None]


@dataclass
class Completion:
    content: str
    actual_model: str
    finish_reason: str
    usage: dict


class Provider(Protocol):
    def identity(self) -> dict: ...
    def complete(self, messages: list[dict], audit: Audit) -> Completion: ...


class FPTProvider:
    def __init__(self, settings: FPTSettings, client=None, *, cache_only=False, lazy=False):
        if not cache_only and not lazy:
            settings.validate_online()
        self.settings = settings
        self.cache_only = cache_only
        self.client = (
            None
            if cache_only
            else (client if lazy else (client or httpx.Client(timeout=settings.timeout)))
        )
        self.owns_client = client is None and not cache_only

    def identity(self):
        return {
            "provider": "fpt",
            "adapter_version": "fpt-chat-1",
            "base_url": self.settings.base_url.rstrip("/"),
            "model": self.settings.model,
            "temperature": 0.0,
            "max_tokens": self.settings.max_tokens,
        }

    def close(self):
        if self.owns_client and self.client is not None:
            self.client.close()

    def complete(self, messages, audit):
        if self.cache_only:
            raise IngestionError(
                "cache_miss", "No valid cached extraction; cache-only mode does not call FPT"
            )
        self.settings.validate_online()
        if self.client is None:
            self.client = httpx.Client(timeout=self.settings.timeout)
        key = self.settings.api_key.get_secret_value()
        event = {
            "provider": "fpt",
            "started_at": utc_now(),
            "http_status": None,
            "error_code": None,
            "body_redacted": False,
        }
        try:
            response = self.client.post(
                self.settings.base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": self.settings.model,
                    "messages": messages,
                    "temperature": 0.0,
                    "max_tokens": self.settings.max_tokens,
                },
            )
        except httpx.TransportError as exc:
            code = (
                "provider_timeout"
                if isinstance(exc, httpx.TimeoutException)
                else "provider_transport"
            )
            event.update(error_code=code, finished_at=utc_now())
            audit(event, None)
            raise IngestionError(code, "FPT request did not complete") from None
        raw = response.content
        for secret in {key.encode(), json.dumps(key)[1:-1].encode()}:
            if secret and secret in raw:
                raw = raw.replace(secret, b"[REDACTED_API_KEY]")
                event["body_redacted"] = True
        event.update(http_status=response.status_code, finished_at=utc_now())
        audit(event, raw)  # Persist before decoding, including HTTP failures.
        if response.status_code != 200:
            code = {401: "provider_auth", 403: "provider_auth", 429: "provider_quota_or_rate"}.get(
                response.status_code, "provider_http"
            )
            raise IngestionError(
                code, f"FPT HTTP status {response.status_code}; no automatic fallback"
            )
        try:
            body = json.loads(raw)
            if body.get("error"):
                raise ValueError("API error")
            choice = body["choices"][0]
            content, model, finish = (
                choice["message"]["content"],
                body["model"],
                choice["finish_reason"],
            )
            usage = body.get("usage") or {}
            if not all(
                isinstance(v, str) and v for v in (content, model, finish)
            ) or not isinstance(usage, dict):
                raise ValueError("invalid envelope")
            return Completion(content, model, finish, usage)
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):
            raise IngestionError("provider_envelope", "FPT response envelope is invalid") from None


class FakeProvider:
    """Explicit deterministic test provider. Never an online fallback."""

    def __init__(self, completions: list[Completion | Exception]):
        self.completions = iter(completions)
        self.calls = 0

    def identity(self):
        return {
            "provider": "fake",
            "adapter_version": "fake-1",
            "model": "fake-test",
            "temperature": 0.0,
            "max_tokens": 4096,
        }

    def complete(self, messages, audit):
        self.calls += 1
        result = next(self.completions)
        if isinstance(result, Exception):
            raise result
        audit(
            {
                "provider": "fake",
                "started_at": utc_now(),
                "finished_at": utc_now(),
                "http_status": None,
                "error_code": None,
            },
            result.content.encode(),
        )
        return result
