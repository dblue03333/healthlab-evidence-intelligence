"""Inclusive date ownership, semantic linkage and genuine blockers stay separate."""

import json

import pytest

from healthlab import editorial as ed
from healthlab.editorial.review_policy import SemanticReview, fact_date_checks, validate_review
from healthlab.editorial.review_policy import review_gate as gate
from tests.editorial_legacy import canonical_v12
from tests.test_editorial import WINDOW, linked_proposal, proposal, row, setup_run

pytestmark = pytest.mark.usefixtures("review_runtime")


def semantic(**updates):
    return (
        dict(
            review_version="editorial-review-4",
            relevance="relevant",
            topic_passage_ids=["P2"],
            supported_fact_indexes=[0],
            supported_date_fact_indexes=[],
            blocking_findings=[],
            audit_notes=[],
            rationale="Nguồn hỗ trợ fact ở đúng trạng thái được thuật lại.",
        )
        | updates
    )


def finding(code):
    return dict(
        code=code,
        explanation="Named unresolved evidence issue",
        fact_indexes=[0],
        passage_ids=["P1"],
    )


def candidate():
    return canonical_v12(proposal(), row(), WINDOW)


@pytest.mark.parametrize(
    "date,eligible",
    [
        ("2026-10-03", True),
        ("2026-10-16", True),
        ("2026-10-02", False),
        ("2026-10-17", False),
    ],
)
def test_code_derives_inclusive_eligibility_from_literal_dated_facts(date, eligible):
    p = candidate()
    p["facts"][0].update(event_date=date, quote=f"Cuộc họp diễn ra ngày {date}.")
    value = validate_review(semantic(), p, ["P1", "P2"], WINDOW)
    assert value["newsletter_eligible"] is eligible
    assert gate(p, value, ["P1", "P2"], WINDOW)["eligible"] is eligible


def test_missing_date_is_not_filled_from_article_publication_or_audit_text():
    p = candidate()
    p["facts"][0].update(event_date=None, quote="Cuộc họp diễn ra ngày 10/10.")
    r = semantic(audit_notes=["Publication on 11 October cannot date the meeting."])
    assert not validate_review(r, p, ["P1", "P2"], WINDOW)["newsletter_eligible"]
    assert fact_date_checks(p, WINDOW)[0]["inside_inclusive_window"] is None


@pytest.mark.parametrize("code", ["event_date_missing", "event_date_outside_window"])
def test_typed_date_claim_contradicting_literal_in_window_evidence_is_rejected(code):
    with pytest.raises(ValueError, match="contradicts"):
        validate_review(
            semantic(blocking_findings=[finding(code)]), candidate(), ["P1", "P2"], WINDOW
        )


def test_real_outside_window_finding_remains_a_blocker():
    p = candidate()
    p["facts"][0].update(event_date="2026-10-02", quote="Cuộc họp ngày 02/10/2026.")
    value = validate_review(
        semantic(blocking_findings=[finding("event_date_outside_window")]), p, ["P1", "P2"], WINDOW
    )
    assert not value["newsletter_eligible"] and value["blocking_findings"]


def test_arithmetic_does_not_override_a_wrong_event_or_real_status_origin_blocker():
    for code in [
        "event_link_unverified",
        "status_overstated",
        "origin_unverified",
        "claim_not_supported",
    ]:
        r = semantic(blocking_findings=[finding(code)])
        value = validate_review(r, candidate(), ["P1", "P2"], WINDOW)
        assert not gate(candidate(), value, ["P1", "P2"], WINDOW)["eligible"]
        assert value["blocking_findings"][0]["code"] == code


def test_supported_draft_is_reportable_but_claimed_rollout_can_still_be_blocked():
    p = candidate()
    p["facts"][0]["reported_status"] = "proposed"
    p["facts"][0]["statement"] = "Tại cuộc họp, lãnh đạo đề nghị triển khai thí điểm."
    value = validate_review(semantic(), p, ["P1", "P2"], WINDOW)
    assert value["newsletter_eligible"]
    p["facts"][0]["statement"] = "Dịch vụ đã triển khai đầy đủ và chứng minh hiệu quả."
    value = validate_review(
        semantic(blocking_findings=[finding("status_overstated")]), p, ["P1", "P2"], WINDOW
    )
    assert not value["newsletter_eligible"]


def test_cross_passage_event_date_still_needs_independent_semantic_confirmation():
    article, payload = linked_proposal()
    p = canonical_v12(ed.resolve_ids(payload, article), article, WINDOW)
    assert fact_date_checks(p, WINDOW)[0]["cross_passage_link_requires_review"]
    assert not validate_review(semantic(), p, ["P1", "P2"], WINDOW)["newsletter_eligible"]
    assert validate_review(semantic(supported_date_fact_indexes=[0]), p, ["P1", "P2"], WINDOW)[
        "newsletter_eligible"
    ]


def test_saved_derived_eligibility_cannot_be_tampered_or_supply_a_second_model_vote():
    schema = SemanticReview.model_json_schema()
    assert "newsletter_eligible" not in schema["properties"]
    with pytest.raises(ValueError):
        SemanticReview.model_validate(semantic(newsletter_eligible=True))
    saved = validate_review(semantic(), candidate(), ["P1", "P2"], WINDOW)
    saved["newsletter_eligible"] = False
    assert gate(candidate(), saved, ["P1", "P2"], WINDOW)["reasons"] == ["review_contract_invalid"]


def test_reviewer_receives_full_source_and_arithmetic_without_source_truth_claim():
    prompt, _ = ed.messages("review", row(), WINDOW, candidate())
    user = json.loads(prompt[1]["content"])
    assert user["code_date_checks"][0]["inside_inclusive_window"] is True
    assert "".join("".join(p["quote"].split()) for p in user["article"]["passages"]) == "".join(
        row()["body"].split()
    )
    assert "NOT semantic approval" in prompt[0]["content"]


def test_contradictory_live_result_is_preserved_as_failure_and_never_auto_retried(
    tmp_path, monkeypatch
):
    bad = semantic(blocking_findings=[finding("event_date_outside_window")])
    store, run, provider = setup_run(tmp_path, monkeypatch, [proposal(), bad])
    ed.run_calls(store, run["run_id"], provider, workers=1)
    ed.plan_review(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="review", workers=1)
    job = next(j for j in ed.jobs(store, store.load(run["run_id"])) if j["role"] == "review")
    assert job["status"] == "failed" and job["content_sha256"]
    assert not ed.evidence_packet(store, run["run_id"])["draft_evidence"]
    ed.run_calls(store, run["run_id"], provider, role="review", workers=1)
    assert provider.calls == 2
