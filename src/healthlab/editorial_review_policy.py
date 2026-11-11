"""Typed review findings and conservative release rules, without model calls.

Audit notes never release or block a record. Legacy untyped issues are not guessed.
A clean review is an AI proposal, not a human approval or a factual guarantee.
"""

from typing import Literal

from pydantic import Field, StrictInt, model_validator

from healthlab.editorial_context import (
    FACETS,
    ContextAssessment,
    date_is_range_endpoint,
    validate_context,
)
from healthlab.models import Contract

REVIEW_VERSION = "editorial-review-2"
READ_REVIEW_VERSIONS = {
    REVIEW_VERSION,
    "editorial-review-3",
    "editorial-review-4",
    "editorial-review-5",
    "editorial-review-6",
}


def fact_date_supported(fact):
    """Literal-date check; the caller must validate context identity against source."""
    from healthlab.signal.triage_v5 import literal_dates

    quote = (fact.get("date_evidence") or {}).get("quote", fact["quote"])
    return fact.get("event_date") in {d["date"] for d in literal_dates(quote)}


class BlockingFinding(Contract):
    code: Literal[
        "topic_not_supported",
        "date_not_supported",
        "event_date_missing",
        "event_date_outside_window",
        "event_link_unverified",
        "origin_unverified",
        "status_overstated",
        "claim_not_supported",
        "contradiction",
        "other",
    ]
    explanation: str = Field(min_length=1, max_length=1600)
    fact_indexes: list[StrictInt] = Field(max_length=5)
    passage_ids: list[str] = Field(max_length=8)


class Review(Contract):
    review_version: Literal["editorial-review-2", "editorial-review-3", "editorial-review-4"]
    relevance: Literal["relevant", "irrelevant", "uncertain"]
    newsletter_eligible: bool
    topic_passage_ids: list[str] = Field(max_length=5)
    supported_fact_indexes: list[StrictInt] = Field(max_length=5)
    supported_date_fact_indexes: list[StrictInt] = Field(default_factory=list, max_length=5)
    blocking_findings: list[BlockingFinding] = Field(max_length=10)
    audit_notes: list[str] = Field(max_length=10)
    rationale: str = Field(min_length=1, max_length=1800)

    @model_validator(mode="after")
    def coherent(self):
        if self.newsletter_eligible and (
            self.relevance != "relevant"
            or not self.supported_fact_indexes
            or not self.topic_passage_ids
            or self.blocking_findings
        ):
            raise ValueError("Eligible review needs relevant supported evidence and no blockers")
        if len(self.supported_fact_indexes) != len(set(self.supported_fact_indexes)):
            raise ValueError("Repeated supported fact index")
        if len(self.supported_date_fact_indexes) != len(set(self.supported_date_fact_indexes)):
            raise ValueError("Repeated supported date fact index")
        if not set(self.supported_date_fact_indexes) <= set(self.supported_fact_indexes):
            raise ValueError("Date support requires the fact to be supported too")
        if self.supported_date_fact_indexes and self.review_version == "editorial-review-2":
            raise ValueError("Cross-passage date review requires version 3")
        return self


class CurrentBlockingFinding(BlockingFinding):
    code: Literal[
        "topic_not_supported",
        "event_date_missing",
        "event_date_outside_window",
        "event_link_unverified",
        "origin_unverified",
        "status_overstated",
        "claim_not_supported",
        "contradiction",
        "other",
    ]


class SemanticReview(Contract):
    """Current model output: meaning and evidence, without an eligibility vote."""

    review_version: Literal["editorial-review-4"]
    relevance: Literal["relevant", "irrelevant", "uncertain"]
    topic_passage_ids: list[str] = Field(max_length=5)
    supported_fact_indexes: list[StrictInt] = Field(max_length=5)
    supported_date_fact_indexes: list[StrictInt] = Field(max_length=5)
    blocking_findings: list[CurrentBlockingFinding] = Field(max_length=10)
    audit_notes: list[str] = Field(max_length=10)
    rationale: str = Field(min_length=1, max_length=1800)


class CitationFinding(CurrentBlockingFinding):
    code: Literal[
        "topic_not_supported",
        "event_date_missing",
        "event_date_outside_window",
        "event_link_unverified",
        "origin_unverified",
        "status_overstated",
        "claim_not_supported",
        "citation_incomplete",
        "contradiction",
        "other",
    ]


