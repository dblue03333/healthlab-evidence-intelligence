"""Scope judgments are separate, source-bound and required for current drafting."""

import json

import pytest

from healthlab import editorial as ed
from healthlab.editorial.scope import scope_gate, validate_scope
from tests.editorial_legacy import resolve_v12
from tests.test_editorial import WINDOW, linked_proposal, proposal, row, scope_assessment, setup_run
from tests.test_editorial_review_policy import review

pytestmark = pytest.mark.usefixtures("review_runtime")


def test_scope_prompt_is_full_source_and_cannot_receive_the_first_proposal():
    prompt, _ = ed.messages("scope", row(), WINDOW)
    user = json.loads(prompt[1]["content"])
    assert "first_proposal" not in user and "evidence" not in user
    assert "".join("".join(p["quote"].split()) for p in user["article"]["passages"]) == "".join(
        row()["body"].split()
    )
    with pytest.raises(ValueError, match="must not see"):
        ed.messages("scope", row(), WINDOW, proposal=proposal())


def test_scope_passages_are_resolved_and_identity_checked():
    units, _ = ed.passage_inputs(row())
    assessment = validate_scope(scope_assessment(), units)
    assert scope_gate(assessment, units)["eligible"]
    assessment["source_passages"][0]["quote"] = "changed source"
    assert not scope_gate(assessment, units)["eligible"]
    with pytest.raises(ValueError, match="Unknown scope"):
        validate_scope(scope_assessment(passage_ids=["P999"]), units)


@pytest.mark.parametrize("verdict", ["mention_only", "unsupported", "uncertain"])
def test_nonsubstantive_or_unknown_scope_blocks_even_with_literal_passages(verdict):
    units, _ = ed.passage_inputs(row())
    value = validate_scope(scope_assessment(verdict=verdict), units)
    assert "scope_not_substantive" in scope_gate(value, units)["reasons"]


def test_outside_knowledge_flag_blocks_a_self_described_substantive_scope():
    units, _ = ed.passage_inputs(row())
    value = validate_scope(scope_assessment(requires_outside_knowledge=True), units)
    assert "scope_requires_outside_knowledge" in scope_gate(value, units)["reasons"]


def test_missing_scope_blocks_a_positive_factual_review_then_explicit_scope_can_release(
    tmp_path, monkeypatch
):
    store, run, provider = setup_run(
        tmp_path, monkeypatch, [proposal(), review(), scope_assessment()]
    )
    ed.run_calls(store, run["run_id"], provider, workers=1)
    ed.plan_review(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="review", workers=1)
    packet = ed.evidence_packet(store, run["run_id"])
    assert packet["articles"][0]["review"]["newsletter_eligible"]
    assert "scope_assessment_missing" in packet["articles"][0]["review_gate"]["reasons"]
    assert not packet["draft_evidence"]
    ed.plan_scope(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="scope", workers=1)
    packet = ed.evidence_packet(store, run["run_id"])
    assert packet["draft_evidence"]
    assert provider.calls == 3
    scope_job = [j for j in ed.jobs(store, store.load(run["run_id"])) if j["role"] == "scope"][0]
    assert "proposal_sha256" not in scope_job
    assert ed.plan_scope(store, run["run_id"])["jobs"] == store.load(run["run_id"])["jobs"]


def test_negative_scope_cannot_be_overridden_by_two_positive_model_labels(tmp_path, monkeypatch):
    store, run, provider = setup_run(
        tmp_path, monkeypatch, [proposal(), review(), scope_assessment(verdict="mention_only")]
    )
    ed.run_calls(store, run["run_id"], provider, workers=1)
    ed.plan_review(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="review", workers=1)
    ed.plan_scope(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="scope", workers=1)
    assert not ed.evidence_packet(store, run["run_id"])["draft_evidence"]
    ed.plan_draft(store, run["run_id"])
    assert not store.load(run["run_id"])["draft_planned"]


def test_scope_planner_reserves_writer_and_never_silently_reduces_scope(tmp_path, monkeypatch):
    store, run, provider = setup_run(tmp_path, monkeypatch, [proposal()])
    ed.run_calls(store, run["run_id"], provider, workers=1)
    saved = store.load(run["run_id"])
    saved["call_budget"] = 2
    store.save(saved)
    with pytest.raises(ValueError, match="Budget cannot cover scope"):
        ed.plan_scope(store, run["run_id"], all_articles=True)
    assert len(store.load(run["run_id"])["jobs"]) == 1


def reference_payload():
    article, legacy = linked_proposal()
    payload = dict(
        legacy,
        facts=[{k: v for k, v in legacy["facts"][0].items() if k != "date_context_passage_id"}],
    )
    return article, payload


def test_current_date_contract_derives_context_but_still_requires_event_link():
    article, payload = reference_payload()
    resolved = resolve_v12(payload, article)
    assert resolved["facts"][0]["date_evidence"]["passage_id"] == "P1"
    assert resolved["facts"][0]["event_date"] == "2026-10-10"
    payload["facts"][0]["date_link_rationale"] = None
    with pytest.raises(ValueError, match="Cross-passage date"):
        resolve_v12(payload, article)


def test_same_passage_date_needs_no_redundant_context_or_rationale():
    article, payload = reference_payload()
    payload["facts"][0].update(passage_id="P1", date_link_rationale=None)
    value = resolve_v12(payload, article)
    assert value["facts"][0]["date_evidence"] is None
    assert value["facts"][0]["event_date"] == "2026-10-10"
    required = ed.ReferenceArticleProposal.model_json_schema()["$defs"]["ReferenceIDFact"][
        "required"
    ]
    assert "date_context_passage_id" not in required
    payload["facts"][0]["date_context_passage_id"] = "P1"
    with pytest.raises(ValueError):
        resolve_v12(payload, article)


def test_current_date_contract_rejects_foreign_candidate():
    article, payload = reference_payload()
    payload["facts"][0]["event_date_candidate_id"] = "D999"
    with pytest.raises(ValueError, match="Unknown event date"):
        resolve_v12(payload, article)


@pytest.mark.parametrize("date_id", ["D1", None])
def test_redundant_rationale_cannot_change_a_same_passage_or_missing_date(date_id):
    article, payload = reference_payload()
    fact = payload["facts"][0]
    fact.update(passage_id="P1", event_date_candidate_id=date_id, date_link_rationale=None)
    before = resolve_v12(payload, article)["facts"][0]
    fact["date_link_rationale"] = "Model explanation with no independent authority over dates."
    after = resolve_v12(payload, article)["facts"][0]
    assert after["date_link_rationale"] == fact["date_link_rationale"]
    assert after["event_date"] == before["event_date"]
    assert after["date_evidence"] is before["date_evidence"] is None
    assert {k: v for k, v in before.items() if k != "date_link_rationale"} == {
        k: v for k, v in after.items() if k != "date_link_rationale"
    }
