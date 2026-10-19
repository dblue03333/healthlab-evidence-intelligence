"""Source HTTP clients use native trust without changing global SSL behavior."""

import ssl
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
import truststore

from healthlab.models import IngestionError


@dataclass(frozen=True)
class FetchPolicy:
    max_attempts: int = 3
    request_interval: float = 0.25
    backoff_seconds: float = 0.5
    max_retry_delay: float = 10.0

    def __post_init__(self):
        if not 1 <= self.max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        if not 0 <= self.request_interval <= 10:
            raise ValueError("request_interval must be between 0 and 10 seconds")
        if not 0 <= self.backoff_seconds <= self.max_retry_delay <= 30:
            raise ValueError("Invalid retry delay bounds")

    def as_dict(self):
        return asdict(self)


class FetchFailure(IngestionError):
    def __init__(self, code, *, retryable=False):
        super().__init__(code, "HTML request failed; inspect saved request audit")
        self.retryable = retryable


class HTMLFetcher:
    """Bounded GET retries; callers persist audit before and after each attempt.

    Attempts include starts interrupted by process failure. Resume cannot reset
    the request budget. Only transient transport/status errors are retried.
    """

    def __init__(self, client, store, policy=None, *, sleep=None, max_bytes=2_000_000):
        self.client = client
        self.store = store
        self.policy = policy or FetchPolicy()
        self.sleep = sleep or time.sleep
        if not 1000 <= max_bytes <= 8_000_000:
            raise ValueError("Response limit must be between 1000 and 8000000 bytes")
        self.max_bytes = max_bytes

    def fetch(self, url, checkpoint, save, *, response_format="html"):
        if response_format not in {"html", "json"}:
            raise ValueError("Unsupported response format")
        if url in checkpoint["responses"]:
            digest = checkpoint["responses"][url]["raw_sha256"]
            return self.store.read(digest), digest
        attempts = checkpoint["http_attempts"]
        previous = [a for a in attempts if a["url"] == url]
        used = len(previous)
        if used >= self.policy.max_attempts:
            raise FetchFailure("html_attempts_exhausted", retryable=True)
        if previous and previous[-1]["status"] == "failed":
            last = previous[-1]
            if not last["retryable"]:
                raise FetchFailure(last["error_code"])
            remaining = max(0, last.get("retry_not_before", 0) - time.time())
            if remaining > self.policy.max_retry_delay:
                raise FetchFailure(last["error_code"], retryable=True)
            self.sleep(remaining)
        for number in range(used + 1, self.policy.max_attempts + 1):
            self.sleep(self.policy.request_interval)
            event = {"url": url, "attempt": number, "status": "started"}
            if response_format == "json":
                event["response_format"] = "json"
            attempts.append(event)
            save()
            retry_after = None
            try:
                with self.client.stream("GET", url, follow_redirects=False) as response:
                    event["http_status"] = response.status_code
                    if response.status_code != 200:
                        retryable = response.status_code in {408, 429, 500, 502, 503, 504}
                        retry_after = response.headers.get("retry-after")
                        raise FetchFailure("html_http", retryable=retryable)
                    expected_type = "text/html" if response_format == "html" else "application/json"
                    if expected_type not in response.headers.get("content-type", "").lower():
                        raise FetchFailure("html_content_type")
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > self.max_bytes:
                            raise FetchFailure("html_limit")
                digest = self.store.put(bytes(raw))
            except (httpx.TransportError, FetchFailure) as exc:
                retryable = (
                    exc.retryable
                    if isinstance(exc, FetchFailure)
                    else isinstance(
                        exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
                    )
                )
                event.update(
                    status="failed",
                    error_code=exc.code if isinstance(exc, FetchFailure) else type(exc).__name__,
                    retryable=retryable,
                )
                delay = self.policy.backoff_seconds * 2 ** (number - 1)
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        try:
                            delay = max(
                                delay,
                                (
                                    parsedate_to_datetime(retry_after) - datetime.now(UTC)
                                ).total_seconds(),
                            )
                        except (ValueError, TypeError, OverflowError):
                            pass
                event["retry_delay_seconds"] = delay
                event["retry_not_before"] = time.time() + delay
                save()
                # Do not retry sooner than a server's Retry-After instruction.
                if (
                    not retryable
                    or number == self.policy.max_attempts
                    or delay > self.policy.max_retry_delay
                ):
                    raise FetchFailure(event["error_code"], retryable=retryable) from exc
                self.sleep(delay)
            else:
                event.update(status="completed", raw_sha256=digest, response_bytes=len(raw))
                checkpoint["responses"][url] = {"raw_sha256": digest}
                if response_format == "json":
                    headers = {
                        key: response.headers[key]
                        for key in ("x-wp-total", "x-wp-totalpages")
                        if key in response.headers
                    }
                    checkpoint["responses"][url]["headers"] = headers
                    event["response_headers"] = headers
                save()
                return bytes(raw), digest


def source_client():
    return httpx.Client(
        verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        timeout=20,
        follow_redirects=False,
    )
