"""Editorial boundaries: evidence traceability is not semantic correctness."""

import json

import pytest

from healthlab.provider import Completion, FakeProvider
from healthlab.signal import draft_v3
from healthlab.signal.newsletter import draft_issue, load_issue, revise
from healthlab.signal.triage import (
    load_triage,
    propose_triage,
    resolve_model_decision,
    triage_packet,
)
from healthlab.signal.triage_v5 import source_passages
from healthlab.store import json_bytes
from tests.test_signal_triage_v5 import case, compact, proposal


@pytest.fixture
def pilot(tmp_path):
    store, collection, doc, triage = case(
        tmp_path,
        "On 2026-10-08, Vietnam opened a pilot older-person care centre. This is a pilot service.",
    )
    sid = collection["documents"][0]["snapshot_id"]
    value = compact(proposal(doc, sid, reported_status="pilot"))
    for key in [
        "topic_passage_id",
        "development_passage_id",
        "status_passage_id",
        "vietnam_passage_id",
    ]:
        value[key] = source_passages(doc)[-1]["passage_id"]
    provider = FakeProvider([Completion(json.dumps(value), "fake-test", "stop", {})])
    result = propose_triage(
        store,
        triage["run_id"],
        provider,
        expected_head=None,
        prompt_version="signal-triage-prompt-11",
    )
    assert result["status"] == "completed"
    decision = load_triage(store, triage["run_id"])["decisions"][sid]
    assert decision.reported_status == "pilot"
    return store, collection, doc, triage, decision


def payload(sid, text="A pilot older-person care centre opened on 8 October 2026."):
    return {
        "signals": [
            dict(
                snapshot_id=sid,
                facts=[dict(text=text, evidence_ids=["E1"])],
                interpretation="A local care example; wider applicability needs review.",
                uncertainty="Long-term outcomes are not established by this source.",
            )
        ]
    }


def test_v11_does_not_rewrite_v6_or_promote_pilot_to_routine_operation(tmp_path):
    _, c, doc, _ = case(tmp_path, "On 2026-10-08, Vietnam opened a pilot older-person care centre.")
    sid = c["documents"][0]["snapshot_id"]
    raw = json.dumps(compact(proposal(doc, sid, reported_status="operational")))
    old = resolve_model_decision(
        raw, doc, prompt_version="signal-triage-prompt-6", monitoring_window=c["question"]
    )
    new = resolve_model_decision(
        raw, doc, prompt_version="signal-triage-prompt-11", monitoring_window=c["question"]
    )
    assert old.reported_status == "operational" and old.decision == "include"
    assert new.reported_status == "unknown" and new.decision == "needs_review"


@pytest.mark.parametrize("date,check_date", [("2026-10-08", False), ("2025-01-02", True)])
def test_v11_review_queue_retains_selected_date_and_independent_origin_hold(
    tmp_path, date, check_date
):
    store, collection, doc, triage = case(
        tmp_path, f"On {date}, Vietnam opened a pilot older-person care centre."
    )
    sid = collection["documents"][0]["snapshot_id"]
    value = compact(proposal(doc, sid, reported_status="pilot", source_role="repost"))
    provider = FakeProvider([Completion(json.dumps(value), "fake-test", "stop", {})])
    propose_triage(
        store,
        triage["run_id"],
        provider,
        expected_head=None,
        prompt_version="signal-triage-prompt-11",
    )
    packet = triage_packet(store, triage["run_id"])
    assert packet["counts"]["needs_review"] == 1 and packet["counts"]["include"] == 0
    row = packet["verification_queue"][0]
    assert row["selected_event_evidence"]["date"] == date
    assert row["check_development_date"] is check_date
    assert row["check_original_article"] and not row["automatic_include"]


def test_new_draft_supplies_window_and_reviewed_quotes_without_truncating(pilot):
    _, c, doc, _, d = pilot
    sid = c["documents"][0]["snapshot_id"]
    prompts = draft_v3.messages({sid: doc}, {sid: d}, c["question"])
    data = json.loads(prompts[1]["content"])
    assert data["monitoring_window"] == c["question"]
    assert data["documents"][0]["reported_status"] == "pilot"
    assert data["documents"][0]["evidence"][0]["quote"] == d.passages[0].quote
    out = draft_v3.canonical(payload(sid), {sid: doc}, {sid: d}, c["question"])
    assert "2026-10-02" in out.overview and "2026-10-09" in out.overview
    assert out.signals[0].headline == doc.title


