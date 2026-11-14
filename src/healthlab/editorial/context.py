"""Literal per-fact context bindings; semantic adequacy still needs review.

fact-context-1 (gold-editorial-14) rejects the whole proposal on any invalid binding.
fact-context-2 (gold-editorial-15) repairs only surface differences that still resolve to
one exact source span inside the fact's attached support, and otherwise drops that facet
to null with a recorded reason. A dropped facet can never become release evidence.
"""

import re
import unicodedata
from typing import Literal

from pydantic import Field, StrictInt

from healthlab.models import Contract

FACETS = ("actor", "document", "population", "status")
MAX_CONTEXT_CHARS = 120  # A context label is a phrase, never a whole sentence.
_ELLIPSIS = re.compile(r"\.\.\.|…")


class ContextBinding(Contract):
    passage_id: str = Field(pattern=r"^P[1-9]\d*$")
    quote: str = Field(min_length=1, max_length=900)


class FactContext(Contract):
    actor: ContextBinding | None
    document: ContextBinding | None
    population: ContextBinding | None
    status: ContextBinding | None


class ResolvedBinding(ContextBinding):
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class ResolvedContext(Contract):
    actor: ResolvedBinding | None
    document: ResolvedBinding | None
    population: ResolvedBinding | None
    status: ResolvedBinding | None


ContextVerdict = Literal["supported", "missing", "not_applicable"]


class ContextAssessment(Contract):
    fact_index: StrictInt = Field(ge=0)
    actor: ContextVerdict
    document: ContextVerdict
    population: ContextVerdict
    status: ContextVerdict
    explanation: str = Field(min_length=1, max_length=1600)


class BindingRepair(Contract):
    fact_index: StrictInt = Field(ge=0)
    field: Literal["actor", "document", "population", "status", "date_link"]
    action: Literal["normalized", "rebound", "retyped", "dropped"]
    reason: str = Field(min_length=1, max_length=80)


def _fold(text):
    """Lower-case and collapse whitespace, mapping each folded char to its source index."""
    chars, index = [], []
    for i, char in enumerate(text):
        if char.isspace():
            if not chars or chars[-1] == " ":
                continue
            char = " "
        for folded in char.lower():
            chars.append(folded)
            index.append(i)
    return "".join(chars), index


def _occurrences(haystack, needle):
    found, start = [], haystack.find(needle)
    while start != -1:
        found.append(start)
        start = haystack.find(needle, start + 1)
    return found


def _folded_matches(quote, span):
    text = span["quote"]
    form = "NFC" if unicodedata.is_normalized("NFC", text) else "NFD"
    quote = unicodedata.normalize(form, quote)
    needle = _fold(quote.strip())[0].strip()
    folded, index = _fold(text)
    if not needle:
        return []
    return [(index[i], index[i + len(needle) - 1] + 1) for i in _occurrences(folded, needle)]


def repair_binding(binding, spans):
    """Return (resolved binding or None, action or None, reason or None)."""
    by_id = {span["passage_id"]: span for span in spans}
    named = by_id.get(binding["passage_id"])
    quote = binding["quote"]
    if named is not None and named["quote"].count(quote) == 1:
        start = named["quote"].index(quote)
        found, action = (named, start, start + len(quote)), None
    elif _ELLIPSIS.search(quote):
        return None, "dropped", "ellipsis_or_joined_fragments"
    else:
        local = _folded_matches(quote, named) if named is not None else []
        if len(local) == 1:
            found, action = (named, *local[0]), "normalized"
        elif len(local) > 1:
            return None, "dropped", "ambiguous_in_bound_passage"
        else:
            hits = [(span, *m) for span in spans for m in _folded_matches(quote, span)]
            if len(hits) != 1:
                reason = (
                    "ambiguous_in_attached_support" if hits else "not_found_in_attached_support"
                )
                return None, "dropped", reason
            found, action = hits[0], "rebound"
    span, start, end = found
    text = span["quote"][start:end]
    if len(text) > MAX_CONTEXT_CHARS:
        return None, "dropped", "overlong_not_a_phrase"
    resolved = {"passage_id": span["passage_id"], "quote": text, "start": span["start"] + start}
    resolved["end"] = resolved["start"] + len(text)
    reason = {"normalized": "case_or_whitespace", "rebound": "found_in_other_attached_passage"}
    return resolved, action, reason.get(action)


def repair_context(value, spans, fact_index):
    """fact-context-2: per-facet repair or drop; returns (context, repairs)."""
    context = FactContext.model_validate(value).model_dump()
    result, repairs = {}, []
    for facet, binding in context.items():
        result[facet], action, reason = (
            (None, None, None) if binding is None else repair_binding(binding, spans)
        )
        if action:
            repairs.append(
                {"fact_index": fact_index, "field": facet, "action": action, "reason": reason}
            )
    return result, repairs


def resolve_context(value, spans):
    """Bind only to attached support, never surrounding or sibling-fact text."""
    context = FactContext.model_validate(value).model_dump()
    by_id = {span["passage_id"]: span for span in spans}
    result = {}
    for facet, binding in context.items():
        if binding is None:
            result[facet] = None
            continue
        source = by_id.get(binding["passage_id"])
        if source is None or source["quote"].count(binding["quote"]) != 1:
            raise ValueError("Context binding must uniquely match attached supporting passage")
        start = source["start"] + source["quote"].index(binding["quote"])
        result[facet] = dict(binding, start=start, end=start + len(binding["quote"]))
    return result


def validate_context(value, spans, *, max_chars=None):
    resolved = ResolvedContext.model_validate(value).model_dump()
    if max_chars and any(b and len(b["quote"]) > max_chars for b in resolved.values()):
        raise ValueError("Context binding is longer than a context phrase")
    raw = {
        facet: (
            {k: v for k, v in binding.items() if k in {"passage_id", "quote"}} if binding else None
        )
        for facet, binding in resolved.items()
    }
    if resolved != resolve_context(raw, spans):
        raise ValueError("Context evidence differs from attached supporting passages")
    return resolved


_NUMERIC_DATE = re.compile(
    r"(?<![\d/.-])(\d{1,2})\s*[/.-]\s*(\d{1,2})(?:\s*[/.-]\s*(\d{4}))?(?!\d|\s*%)"
)
_WORDED_DATE = re.compile(r"ngày\s+(\d{1,2})\s+tháng\s+(\d{1,2})(?:\s*(?:năm|,)\s*(\d{4}))?", re.I)


def rationale_date_conflict(rationale, iso_date):
    """True when a link rationale names a calendar day other than the selected candidate.

    Catches an event placed on another day or a year the source never states (for example,
    a yearless ceremony linked to the end of an anniversary range). Conservative: any other
    day/month or year mentioned in the rationale withdraws the link.
    """
    year, month, day = (int(part) for part in iso_date.split("-"))
    for pattern in (_NUMERIC_DATE, _WORDED_DATE):
        for d, m, y in pattern.findall(rationale or ""):
            d, m = int(d), int(m)
            if not (1 <= d <= 31 and 1 <= m <= 12):
                continue
            if (d, m) != (day, month) or (y and int(y) != year):
                return True
    return False


def date_is_range_endpoint(text, iso_date, *, policy=None):
    """True when the literal date starts or ends a range such as an anniversary period."""
    from healthlab.editorial.dates import editorial_literal_dates, is_range_endpoint

    return any(
        found["date"] == iso_date and is_range_endpoint(text, found)
        for found in editorial_literal_dates(text or "", policy=policy)
    )
