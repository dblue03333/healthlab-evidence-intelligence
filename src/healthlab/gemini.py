"""Opt-in Google Gemini text adapter with native JSON schema and immutable audits.

One non-streaming REST call, fixed Google host, no tools, retry or fallback. Billing
tier belongs to the Google project; an API key cannot certify Free Tier status.
"""

import json

import httpx

from healthlab.models import IngestionError
from healthlab.provider import Completion, json_schema_format, schema_digest
from healthlab.pubmed import utc_now

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


def _contents(messages):
    system, contents = [], []
    for message in messages:
        role, content = message.get("role"), message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Gemini adapter accepts nonempty text messages only")
        if role == "system" and not contents:
            system.append({"text": content})
        elif role in {"user", "assistant"}:
            contents.append(
                {"role": "model" if role == "assistant" else "user", "parts": [{"text": content}]}
            )
        else:
            raise ValueError("Unsupported message role or system instruction position")
    if not contents or contents[0]["role"] != "user":
        raise ValueError("Gemini request needs an initial user message")
    payload = {"contents": contents}
    if system:
        payload["systemInstruction"] = {"parts": system}
    return payload


def _usage(native):
    if not isinstance(native, dict):
        raise ValueError("Invalid usage metadata")
    for field in (
        "promptTokenCount",
        "candidatesTokenCount",
        "thoughtsTokenCount",
        "totalTokenCount",
        "cachedContentTokenCount",
    ):
        if field in native and (type(native[field]) is not int or native[field] < 0):
            raise ValueError("Invalid token count")
    usage = {"gemini_usage_metadata": native}
    for original, normalized in (
        ("promptTokenCount", "prompt_tokens"),
        ("totalTokenCount", "total_tokens"),
        ("thoughtsTokenCount", "reasoning_tokens"),
        ("candidatesTokenCount", "output_text_tokens"),
        ("cachedContentTokenCount", "cached_input_tokens"),
    ):
        if original in native:
            usage[normalized] = native[original]
    if "candidatesTokenCount" in native or "thoughtsTokenCount" in native:
        usage["completion_tokens"] = native.get("candidatesTokenCount", 0) + native.get(
            "thoughtsTokenCount", 0
        )
    return usage