@pytest.mark.parametrize(
    "text",
    [
        "This week a pilot opened.",
        "Hôm nay một cơ sở mở cửa.",
        "A nationwide rollout is complete.",
        "Đã triển khai đại trà mô hình.",
    ],
)
def test_relative_dates_and_pilot_promotion_are_rejected_in_both_languages(pilot, text):
    _, c, doc, _, d = pilot
    sid = c["documents"][0]["snapshot_id"]
    with pytest.raises(ValueError):
        draft_v3.canonical(payload(sid, text), {sid: doc}, {sid: d}, c["question"])


@pytest.mark.parametrize(
    "damage", ["unknown_evidence", "duplicate_evidence", "unknown_snapshot", "missing_signal"]
)
def test_each_fact_requires_known_unique_reviewed_evidence(pilot, damage):
    _, c, doc, _, d = pilot
    sid = c["documents"][0]["snapshot_id"]
    value = payload(sid)
    if damage == "unknown_evidence":
        value["signals"][0]["facts"][0]["evidence_ids"] = ["E999"]
    elif damage == "duplicate_evidence":
        value["signals"][0]["facts"][0]["evidence_ids"] = ["E1", "E1"]
    elif damage == "unknown_snapshot":
        value["signals"][0]["snapshot_id"] = "f" * 64
    else:
        value["signals"] = []
    with pytest.raises(ValueError):
        draft_v3.canonical(value, {sid: doc}, {sid: d}, c["question"])


def test_evidence_draft_replay_and_revision_require_matching_facts(pilot):
    s, c, doc, t, d = pilot
    sid = c["documents"][0]["snapshot_id"]
    value = payload(sid)
    provider = FakeProvider([Completion(json.dumps(value), "fake-test", "stop", {})])
    run = draft_issue(s, c["run_id"], provider, triage_id=t["run_id"], evidence_output=True)
    assert run["status"] == "completed" and run["prompt_version"] == "signal-draft-3"
    original, draft, _ = load_issue(s, run["run_id"])
    raw = s.read(run["model_response_sha256"])
    value["signals"][0]["facts"][0]["text"] = "A pilot care centre opened on 8 October 2026."
    edited = revise(s, run["run_id"], value, expected_version=run["issue_sha256"])
    assert edited["issue_sha256"] != run["issue_sha256"] and not edited.get("approval_sha256")
    assert s.read(run["model_response_sha256"]) == raw
    _, current, _ = load_issue(s, run["run_id"])
    assert current.signals[0].what_happened == value["signals"][0]["facts"][0]["text"]
    broken = current.model_dump()
    broken["signals"][0]["what_happened"] = "An unsupported replacement."
    manifest = s.load(run["run_id"])
    manifest["issue_sha256"] = s.put(json_bytes(broken))
    s.save(manifest)
    with pytest.raises(ValueError, match="differs from evidence-linked"):
        load_issue(s, run["run_id"])


def test_english_and_vietnamese_facts_are_accepted_without_claiming_entailment(pilot):
    _, c, doc, _, d = pilot
    sid = c["documents"][0]["snapshot_id"]
    for text in [
        "A pilot care centre opened on 8 October 2026.",
        "Ngày 8/10/2026, một mô hình chăm sóc thí điểm được khai trương.",
    ]:
        assert draft_v3.canonical(payload(sid, text), {sid: doc}, {sid: d}, c["question"]).signals


def test_cli_new_inference_defaults_to_status_v11_and_evidence_draft(pilot, monkeypatch, capsys):
    from healthlab.signal.__main__ import main

    store, collection, doc, triage, decision = pilot
    sid = collection["documents"][0]["snapshot_id"]
    provider = FakeProvider([Completion(json.dumps(payload(sid)), "fake-test", "stop", {})])
    provider.close = lambda: None
    monkeypatch.setattr("healthlab.signal.__main__.signal_provider", lambda name: provider)
    assert (
        main(
            [
                "--store",
                str(store.root),
                "draft",
                collection["run_id"],
                "--triage-run",
                triage["run_id"],
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert store.load(result["run_id"])["prompt_version"] == "signal-draft-3"
    # Show default inference contract without performing a new model request.
    captured = {}
    monkeypatch.setattr(
        "healthlab.signal.__main__.propose_triage",
        lambda *args, **kwargs: captured.update(kwargs) or {"status": "completed"},
    )
    assert (
        main(
            [
                "--store",
                str(store.root),
                "triage",
                "propose",
                triage["run_id"],
                "--expected-head",
                store.load(triage["run_id"])["head"],
            ]
        )
        == 0
    )
    assert captured["prompt_version"] == "signal-triage-prompt-11"
