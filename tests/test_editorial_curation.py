"""Curation cannot promote a topic label or change frozen evidence to write facts."""

import pytest

from healthlab.editorial_curation import validate_curation
from tests.test_editorial import QUOTE, WINDOW, row


def curation():
    article = row() | {"content_version": "c" * 64}
    payload = dict(
        document_id=article["document_id"],
        snapshot_id=article["snapshot_id"],
        content_version=article["content_version"],
        disposition="newsletter_ready",
        topics=["longevity"],
        actor_type="agent",
        actor="test-agent",
        review_depth="full_source",
        rationale="Source-based review, not a human label.",
        evidence=[dict(start=0, end=len(QUOTE), quote=QUOTE)],
        facts=[
            dict(
                statement="The source reports a pilot.",
                evidence=dict(start=0, end=len(QUOTE), quote=QUOTE),
                event_date="2026-10-10",
                reported_status="pilot",
            )
        ],
        blocking_reasons=[],
        original_classification_failure=None,
        human_approval=False,
    )
    return article, payload


def test_literal_evidence_and_date_keep_agent_review_distinct_from_approval():
    article, payload = curation()
    result = validate_curation(payload, article, WINDOW)
    assert result["actor_type"] == "agent" and not result["human_approval"]
    assert result["facts"][0]["evidence"]["quote"] == QUOTE


@pytest.mark.parametrize("field", ["document_id", "snapshot_id", "content_version"])
def test_curation_cannot_switch_snapshot_or_content_version(field):
    article, payload = curation()
    payload[field] = "different"
    with pytest.raises(ValueError, match="identity differs"):
        validate_curation(payload, article, WINDOW)


@pytest.mark.parametrize("field", ["quote", "start", "end"])
def test_changed_or_mislocated_evidence_is_rejected(field):
    article, payload = curation()
    payload["facts"][0]["evidence"][field] = "invented quote" if field == "quote" else 2
    with pytest.raises(ValueError):
        validate_curation(payload, article, WINDOW)


@pytest.mark.parametrize(
    "updates",
    [
        dict(review_depth="source_passage_and_model_audit"),
        dict(blocking_reasons=["untraced_origin"]),
        dict(human_approval=True),
        dict(facts=[]),
        dict(topics=[]),
    ],
)
def test_topic_only_or_pending_evidence_cannot_be_released(updates):
    article, payload = curation()
    payload.update(updates)
    with pytest.raises(ValueError):
        validate_curation(payload, article, WINDOW)


def test_date_must_be_literal_and_in_window():
    article, payload = curation()
    payload["facts"][0]["event_date"] = "2026-10-11"
    with pytest.raises(ValueError, match="literal evidence"):
        validate_curation(payload, article, WINDOW)
    payload["facts"][0]["event_date"] = "2026-10-10"
    with pytest.raises(ValueError, match="in-window"):
        validate_curation(payload, article, {"mindate": "2026-10-11", "maxdate": "2026-10-16"})


def external_date_case():
    article, payload = curation()
    text = "Chiều 12/10/2026: phiên họp về chăm sóc người cao tuổi."
    payload["facts"][0].update(
        event_date="2026-10-12",
        date_reference=dict(
            source_id="official-event-inventory",
            raw_sha256="d" * 64,
            start=0,
            end=len(text),
            quote=text,
            link_rationale="Agent matched the same meeting and agenda; not a publication-date guess.",
        ),
    )
    sources = {"official-event-inventory": {"raw_sha256": "d" * 64, "body": text}}
    return article, payload, sources


def test_external_event_date_requires_explicit_captured_source():
    article, payload, sources = external_date_case()
    result = validate_curation(payload, article, WINDOW, date_sources=sources)
    assert result["facts"][0]["event_date"] == "2026-10-12"
    with pytest.raises(ValueError, match="captured source"):
        validate_curation(payload, article, WINDOW)


@pytest.mark.parametrize(
    "field,value",
    [
        ("raw_sha256", "e" * 64),
        ("start", 1),
        ("quote", "Uncaptured date 12/10/2026"),
        ("source_id", "unknown"),
    ],
)
def test_date_reference_cannot_switch_capture_or_offsets(field, value):
    article, payload, sources = external_date_case()
    payload["facts"][0]["date_reference"][field] = value
    with pytest.raises(ValueError, match="captured source"):
        validate_curation(payload, article, WINDOW, date_sources=sources)


def test_external_reference_cannot_certify_a_different_date():
    article, payload, sources = external_date_case()
    payload["facts"][0]["event_date"] = "2026-10-13"
    with pytest.raises(ValueError, match="literal evidence"):
        validate_curation(payload, article, WINDOW, date_sources=sources)


def test_retained_background_does_not_need_a_fabricated_event_date():
    article, payload = curation()
    payload["disposition"] = "retain_for_evidence_review"
    payload["facts"] = []
    payload["blocking_reasons"] = ["undated"]
    result = validate_curation(payload, article, WINDOW)
    assert not result["facts"] and result["blocking_reasons"]


def test_background_only_is_reviewed_but_does_not_release_writing_facts():
    article, payload = curation()
    payload.update(disposition="background_only", facts=[], blocking_reasons=["undated_source"])
    assert validate_curation(payload, article, WINDOW)["disposition"] == "background_only"
    payload["facts"] = curation()[1]["facts"]
    with pytest.raises(ValueError, match="Background needs"):
        validate_curation(payload, article, WINDOW)


@pytest.mark.parametrize(
    "updates",
    [
        dict(topics=[]),
        dict(evidence=[]),
        dict(blocking_reasons=[]),
        dict(review_depth="source_passage_and_model_audit"),
    ],
)
def test_background_requires_source_review_and_explicit_restrictions(updates):
    article, payload = curation()
    payload.update(disposition="background_only", facts=[], blocking_reasons=["undated_source"])
    payload.update(updates)
    with pytest.raises(ValueError, match="Background needs"):
        validate_curation(payload, article, WINDOW)