class CitationReview(SemanticReview):
    review_version: Literal["editorial-review-5"]
    supported_citation_fact_indexes: list[StrictInt] = Field(max_length=5)
    blocking_findings: list[CitationFinding] = Field(max_length=10)


class ReviewedCitations(Review):
    review_version: Literal["editorial-review-5"]
    supported_citation_fact_indexes: list[StrictInt] = Field(max_length=5)
    blocking_findings: list[CitationFinding] = Field(max_length=10)

    @model_validator(mode="after")
    def citation_indexes(self):
        indexes = self.supported_citation_fact_indexes
        if len(indexes) != len(set(indexes)) or not set(indexes) <= set(
            self.supported_fact_indexes
        ):
            raise ValueError("Citation support needs distinct source-supported fact indexes")
        return self


class ContextReview(CitationReview):
    review_version: Literal["editorial-review-6"]
    context_assessments: list[ContextAssessment] = Field(max_length=5)


class ReviewedContext(ReviewedCitations):
    review_version: Literal["editorial-review-6"]
    context_assessments: list[ContextAssessment] = Field(max_length=5)


def context_supported(proposal, review):
    assessments = {a["fact_index"]: a for a in review["context_assessments"]}
    return all(
        all(assessments[index][facet] != "missing" for facet in FACETS)
        for index in review["supported_fact_indexes"]
    )


def fact_date_checks(proposal, window):
    """Arithmetic only; literal identity never proves that a date is this event's.

    fact-context-2 adds a lexical range-endpoint flag; earlier contracts keep the exact
    historical payload so their review prompts stay byte-identical.
    """
    checks = [
        {
            "fact_index": index,
            "event_date": fact.get("event_date"),
            "literal_date_supported": fact_date_supported(fact),
            "inside_inclusive_window": (
                window["mindate"] <= fact["event_date"] <= window["maxdate"]
                if fact.get("event_date")
                else None
            ),
            "cross_passage_link_requires_review": bool(fact.get("date_evidence")),
            "proposed_reported_status": fact["reported_status"],
        }
        for index, fact in enumerate(proposal["facts"])
    ]
    if proposal.get("context_contract") == "fact-context-2":
        for check, fact in zip(checks, proposal["facts"], strict=True):
            text = (fact.get("date_evidence") or {}).get("quote", fact["quote"])
            check["date_is_range_endpoint"] = bool(
                fact.get("event_date") and date_is_range_endpoint(text, fact["event_date"])
            )
    return checks


def supported_development(proposal, review, window):
    return bool(window) and any(
        check["literal_date_supported"]
        and check["inside_inclusive_window"] is True
        and (
            not check["cross_passage_link_requires_review"]
            or check["fact_index"] in review["supported_date_fact_indexes"]
        )
        for check in fact_date_checks(proposal, window)
        if check["fact_index"] in review["supported_fact_indexes"]
    )


