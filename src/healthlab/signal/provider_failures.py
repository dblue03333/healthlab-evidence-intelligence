"""Conservative error metadata from saved HTTP evidence, without retrying requests."""

import json
import math
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


def failure_details(event, raw=None):
    status, code = event.get("http_status"), event.get("error_code")
    if code in {"provider_timeout", "provider_transport", "provider_interrupted"}:
        return {"kind": "unknown_outcome", "retryable": False}
    if status == 200 or (status is None and not code):
        return None
    delays, quota_ids = [], []
    try:
        body = json.loads(raw) if raw else {}
        details = body.get("error", {}).get("details", [])
        for item in details:
            if item.get("@type", "").endswith(".QuotaFailure"):
                quota_ids.extend(v.get("quotaId", "") for v in item.get("violations", []))
            if item.get("@type", "").endswith(".RetryInfo"):
                value = item.get("retryDelay", "")
                if isinstance(value, str) and re.fullmatch(r"\d+(?:\.\d+)?s", value):
                    delays.append(math.ceil(float(value[:-1])))
    except (ValueError, TypeError, AttributeError):
        pass  # Malformed/missing error data does not prove a retryable quota category.
    retry_after = event.get("retry_after")
    if isinstance(retry_after, str):
        if re.fullmatch(r"\d+", retry_after):
            delays.append(int(retry_after))
        else:
            try:
                start = datetime.fromisoformat(event["finished_at"])
                delays.append(
                    max(0, math.ceil((parsedate_to_datetime(retry_after) - start).total_seconds()))
                )
            except (ValueError, TypeError, KeyError, OverflowError):
                pass
    if status == 429:
        if any("PerDay" in value for value in quota_ids):
            kind, retryable = "daily_quota", True
        elif quota_ids and all("PerMinute" in value or "PerSecond" in value for value in quota_ids):
            kind, retryable = "rate_limit", True
        else:
            kind, retryable = "quota_or_rate_unknown", False
    elif status in {500, 502, 503, 504}:
        kind, retryable = "unavailable", True
    elif status in {401, 402, 403, 400, 404}:
        kind, retryable = "auth_or_config", False
    else:
        kind, retryable = "provider_error", False
    return dict(
        kind=kind,
        retryable=retryable,
        retry_delay_seconds=max(delays, default=0),
        quota_ids=quota_ids,
    )


def now():
    return datetime.now(UTC)
