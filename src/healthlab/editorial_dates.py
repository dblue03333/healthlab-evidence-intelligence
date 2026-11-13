"""Versioned literal dates and lexical clues for editorial date review."""

import re
from datetime import date

from healthlab.signal.triage_v5 import literal_dates

DATE_POLICY = "editorial-dates-2"
DATE_POLICY_VERSIONS = {"gold-editorial-16", "gold-editorial-17", "gold-editorial-18"}
YEAR_INFERENCE_MAX_DAYS = 30
INFERRED_YEAR_BASIS = "inferred_from_publication_year"
_DAY_FIRST_HYPHEN = re.compile(r"(?<![\w/.-])(\d{1,2})-(\d{1,2})-(20\d{2})(?![\w/-]|\.\d)")
_RANGE = r"\s*(?:[-–—]|đến|tới)\s*"
_DATE_PREFIX = r"(?:(?:ngày|sáng|trưa|chiều|tối|đêm|hôm)\s+)?"
_AS_OF = re.compile(r"\b(?:tính\s+(?:đến|tới)|đến\s+hết)\s*(?:ngày\s+)?$", re.I)
_DEADLINE = re.compile(
    r"\b(?:hạn(?:\s+(?:chót|cuối))?|chậm\s+nhất)\s*(?:(?:là|vào)\s+)?(?:ngày\s+)?$", re.I
)
_YEARLESS = re.compile(
    r"\b(?:ngày|sáng|trưa|chiều|tối|đêm|hôm)\s+(\d{1,2})(?:\s*[/\-]\s*|\s+tháng\s+)"
    r"(\d{1,2})(?!\d|\s*(?:[/%]|[.-]\s*\d|năm\s+\d)|,\s*\d{4})",
    re.I,
)
_RELATIVE = {
    "as_of_without_date": re.compile(r"\b(?:tính\s+)?đến\s+nay\b", re.I),
    "relative_date": re.compile(r"\b(?:mới\s+đây|vừa\s+(?:qua|ký|ban\s+hành))\b", re.I),
}


def editorial_literal_dates(text, *, policy=None, publication_date=None):
    dates = literal_dates(text)
    if policy != DATE_POLICY:
        return dates
    for match in _DAY_FIRST_HYPHEN.finditer(text):
        day, month, year = map(int, match.groups())
        try:
            value = date(year, month, day)
        except ValueError:
            continue
        if any(match.start() < item["end"] and item["start"] < match.end() for item in dates):
            continue
        dates.append(
            dict(date=value.isoformat(), start=match.start(), end=match.end(), quote=match[0])
        )
    for item in resolve_yearless_dates(text, publication_date):
        if not any(item["start"] < d["end"] and d["start"] < item["end"] for d in dates):
            dates.append(item)
    return sorted(dates, key=lambda item: item["start"])


def resolve_yearless_dates(text, publication_date):
    """Resolve "sáng 2/11" to a full date with the document's publication year, in code.

    The model never infers a year. The result is never after the publication date (a later
    day/month belongs to the previous year) and at most YEAR_INFERENCE_MAX_DAYS before it.
    Deadlines, as-of dates and range endpoints are not event dates and stay unresolved.
    """
    try:
        published = date.fromisoformat(publication_date)
    except (TypeError, ValueError):
        return []
    found = []
    for match in _YEARLESS.finditer(text):
        day, month = map(int, match.groups())
        value = None
        for year in (published.year, published.year - 1):
            try:
                candidate = date(year, month, day)
            except ValueError:
                continue
            if candidate <= published:
                value = candidate
                break
        if value is None or (published - value).days > YEAR_INFERENCE_MAX_DAYS:
            continue
        item = dict(
            date=value.isoformat(),
            start=match.start(),
            end=match.end(),
            quote=match[0],
            basis=INFERRED_YEAR_BASIS,
        )
        if date_role(text, item) in {"as_of", "deadline", "range_endpoint"}:
            continue
        found.append(item)
    return found


def is_range_endpoint(text, item):
    before, after = text[: item["start"]], text[item["end"] :]
    return bool(
        re.search(r"\d" + _RANGE + "$", before, re.I)
        or re.match(_RANGE + _DATE_PREFIX + r"\d", after, re.I)
    )


def date_role(text, item):
    before = text[: item["start"]]
    if _AS_OF.search(before):
        return "as_of"
    if _DEADLINE.search(before):
        return "deadline"
    if is_range_endpoint(text, item):
        return "range_endpoint"
    return "unverified"


def incomplete_date_mentions(text):
    for match in _YEARLESS.finditer(text):
        day, month = map(int, match.groups())
        try:
            date(2000, month, day)  # Allow a possible leap day without asserting a year.
        except ValueError:
            continue
        yield dict(kind="yearless", quote=match[0], start=match.start(), end=match.end())
    for kind, pattern in _RELATIVE.items():
        for match in pattern.finditer(text):
            yield dict(kind=kind, quote=match[0], start=match.start(), end=match.end())
