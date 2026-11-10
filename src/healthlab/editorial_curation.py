"""Validate explicit source-based agent/human curation without inferring labels.

A reviewer supplies decisions and evidence. This module verifies identity/offsets,
state invariants and date citations; semantic support remains the reviewer's task.
"""

from typing import Literal

from pydantic import Field, model_validator

from healthlab.models import Contract
from healthlab.signal.triage_v5 import literal_dates


class SourceSpan(Contract):
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1)


class CuratedDateReference(SourceSpan):
    source_id: str = Field(min_length=1)
    raw_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    link_rationale: str = Field(min_length=1)


class CuratedFact(Contract):
    statement: str = Field(min_length=1)
    evidence: SourceSpan
    event_date: str | None
    reported_status: str = Field(min_length=1)
    date_reference: CuratedDateReference | None = None


class CurationDecision(Contract):
    document_id: str
    snapshot_id: str
    content_version: str
    disposition: Literal[
        "newsletter_ready",
        "background_only",
        "retain_for_evidence_review",
        "outside_scope",
        "needs_review",
        "not_reassessed",
    ]
    topics: list[Literal["aging_society", "longevity"]]
    actor_type: Literal["agent", "human"]
    actor: str = Field(min_length=1)
    review_depth: Literal["full_source", "source_passage_and_model_audit", "not_reassessed"]
    rationale: str = Field(min_length=1)
    evidence: list[SourceSpan]
    facts: list[CuratedFact]
    blocking_reasons: list[str]
    original_classification_failure: str | None
    human_approval: Literal[False] = False

    @model_validator(mode="after")
    def ready(self):
        if self.disposition == "newsletter_ready" and (
            not self.topics
            or self.blocking_reasons
            or not self.facts
            or self.review_depth != "full_source"
        ):
            raise ValueError("Ready curation needs full source review, facts and no blockers")
        if self.disposition == "outside_scope" and self.facts:
            raise ValueError("Out-of-scope facts cannot feed writing")
        if self.disposition == "background_only" and (
            self.review_depth != "full_source"
            or not self.topics
            or not self.evidence
            or self.facts
            or not self.blocking_reasons
        ):
            raise ValueError(
                "Background needs full review, topic evidence and release restrictions"
            )
        return self


def validate_curation(payload, row, window, *, date_sources=None):
    decision = CurationDecision.model_validate(payload)
    for field in ("document_id", "snapshot_id", "content_version"):
        if getattr(decision, field) != row[field]:
            raise ValueError("Curation snapshot/content identity differs")
    spans = list(decision.evidence) + [f.evidence for f in decision.facts]
    for span in spans:
        if row["body"][span.start : span.end] != span.quote:
            raise ValueError("Curation evidence differs from frozen body")
    for fact in decision.facts:
        date_quote = fact.evidence.quote
        if fact.date_reference:
            ref = fact.date_reference
            source = (date_sources or {}).get(ref.source_id)
            if (
                not source
                or source["raw_sha256"] != ref.raw_sha256
                or source["body"][ref.start : ref.end] != ref.quote
                or not fact.event_date
            ):
                raise ValueError("Curated date reference differs from captured source")
            date_quote = ref.quote
        if fact.event_date and fact.event_date not in {
            d["date"] for d in literal_dates(date_quote)
        }:
            raise ValueError("Curated date lacks literal evidence")
    if decision.disposition == "newsletter_ready" and not any(
        f.event_date and window["mindate"] <= f.event_date <= window["maxdate"]
        for f in decision.facts
    ):
        raise ValueError("Ready curation lacks a supported in-window development")
    return decision.model_dump()
