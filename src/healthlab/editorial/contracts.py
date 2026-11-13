"""Versioned editorial schemas and validation against frozen source text."""

from types import SimpleNamespace
from typing import Annotated, Literal

from pydantic import Field, model_validator

from healthlab.editorial_context import (
    MAX_CONTEXT_CHARS,
    BindingRepair,
    FactContext,
    ResolvedContext,
    rationale_date_conflict,
    repair_context,
    resolve_context,
    validate_context,
)
from healthlab.editorial_dates import DATE_POLICY, DATE_POLICY_VERSIONS, editorial_literal_dates
from healthlab.editorial_origin import footer_attributions
from healthlab.editorial_passages import POLICY as PASSAGE_POLICY
from healthlab.editorial_passages import editorial_passages
from healthlab.editorial_review_policy import (
    RELEASE_POLICY,
    RELEASE_POLICY_VERSIONS,
    fact_date_supported,
)
from healthlab.models import Contract
from healthlab.signal.triage import source_passages

VERSION = "gold-editorial-17"
# Finished known schemas stay readable; inference and mutations require the exact
# current runtime. Legacy review findings never silently acquire new semantics.
READ_VERSIONS = {
    "gold-editorial-1",
    "gold-editorial-2",
    "gold-editorial-3",
    "gold-editorial-4",
    "gold-editorial-5",
    "gold-editorial-6",
    "gold-editorial-7",
    "gold-editorial-8",
    "gold-editorial-9",
    "gold-editorial-10",
    "gold-editorial-11",
    "gold-editorial-12",
    "gold-editorial-13",
    "gold-editorial-14",
    "gold-editorial-15",
    "gold-editorial-16",
    VERSION,
}
# Context contract per version: v14 rejects invalid bindings; later versions repair or drop them.
CONTEXT_CONTRACTS = {
    "gold-editorial-14": "fact-context-1",
    "gold-editorial-15": "fact-context-2",
    "gold-editorial-16": "fact-context-2",
    "gold-editorial-17": "fact-context-2",
}
MAX_BODY_CHARS = 20_000


class DateEvidence(Contract):
    passage_id: str = Field(pattern=r"^P[1-9]\d*$")
    quote: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class EvidenceFact(Contract):
    statement: str = Field(min_length=1, max_length=900)
    quote: str = Field(min_length=12, max_length=1800)
    event_date: str | None
    date_evidence: DateEvidence | None = None
    date_link_rationale: str | None = Field(default=None, min_length=1, max_length=900)
    reported_status: Literal[
        "proposed",
        "announced",
        "adopted",
        "effective",
        "pilot",
        "operational",
        "published_report",
        "not_applicable",
        "unknown",
    ]


class ArticleProposal(Contract):
    relevance: Literal["relevant", "irrelevant", "uncertain"]
    topics: list[Literal["longevity", "aging_society"]] = Field(max_length=2)
    jurisdiction: Literal["vietnam", "international", "mixed", "unclear"]
    content_kind: Literal["new_development", "background", "unclear"]
    source_role: Literal["original", "repost", "unknown"]
    summary: str = Field(min_length=1, max_length=1400)
    rationale: str = Field(min_length=1, max_length=1600)
    relevance_quote: str = Field(min_length=12, max_length=1800)
    facts: list[EvidenceFact] = Field(max_length=5)
    uncertainties: list[str] = Field(max_length=6)

    @model_validator(mode="after")
    def coherent(self):
        if self.relevance == "relevant" and (not self.topics or not self.facts):
            raise ValueError("Relevant content needs a topic and evidence facts")
        if len(self.topics) != len(set(self.topics)):
            raise ValueError("Duplicate topics")
        return self


class IDFact(Contract):
    statement: str = Field(min_length=1, max_length=900)
    passage_id: str = Field(pattern=r"^P[1-9]\d*$")
    event_date_candidate_id: str | None
    date_context_passage_id: str | None = Field(pattern=r"^P[1-9]\d*$")
    date_link_rationale: str | None = Field(min_length=1, max_length=900)
    reported_status: Literal[
        "proposed",
        "announced",
        "adopted",
        "effective",
        "pilot",
        "operational",
        "published_report",
        "not_applicable",
        "unknown",
    ]


class IDArticleProposal(Contract):
    relevance: Literal["relevant", "irrelevant", "uncertain"]
    topics: list[Literal["longevity", "aging_society"]] = Field(max_length=2)
    jurisdiction: Literal["vietnam", "international", "mixed", "unclear"]
    content_kind: Literal["new_development", "background", "unclear"]
    source_role: Literal["original", "repost", "unknown"]
    summary: str = Field(min_length=1, max_length=1400)
    rationale: str = Field(min_length=1, max_length=1600)
    relevance_passage_id: str = Field(pattern=r"^P[1-9]\d*$")
    facts: list[IDFact] = Field(max_length=5)
    uncertainties: list[str] = Field(max_length=6)


