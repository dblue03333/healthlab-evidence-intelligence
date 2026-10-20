"""Evidence-backed agent proposals, kept distinct from human editorial labels."""

import json
from datetime import date
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from healthlab.models import Contract, IngestionError
from healthlab.pubmed import utc_now
from healthlab.signal.contracts import public_url
from healthlab.signal.html_tree import ArticleTree
from healthlab.signal.storage import collection_storage
from healthlab.signal.triage import Digest, Nonblank, TriageDecision, source_attribution
from healthlab.store import json_bytes

VERIFICATION_VERSION = "signal-agent-verification-2"


class ExternalCitation(Contract):
    capture_run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    url: str
    raw_sha256: Digest
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=2000)
    purpose: Literal["original_match", "publication", "event_date", "status", "context"]

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        return public_url(value)


class AgentAssessment(Contract):
    decision: TriageDecision
    event_date: date | None
    findings: Nonblank
    evidence: list[ExternalCitation] = Field(default_factory=list, max_length=12)


class AgentVerification(Contract):
    version: Literal["signal-agent-verification-1", "signal-agent-verification-2"] = (
        VERIFICATION_VERSION
    )
    triage_run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    input_sha256: Digest
    base_head: Digest | None
    author: Nonblank
    actor_type: Literal["agent"] = "agent"
    assessments: list[AgentAssessment] = Field(min_length=1, max_length=30)


def _host(url):
    return (urlsplit(url).hostname or "").removeprefix("www.")


def validate_verification(storage, bundle, *, triage_id, input_sha256, documents, window):
    """Check identity, capture lineage and literal support; semantics remain agent judgments."""
    if bundle.triage_run_id != triage_id or bundle.input_sha256 != input_sha256:
        raise ValueError("Verification belongs to another triage input")
    seen = set()
    for assessment in bundle.assessments:
        decision = assessment.decision
        if decision.snapshot_id in seen or decision.snapshot_id not in documents:
            raise ValueError("Duplicate or unknown verification snapshot")
        seen.add(decision.snapshot_id)
        document = documents[decision.snapshot_id]
        for citation in assessment.evidence:
            capture = storage.load(citation.capture_run_id)
            capture_triage = capture.get("question", {}).get("triage_run_id")
            same_corpus = capture_triage == triage_id
            if capture_triage and not same_corpus:
                previous = storage.load(capture_triage)
                previous_input = json.loads(storage.read(previous["input_sha256"]))
                current_input = json.loads(storage.read(input_sha256))
                same_corpus = (
                    previous.get("mode") == "signal_triage"
                    and previous_input["collection_sha256"] == current_input["collection_sha256"]
                )
            page = capture.get("pages", {}).get(citation.url, {})
            if (
                capture.get("mode") != "signal_source_verification"
                or not same_corpus
                or page.get("status") != "captured"
                or page.get("raw_sha256") != citation.raw_sha256
            ):
                raise ValueError("Evidence is not an audited captured response for this triage")
            attempts = capture.get("checkpoint", {}).get("http_attempts", [])
            if not any(
                a.get("url") == citation.url
                and a.get("status") == "completed"
                and a.get("raw_sha256") == citation.raw_sha256
                for a in attempts
            ):
                raise ValueError("Evidence lacks a completed HTTP capture")
            text = ArticleTree(storage.read(citation.raw_sha256)).root.text()
            if text[citation.start : citation.end] != citation.quote or citation.end > len(text):
                raise ValueError("External citation differs from saved raw HTML")
        if decision.source_role == "repost" and decision.original_source_url:
            original = decision.original_source_url
            if urlsplit(original).path in {"", "/"}:
                raise ValueError("Publisher homepage is not an original article")
            attributions = source_attribution(document)
            if not any(_host(a["url"]) == _host(original) for a in attributions):
                raise ValueError("Original article must match the attributed publisher")
            matches = [
                c
                for c in assessment.evidence
                if c.purpose == "original_match"
                and c.url == original
                and c.quote in document.text
                and len(c.quote) >= 40
            ]
            if not matches:
                raise ValueError("Traced original requires a shared passage in both snapshots")
        if "event_outside_window" in decision.reason_codes:
            if assessment.event_date is None or (
                date.fromisoformat(window["mindate"])
                <= assessment.event_date
                <= date.fromisoformat(window["maxdate"])
            ):
                raise ValueError("Event outside window requires an out-of-window event date")
        if assessment.event_date is not None and not any(
            c.purpose == "event_date" for c in assessment.evidence
        ):
            raise ValueError("Event date requires an external supporting excerpt")
        if bundle.version == "signal-agent-verification-2" and assessment.event_date is not None:
            from healthlab.signal.triage_v5 import literal_dates

            if not any(
                value["date"] == assessment.event_date.isoformat()
                for citation in assessment.evidence
                if citation.purpose == "event_date"
                for value in literal_dates(citation.quote)
            ):
                raise ValueError("Event date must match a complete literal date in its evidence")
        if decision.decision == "include":
            if assessment.event_date is None or not (
                date.fromisoformat(window["mindate"])
                <= assessment.event_date
                <= date.fromisoformat(window["maxdate"])
            ):
                raise ValueError("Agent include requires a supported event date in the window")
            purposes = {c.purpose for c in assessment.evidence}
            if not {"publication", "status", "event_date"} <= purposes:
                raise ValueError("Agent include requires publication, event and status evidence")
            if decision.source_role == "repost" and not decision.original_source_url:
                raise ValueError("Agent include cannot bypass unresolved original source")


