"""Explicit OpenRouter routing for controlled experiments; no retry or fallback."""

import json

import httpx

from healthlab.models import IngestionError
from healthlab.provider import Completion, json_schema_format, schema_digest
from healthlab.pubmed import utc_now

BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider:
    def __init__(self, settings, client=None, *, cache_only=False, lazy=False):
        settings.validate_identity()
        if not cache_only and not lazy:
            settings.validate_online()
        self.settings = settings
        self.cache_only = cache_only
        self.client = None if cache_only else client
        self.owns_client = client is None and not cache_only

    def identity(self):
        return {
            "provider": "openrouter",
            "adapter_version": "openrouter-chat-1",
            "base_url": BASE_URL,
            "model": self.settings.model,
            "temperature": 0.0,
            "max_tokens": self.settings.max_tokens,
            "routing": {
                "only": [self.settings.provider_slug],
                "allow_fallbacks": False,
                "require_parameters": True,
            },
        }

    def close(self):
        if self.owns_client and self.client is not None:
            self.client.close()

    def complete(self, messages, audit):
        return self._complete(messages, audit)

    def complete_json(self, messages, audit, *, schema):
        return self._complete(messages, audit, response_format=json_schema_format(schema))

    def complete_json_object(self, messages, audit, *, schema):
        json_schema_format(schema)
        return self._complete(
            messages, audit, response_format={"type": "json_object"}, contract=schema
        )

    def _complete(self, messages, audit, *, response_format=None, contract=None):
        if self.cache_only:
            raise IngestionError("cache_miss", "OpenRouter cache-only mode never calls network")
        self.settings.validate_online()
        if self.client is None:
            self.client = httpx.Client(timeout=self.settings.timeout)
        key = self.settings.api_key.get_secret_value()
        event = {
            "provider": "openrouter",
            "started_at": utc_now(),
            "http_status": None,
            "error_code": None,
            "body_redacted": False,
            "requested_model": self.settings.model,
            "requested_provider": self.settings.provider_slug,
        }
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": self.settings.max_tokens,
            "stream": False,
            "provider": self.identity()["routing"],
        }
        if response_format is not None:
            payload["response_format"] = response_format
            event.update(
                response_format=response_format["type"],
                response_schema_sha256=schema_digest(
                    contract if contract is not None else response_format["json_schema"]["schema"]
                ),
            )
        try:
            response = self.client.post(
                BASE_URL + "/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=payload,
            )
        except (KeyboardInterrupt, SystemExit):
            event.update(error_code="provider_interrupted", finished_at=utc_now())
            audit(event, None)
            raise
        except httpx.TransportError as exc:
            code = (
                "provider_timeout"
                if isinstance(exc, httpx.TimeoutException)
                else "provider_transport"
            )
            event.update(error_code=code, finished_at=utc_now())
            audit(event, None)
            raise IngestionError(code, "OpenRouter request did not complete") from None
        raw = response.content
        for secret in {key.encode(), json.dumps(key)[1:-1].encode()}:
            if secret and secret in raw:
                raw = raw.replace(secret, b"[REDACTED_API_KEY]")
                event["body_redacted"] = True
        event.update(http_status=response.status_code, finished_at=utc_now())
        # Metadata is copied into the request audit as well as the raw envelope.
        try:
            body = json.loads(raw)
            if isinstance(body, dict):
                for field in ("id", "model", "provider"):
                    if isinstance(body.get(field), str):
                        event[
                            {
                                "id": "generation_id",
                                "model": "actual_model",
                                "provider": "actual_provider",
                            }[field]
                        ] = body[field]
                if isinstance(body.get("usage"), dict):
                    event["reported_usage"] = body["usage"]
        except ValueError:
            body = None
        audit(event, raw)
        if response.status_code != 200:
            code = {
                401: "provider_auth",
                403: "provider_auth",
                402: "provider_credit",
                429: "provider_quota_or_rate",
            }.get(response.status_code, "provider_http")
            error = body.get("error") if isinstance(body, dict) else None
            if (
                response.status_code == 403
                and isinstance(error, dict)
                and str(error.get("message", "")).startswith("Key limit exceeded")
            ):
                code = "provider_credit"
            raise IngestionError(
                code, f"OpenRouter HTTP status {response.status_code}; no fallback"
            )
        try:
            if not isinstance(body, dict) or body.get("error"):
                raise ValueError("API error")
            choice = body["choices"][0]
            content, model, finish = (
                choice["message"]["content"],
                body["model"],
                choice["finish_reason"],
            )
            usage = body.get("usage") or {}
            if content is None and finish == "length":
                content = ""
            if (
                not isinstance(content, str)
                or (not content and finish != "length")
                or not all(isinstance(v, str) and v for v in (model, finish))
                or not isinstance(usage, dict)
            ):
                raise ValueError("invalid response")
            if model != self.settings.model:
                raise IngestionError(
                    "provider_model_mismatch",
                    "Returned model differs from the explicitly requested model; inspect raw response",
                )
            return Completion(content, model, finish, usage)
        except (ValueError, KeyError, TypeError, IndexError):
            raise IngestionError(
                "provider_envelope", "OpenRouter response envelope is invalid"
            ) from None