class ReferenceIDFact(Contract):
    statement: str = Field(min_length=1, max_length=900)
    passage_id: str = Field(pattern=r"^P[1-9]\d*$")
    event_date_candidate_id: str | None
    date_link_rationale: str | None = Field(min_length=1, max_length=900)
    reported_status: Literal[
        "proposed",
        "announced",
        "adopted",
        "effective",
        "pilot",
        "operational",
        "published_report",
        "not_applicable",
        "unknown",
    ]


class ReferenceArticleProposal(IDArticleProposal):
    facts: list[ReferenceIDFact] = Field(max_length=5)


class SupportedIDFact(ReferenceIDFact):
    supporting_passage_ids: list[str] = Field(max_length=3)

    @model_validator(mode="after")
    def distinct_support(self):
        ids = [self.passage_id, *self.supporting_passage_ids]
        if len(ids) != len(set(ids)):
            raise ValueError("Repeated supporting passage ID")
        return self


class SupportedReferenceProposal(ReferenceArticleProposal):
    facts: list[SupportedIDFact] = Field(max_length=5)


class SupportedEvidenceFact(EvidenceFact):
    supporting_passages: list[DateEvidence] = Field(min_length=1, max_length=4)


class SupportedArticleProposal(ArticleProposal):
    facts: list[SupportedEvidenceFact] = Field(max_length=5)


class SamePassageDate(Contract):
    kind: Literal["same_passage"]
    candidate_id: str = Field(pattern=r"^D[1-9]\d*$")


class CrossPassageDate(Contract):
    kind: Literal["cross_passage"]
    candidate_id: str = Field(pattern=r"^D[1-9]\d*$")
    # Checked server-side: FPT rejects (HTTP 400) a schema with an unanchored `pattern`.
    rationale: str = Field(min_length=1, max_length=900)

    @model_validator(mode="after")
    def non_blank_rationale(self):
        if not self.rationale.strip():
            raise ValueError("Blank cross-passage date rationale")
        return self


class ContextIDFact(Contract):
    statement: str = Field(min_length=1, max_length=900)
    passage_id: str = Field(pattern=r"^P[1-9]\d*$")
    supporting_passage_ids: list[str] = Field(max_length=3)
    context: FactContext
    date_link: Annotated[SamePassageDate | CrossPassageDate, Field(discriminator="kind")] | None
    reported_status: Literal[
        "proposed",
        "announced",
        "adopted",
        "effective",
        "pilot",
        "operational",
        "published_report",
        "not_applicable",
        "unknown",
    ]


class ContextReferenceProposal(ReferenceArticleProposal):
    facts: list[ContextIDFact] = Field(max_length=5)


class ContextEvidenceFact(SupportedEvidenceFact):
    context: ResolvedContext


class ContextArticleProposal(ArticleProposal):
    facts: list[ContextEvidenceFact] = Field(max_length=5)


class RepairedContextArticleProposal(ContextArticleProposal):
    binding_repairs: list[BindingRepair] = Field(default_factory=list, max_length=25)


def passage_policy(version):
    if version in {
        "gold-editorial-11",
        "gold-editorial-12",
        "gold-editorial-13",
        *CONTEXT_CONTRACTS,
    }:
        return PASSAGE_POLICY
    if version in READ_VERSIONS:
        return "editorial-passages-1"
    raise ValueError("Unknown editorial passage runtime")


def passage_inputs(article, *, version=VERSION):
    units = (
        editorial_passages(article["body"])
        if passage_policy(version) == PASSAGE_POLICY
        else source_passages(SimpleNamespace(text=article["body"]))
    )
    dates = []
    policy = DATE_POLICY if version in DATE_POLICY_VERSIONS else None
    for unit in units:
        for value in editorial_literal_dates(
            unit["quote"], policy=policy, publication_date=article.get("publication_date")
        ):
            dates.append(
                dict(candidate_id=f"D{len(dates) + 1}", passage_id=unit["passage_id"], **value)
            )
    return units, dates