class GeminiProvider:
    def __init__(self, settings, client=None, *, cache_only=False, lazy=False):
        if not cache_only and not lazy:
            settings.validate_online()
        self.settings = settings
        self.cache_only = cache_only
        self.client = None if cache_only else client
        self.owns_client = client is None and not cache_only

    def identity(self):
        return {
            "provider": "gemini",
            "adapter_version": "gemini-generate-content-1",
            "base_url": BASE_URL,
            "model": self.settings.model,
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
            "thinking_level": self.settings.thinking_level,
            "include_thoughts": False,
        }

    def close(self):
        if self.owns_client and self.client is not None:
            self.client.close()

    def complete(self, messages, audit):
        return self._complete(messages, audit)

    def complete_json(self, messages, audit, *, schema):
        json_schema_format(schema)  # Same closed-object precondition as the other adapters.
        return self._complete(messages, audit, schema=schema, mode="json_schema")

    def complete_json_object(self, messages, audit, *, schema):
        json_schema_format(schema)
        return self._complete(messages, audit, schema=schema, mode="json_object")

    def _complete(self, messages, audit, *, schema=None, mode="text"):
        if self.cache_only:
            raise IngestionError("cache_miss", "Gemini cache-only mode never calls network")
        self.settings.validate_online()
        payload = _contents(messages)
        config = {
            "candidateCount": 1,
            "temperature": self.settings.temperature,
            "maxOutputTokens": self.settings.max_tokens,
            "thinkingConfig": {
                "thinkingLevel": self.settings.thinking_level,
                "includeThoughts": False,
            },
        }
        if mode != "text":
            # responseFormat.text uses a protobuf enum, unlike the legacy
            # responseMimeType string. Some guide examples conflate the two.
            text_format = {"mimeType": "APPLICATION_JSON"}
            if mode == "json_schema":
                text_format["schema"] = schema
            config["responseFormat"] = {"text": text_format}
        payload["generationConfig"] = config
        if self.client is None:
            self.client = httpx.Client(timeout=self.settings.timeout, follow_redirects=False)
        key = self.settings.api_key.get_secret_value()
        event = dict(
            provider="gemini",
            adapter_version=self.identity()["adapter_version"],
            requested_model=self.settings.model,
            started_at=utc_now(),
            http_status=None,
            error_code=None,
            body_redacted=False,
            response_format=mode,
        )
        if schema is not None:
            event["response_schema_sha256"] = schema_digest(schema)
        try:
            response = self.client.post(
                f"{BASE_URL}/models/{self.settings.model}:generateContent",
                headers={"x-goog-api-key": key},
                json=payload,
                follow_redirects=False,
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
            raise IngestionError(code, "Gemini request did not complete") from None
        raw = response.content
        for secret in {key.encode(), json.dumps(key)[1:-1].encode()}:
            if secret and secret in raw:
                raw = raw.replace(secret, b"[REDACTED_API_KEY]")
                event["body_redacted"] = True
        event.update(http_status=response.status_code, finished_at=utc_now())
        if response.headers.get("retry-after"):
            event["retry_after"] = response.headers["retry-after"]
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if isinstance(body, dict):
            for original, field in (
                ("modelVersion", "actual_model"),
                ("responseId", "generation_id"),
            ):
                if isinstance(body.get(original), str):
                    event[field] = body[original]
            if isinstance(body.get("usageMetadata"), dict):
                event["reported_usage"] = body["usageMetadata"]
        audit(event, raw)  # Preserve HTTP errors and incomplete candidates before parsing.
        if response.status_code != 200:
            code = {
                400: "provider_request",
                401: "provider_auth",
                402: "provider_credit",
                403: "provider_auth",
                404: "provider_model_unavailable",
                429: "provider_quota_or_rate",
            }.get(response.status_code, "provider_http")
            raise IngestionError(code, f"Gemini HTTP status {response.status_code}; no fallback")
        try:
            if not isinstance(body, dict) or body.get("error"):
                raise ValueError("Invalid envelope")
            usage = _usage(body.get("usageMetadata", {}))
            blocked = body.get("promptFeedback", {}).get("blockReason")
            if blocked and blocked != "BLOCK_REASON_UNSPECIFIED":
                raise IngestionError(
                    "provider_blocked", "Gemini blocked this prompt; inspect audit"
                )
            model = body["modelVersion"]
            if not isinstance(model, str) or not model:
                raise ValueError("Missing model version")
            if model != self.settings.model:
                raise IngestionError(
                    "provider_model_mismatch", "Gemini returned a different model; inspect audit"
                )
            candidates = body["candidates"]
            if not isinstance(candidates, list) or len(candidates) != 1:
                raise ValueError("Expected exactly one candidate")
            candidate = candidates[0]
            finish = candidate["finishReason"]
            if not isinstance(finish, str) or not finish:
                raise ValueError("Missing finish reason")
            content = candidate.get("content", {})
            if content.get("role", "model") != "model":
                raise ValueError("Unexpected candidate role")
            parts = content.get("parts", [])
            if not isinstance(parts, list):
                raise ValueError("Invalid text parts")
            texts = []
            for part in parts:
                if not isinstance(part, dict) or not isinstance(part.get("thought", False), bool):
                    raise ValueError("Invalid text part")
                if part.get("thought") is True:
                    continue
                if not isinstance(part.get("text"), str) or any(
                    name in part
                    for name in ("functionCall", "functionResponse", "inlineData", "fileData")
                ):
                    raise ValueError("Non-text output is unsupported")
                texts.append(part["text"])
            text = "".join(texts)
            normalized_finish = (
                "stop"
                if finish == "STOP"
                else "length"
                if finish == "MAX_TOKENS"
                else finish.lower()
            )
            if not text.strip() and normalized_finish == "stop":
                raise ValueError("Empty completed answer")
            return Completion(text, model, normalized_finish, usage)
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):
            raise IngestionError(
                "provider_envelope", "Gemini response envelope is invalid"
            ) from None