def validate_review(payload, proposal, passage_ids, window=None):
    contextual = payload.get("review_version") == "editorial-review-6"
    citations = payload.get("review_version") in {"editorial-review-5", "editorial-review-6"}
    current = payload.get("review_version") in {
        "editorial-review-4",
        "editorial-review-5",
        "editorial-review-6",
    }
    requires_context = proposal.get("context_contract") in {
        "fact-context-1",
        "fact-context-2",
    } or any("context" in f for f in proposal["facts"])
    if requires_context != contextual:
        raise ValueError("Context proposal requires matching context review")
    semantic_model = (
        ContextReview if contextual else CitationReview if citations else SemanticReview
    )
    review_model = ReviewedContext if contextual else ReviewedCitations if citations else Review
    supplied_eligibility = payload.get("newsletter_eligible")
    if current:
        if window is None:
            raise ValueError("Current review requires its reporting window")
        semantic = {k: v for k, v in payload.items() if k != "newsletter_eligible"}
        payload = dict(
            semantic_model.model_validate(semantic).model_dump(), newsletter_eligible=False
        )
    review = review_model.model_validate(payload)
    if citations and any(not fact.get("supporting_passages") for fact in proposal["facts"]):
        raise ValueError("Citation review requires explicit fact support passages")
    if contextual:
        assessments = {a.fact_index: a for a in review.context_assessments}
        if len(assessments) != len(review.context_assessments) or set(assessments) != set(
            range(len(proposal["facts"]))
        ):
            raise ValueError("Context review needs exactly one assessment per fact")
        # A verdict that contradicts the attached binding (supported on a null facet,
        # not_applicable on a bound one) withholds only that fact: it becomes "missing"
        # instead of rejecting the whole review, mirroring per-facet repair at classify.
        corrected = []
        for assessment in review.context_assessments:
            fact = proposal["facts"][assessment.fact_index]
            context = validate_context(fact.get("context"), fact["supporting_passages"])
            fixes = {
                facet: "missing"
                for facet in FACETS
                if (getattr(assessment, facet) == "supported" and context[facet] is None)
                or (getattr(assessment, facet) == "not_applicable" and context[facet] is not None)
            }
            corrected.append(assessment.model_copy(update=fixes))
        review = review.model_copy(update={"context_assessments": corrected})
    valid = set(passage_ids)
    if any(p not in valid for p in review.topic_passage_ids):
        raise ValueError("Unknown review topic passage")
    indexes = list(review.supported_fact_indexes)
    for finding in review.blocking_findings:
        indexes.extend(finding.fact_indexes)
        if any(p not in valid for p in finding.passage_ids):
            raise ValueError("Unknown blocking finding passage")
    if any(i < 0 or i >= len(proposal["facts"]) for i in indexes):
        raise ValueError("Invalid review fact index")
    if current:
        checks = fact_date_checks(proposal, window)
        for finding in review.blocking_findings:
            if citations and finding.code == "citation_incomplete":
                if not finding.fact_indexes:
                    raise ValueError("Incomplete citation needs concrete fact indexes")
                if set(finding.fact_indexes) & set(review.supported_citation_fact_indexes):
                    raise ValueError("Incomplete citation contradicts positive citation support")
            if finding.code not in {"event_date_missing", "event_date_outside_window"}:
                continue
            if not finding.fact_indexes:
                raise ValueError("Arithmetic date finding needs concrete fact indexes")
            for index in finding.fact_indexes:
                check = checks[index]
                if finding.code == "event_date_missing" and check["literal_date_supported"]:
                    raise ValueError("Missing-date finding contradicts the literal date")
                if finding.code == "event_date_outside_window" and (
                    not check["literal_date_supported"]
                    or check["inside_inclusive_window"] is not False
                ):
                    raise ValueError("Outside-window finding contradicts code date checks")
        value = review.model_dump()
        eligible = bool(
            review.relevance == "relevant"
            and review.topic_passage_ids
            and review.supported_fact_indexes
            and not review.blocking_findings
            and (
                not citations
                or set(review.supported_fact_indexes) <= set(review.supported_citation_fact_indexes)
            )
            and supported_development(proposal, value, window)
            and (not contextual or context_supported(proposal, value))
        )
        if supplied_eligibility is not None and supplied_eligibility is not eligible:
            raise ValueError("Saved current review eligibility differs from derived eligibility")
        value["newsletter_eligible"] = eligible
        return review_model.model_validate(value).model_dump()
    return review.model_dump()


def review_gate(proposal, review, passage_ids, window=None):
    if review is None:
        return {"eligible": False, "reasons": ["review_missing"]}
    if review.get("review_version") not in READ_REVIEW_VERSIONS:
        return {"eligible": False, "reasons": ["legacy_review_requires_explicit_assessment"]}
    if any("supporting_passages" in f for f in proposal["facts"]) and review.get(
        "review_version"
    ) not in {"editorial-review-5", "editorial-review-6"}:
        return {"eligible": False, "reasons": ["citation_review_required"]}
    try:
        value = validate_review(review, proposal, passage_ids, window)
    except ValueError:
        return {"eligible": False, "reasons": ["review_contract_invalid"]}
    reasons = []
    if not proposal["newsletter_candidate"]:
        reasons.append("primary_not_newsletter_candidate")
    if value["relevance"] != "relevant" or proposal["relevance"] != "relevant":
        reasons.append("topic_unresolved_or_disagreed")
    if not value["newsletter_eligible"]:
        reasons.append("review_not_newsletter_eligible")
    if value["blocking_findings"]:
        reasons.append("blocking_findings")
    if value["review_version"] in {"editorial-review-5", "editorial-review-6"} and not set(
        value["supported_fact_indexes"]
    ) <= set(value["supported_citation_fact_indexes"]):
        reasons.append("citation_support_incomplete")
    if value["review_version"] == "editorial-review-6" and not context_supported(proposal, value):
        reasons.append("context_support_incomplete")
    # An in-window date on a rejected/unsupported fact cannot release the rest.
    if not supported_development(proposal, value, window):
        reasons.append("supported_in_window_development_missing")
    return {"eligible": not reasons, "reasons": reasons}