def resolve_ids(payload, article, *, version="gold-editorial-10"):
    value = IDArticleProposal.model_validate(payload)
    units, dates = passage_inputs(article, version=version)
    by_id = {p["passage_id"]: p for p in units}
    by_date = {d["candidate_id"]: d for d in dates}
    if value.relevance_passage_id not in by_id:
        raise ValueError("Unknown relevance passage")
    resolved = []
    for fact in value.facts:
        if fact.passage_id not in by_id:
            raise ValueError("Unknown fact passage")
        selected = by_date.get(fact.event_date_candidate_id)
        context = by_id.get(fact.date_context_passage_id)
        if fact.date_context_passage_id is not None and (
            selected is None
            or context is None
            or selected["passage_id"] != fact.date_context_passage_id
            or not fact.date_link_rationale
        ):
            raise ValueError("Date context needs a matching literal date and event link")
        if fact.event_date_candidate_id is not None and (
            selected is None or (selected["passage_id"] != fact.passage_id and context is None)
        ):
            raise ValueError("Date candidate does not belong to fact passage")
        resolved.append(
            dict(
                statement=fact.statement,
                quote=by_id[fact.passage_id]["quote"],
                event_date=selected["date"] if selected else None,
                date_evidence=context,
                date_link_rationale=fact.date_link_rationale,
                reported_status=fact.reported_status,
            )
        )
    raw = value.model_dump(exclude={"relevance_passage_id", "facts"})
    return dict(raw, relevance_quote=by_id[value.relevance_passage_id]["quote"], facts=resolved)


def resolve_reference_ids(payload, article, *, version=VERSION):
    """Derive the date passage from its immutable candidate; never infer event linkage.

    V1–7 ID payloads retain their original validator above. Current inference no
    longer asks the model to repeat a passage ID already determined by a date ID.
    A rationale for a same-passage/null date is retained as untrusted audit text;
    it cannot change the selected date or create cross-passage evidence.
    """
    if version in CONTEXT_CONTRACTS:
        # v15 repairs/drops a bad facet or date link per fact; v14 rejects the proposal.
        repair = CONTEXT_CONTRACTS[version] == "fact-context-2"
        value = ContextReferenceProposal.model_validate(payload).model_dump()
        units, dates = passage_inputs(article, version=version)
        by_id = {u["passage_id"]: u for u in units}
        by_date = {d["candidate_id"]: d for d in dates}
        contexts, repairs = [], []
        for index, fact in enumerate(value["facts"]):
            ids = [fact["passage_id"], *fact["supporting_passage_ids"]]
            if len(set(ids)) != len(ids) or any(pid not in by_id for pid in ids):
                raise ValueError("Unknown or repeated supporting passage")
            spans = [by_id[pid] for pid in ids]
            if repair:
                context, fixed = repair_context(fact.pop("context"), spans, index)
                contexts.append(context)
                repairs.extend(fixed)
            else:
                contexts.append(resolve_context(fact.pop("context"), spans))
            link = fact.pop("date_link")
            selected = by_date.get(link["candidate_id"]) if link else None
            if repair and link:
                cross = selected is not None and selected["passage_id"] != fact["passage_id"]
                dropped = (
                    "unknown_candidate"
                    if selected is None
                    else "rationale_names_other_date"
                    if rationale_date_conflict(link.get("rationale"), selected["date"])
                    else "cross_passage_without_rationale"
                    if cross and link["kind"] == "same_passage"
                    else None
                )
                if dropped:
                    repairs.append(
                        dict(fact_index=index, field="date_link", action="dropped", reason=dropped)
                    )
                    link = selected = None
                elif not cross and link["kind"] == "cross_passage":
                    repairs.append(
                        dict(
                            fact_index=index,
                            field="date_link",
                            action="retyped",
                            reason="candidate_in_primary_passage",
                        )
                    )
                    link = dict(link, kind="same_passage")
            if link and selected is None:
                raise ValueError("Unknown event date candidate")
            if link and (link["kind"] == "same_passage") != (
                selected["passage_id"] == fact["passage_id"]
            ):
                raise ValueError("Date link kind differs from selected passage")
            fact["event_date_candidate_id"] = link["candidate_id"] if link else None
            fact["date_link_rationale"] = link.get("rationale") if link else None
        result = _resolve_reference_facts(value, article, version=version, supported=True)
        for fact, context in zip(result["facts"], contexts, strict=True):
            fact["context"] = context
        if repair:
            result["binding_repairs"] = repairs
        return result
    return _resolve_reference_facts(
        payload, article, version=version, supported=version == "gold-editorial-13"
    )


def _resolve_reference_facts(payload, article, *, version, supported):
    model = SupportedReferenceProposal if supported else ReferenceArticleProposal
    value = model.model_validate(payload).model_dump()
    units, dates = passage_inputs(article, version=version)
    by_id = {u["passage_id"]: u for u in units}
    supports = []
    if supported:
        for fact in value["facts"]:
            ids = [fact["passage_id"], *fact.pop("supporting_passage_ids")]
            if any(pid not in by_id for pid in ids):
                raise ValueError("Unknown supporting passage")
            supports.append([by_id[pid] for pid in ids])
    by_date = {d["candidate_id"]: d for d in dates}
    for fact in value["facts"]:
        date_id = fact["event_date_candidate_id"]
        selected = by_date.get(date_id)
        if date_id is not None and selected is None:
            raise ValueError("Unknown event date candidate")
        linked = selected and selected["passage_id"] != fact["passage_id"]
        if linked and not fact["date_link_rationale"]:
            raise ValueError("Cross-passage date needs an explicit same-event rationale")
        fact["date_context_passage_id"] = selected["passage_id"] if linked else None
    result = resolve_ids(value, article, version=version)
    if supported:
        for fact, spans in zip(result["facts"], supports, strict=True):
            fact["supporting_passages"] = spans
    return result