def verification_requirements(store, view):
    """Actionable holds, without treating a traced original as fresh development.

    Resolving this checklist does not submit a decision: a complete evidence-backed
    assessment must still pass submission validation at the current triage head.
    """
    from healthlab.signal.triage_v5 import date_candidates

    rows = []
    attempts = {a["snapshot_id"]: a for a in view["run"].get("model_attempts", [])}
    for sid, doc in view["documents"].items():
        decision = view["decisions"].get(sid)
        if decision and decision.decision != "needs_review":
            continue
        reasons = set(decision.reason_codes) if decision else set()
        attempt = attempts.get(sid, {})
        candidate = None
        if attempt.get("model_response_sha256") and attempt.get("prompt_version") in {
            "signal-triage-prompt-5",
            "signal-triage-prompt-6",
            "signal-triage-prompt-7",
            "signal-triage-prompt-8",
            "signal-triage-prompt-9",
            "signal-triage-prompt-10",
            "signal-triage-prompt-11",
        }:
            from healthlab.evidence import decode_model_json

            response = json.loads(collection_storage(store).read(attempt["model_response_sha256"]))
            if response:
                try:
                    if attempt["prompt_version"] == "signal-triage-prompt-10":
                        from healthlab.signal.triage_v10 import decode_choices

                        payload = decode_choices(response["content"])
                    else:
                        payload = decode_model_json(response["content"])
                    if not isinstance(payload, dict):
                        raise ValueError("Expected a proposal object")
                    candidate = next(
                        (
                            d
                            for d in date_candidates(doc)
                            if d["candidate_id"] == payload.get("event_date_candidate_id")
                        ),
                        None,
                    )
                except (ValueError, IngestionError):
                    pass
        rows.append(
            dict(
                snapshot_id=sid,
                url=doc.url,
                processing_error=attempt.get("error_code"),
                check_original_article=decision is None
                or decision.source_role != "original"
                or any(a["external"] for a in source_attribution(doc)),
                check_development_date=candidate is None
                or not view["run"]["question"]["mindate"]
                <= candidate["date"]
                <= view["run"]["question"]["maxdate"]
                or bool(reasons & {"development_unclear", "event_outside_window"}),
                check_reported_status=decision is None or decision.reported_status == "unknown",
                selected_event_evidence=candidate,
                automatic_include=False,
            )
        )
    return rows


def submit_agent_verification(store, triage_id, payload, *, expected_head):
    """Append researched AI proposals. Never creates human labels or issue approval."""
    from healthlab.signal.triage import _check_duplicates, _validate_decision, load_triage

    bundle = AgentVerification.model_validate(payload)
    if bundle.version != VERIFICATION_VERSION:
        raise ValueError("Current verification version required for new submissions")
    storage = collection_storage(store)
    with storage.state.lock():
        view = load_triage(storage, triage_id)
        if expected_head != view["head"] or bundle.base_head != expected_head:
            raise ValueError("Stale verification head; reload and reassess")
        validate_verification(
            storage,
            bundle,
            triage_id=triage_id,
            input_sha256=view["run"]["input_sha256"],
            documents=view["documents"],
            window=view["run"]["question"],
        )
        updated = dict(view["decisions"])
        for assessment in bundle.assessments:
            decision = assessment.decision
            if view["decision_provenance"].get(decision.snapshot_id) == "human":
                raise ValueError("Agent verification cannot overwrite a human decision")
            _validate_decision(
                decision, view["documents"], view["proposals"], rubric=view["rubric"]
            )
            updated[decision.snapshot_id] = decision
        _check_duplicates(updated, view["documents"])
        digest = storage.put(json_bytes(bundle.model_dump(mode="json")))
        event = {
            "triage_run_id": triage_id,
            "input_sha256": view["run"]["input_sha256"],
            "previous": expected_head,
            "reviewer": bundle.author,
            "reviewer_type": "agent",
            "created_at": utc_now(),
            "verification_sha256": digest,
            "decisions": [a.decision.model_dump() for a in bundle.assessments],
            "semantic_verification": "agent_assessed_not_human_reviewed",
        }
        view["run"].update(head=storage.put(json_bytes(event)), stage="agent_verification_proposed")
        storage.save(view["run"])
        return {
            "status": "completed",
            "head": view["run"]["head"],
            "verification_sha256": digest,
            "human_labels_created": 0,
        }