class DraftClaim(Contract):
    text: str = Field(min_length=1, max_length=1200)
    evidence_ids: list[str] = Field(min_length=1, max_length=5)


class DraftSection(Contract):
    title: str = Field(min_length=1, max_length=200)
    facts: list[DraftClaim] = Field(min_length=1, max_length=5)
    interpretation: str = Field(min_length=1, max_length=1400)
    uncertainty: str = Field(min_length=1, max_length=1000)


class EditorialDraft(Contract):
    title: str = Field(min_length=1, max_length=200)
    sections: list[DraftSection] = Field(min_length=1, max_length=8)


def canonical_proposal(value, article, window, *, version=VERSION):
    contract = CONTEXT_CONTRACTS.get(version)
    date_policy = DATE_POLICY if version in DATE_POLICY_VERSIONS else None
    model = (
        RepairedContextArticleProposal
        if contract == "fact-context-2"
        else ContextArticleProposal
        if contract
        else SupportedArticleProposal
        if version == "gold-editorial-13"
        else ArticleProposal
    )
    p = model.model_validate(value)
    body = article["body"]
    publication_date = article.get("publication_date") if date_policy else None
    if p.relevance_quote not in body:
        raise ValueError("Relevance quote is not literal article text")
    resolved, holds = [], []
    units, _ = passage_inputs(article, version=version)
    by_id = {u["passage_id"]: u for u in units}
    for index, fact in enumerate(p.facts):
        if fact.quote not in body:
            raise ValueError("Fact quote is not literal article text")
        if fact.date_evidence:
            context = fact.date_evidence
            if not any(context.model_dump() == unit for unit in units):
                raise ValueError("Date evidence differs from frozen source passage")
            if not fact.date_link_rationale or fact.event_date is None:
                raise ValueError("Date context lacks an event date or linkage rationale")
        if fact.event_date is not None and not fact_date_supported(
            fact.model_dump(), policy=date_policy, publication_date=publication_date
        ):
            holds.append(f"fact_{index}:event_date_not_literal_in_quote")
        if isinstance(fact, SupportedEvidenceFact):
            spans = [s.model_dump() for s in fact.supporting_passages]
            ids = [s["passage_id"] for s in spans]
            if len(ids) != len(set(ids)) or any(by_id.get(s["passage_id"]) != s for s in spans):
                raise ValueError("Supporting evidence differs from frozen source passages")
            if spans[0]["quote"] != fact.quote:
                raise ValueError("Primary quote differs from first supporting passage")
            if isinstance(fact, ContextEvidenceFact):
                validate_context(
                    fact.context.model_dump(),
                    spans,
                    max_chars=MAX_CONTEXT_CHARS if contract == "fact-context-2" else None,
                )
            start = spans[0]["start"]
        else:
            start = body.index(fact.quote)
        resolved.append(dict(fact.model_dump(), start=start, end=start + len(fact.quote)))
    in_window = any(
        f.event_date
        and fact_date_supported(
            f.model_dump(), policy=date_policy, publication_date=publication_date
        )
        and window["mindate"] <= f.event_date <= window["maxdate"]
        for f in p.facts
    )
    # A footer attribution forces origin review regardless of model confidence.
    # Under the current date policy a footer naming only trusted publishers is accepted as a
    # repost from a known outlet; any other footer still forces origin review.
    has_footer, trusted_hosts, all_trusted = footer_attributions(body)
    trusted_repost = bool(date_policy and has_footer and all_trusted and trusted_hosts)
    external_footer = has_footer and not trusted_repost
    candidate = (
        p.relevance == "relevant"
        and p.jurisdiction == "vietnam"
        and p.content_kind == "new_development"
        and (p.source_role == "original" or (trusted_repost and p.source_role == "repost"))
        and in_window
        and not holds
        and not external_footer
    )
    result = dict(
        p.model_dump(),
        resolved_facts=resolved,
        technical_holds=holds,
        origin_footer_review=external_footer,
        newsletter_candidate=candidate,
        semantic_support="not_human_reviewed",
    )
    if contract:
        result["context_contract"] = contract
    if date_policy:
        result["date_policy"] = date_policy
        result["publication_date"] = publication_date
        result["trusted_origin_hosts"] = trusted_hosts if trusted_repost else []
    if version in RELEASE_POLICY_VERSIONS:
        result["release_policy"] = RELEASE_POLICY
    return result
