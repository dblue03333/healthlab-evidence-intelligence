"""gold-editorial-18 / topic-only-1: AI picks the topic and writes; it never judges facts.

Synthetic text only. Sources are trusted publishers, so no AI reviewer checks whether a
fact is right. The classifier's topic call plus the independent scope check decide
relevance; code checks literal quotes and in-window dates; the human approver reads the
source and the draft.
"""

import pytest

from healthlab import editorial as ed
from healthlab.editorial import runs
from healthlab.editorial_issue import _draft_body
from healthlab.editorial_review_policy import (
    RELEASE_POLICY,
    TOPIC_ONLY_POLICY,
    ai_review_required,
    topic_gate,
)
from tests.conftest import REVIEW_RUNTIME
from tests.test_editorial import WINDOW, proposal, row, scope_assessment, setup_run
from tests.test_editorial_adopt_reviewed import REVIEW
from tests.test_editorial_recovery import child_run, live_output

DRAFT = "# Thí điểm phục hồi chức năng\n\nNguồn báo cáo chương trình bắt đầu ngày 10/10/2026."


def canonical(version=ed.VERSION, **updates):
    article = row()
    value = dict(live_output(), **updates)
    return ed.canonical_proposal(
        ed.resolve_reference_ids(value, article, version=version),
        article,
        WINDOW,
        version=version,
    )


def test_only_gold_editorial_18_drops_the_ai_review_step():
    assert ed.VERSION == "gold-editorial-18" and REVIEW_RUNTIME in ed.READ_VERSIONS
    assert canonical()["release_policy"] == TOPIC_ONLY_POLICY
    assert canonical(REVIEW_RUNTIME)["release_policy"] == RELEASE_POLICY
    assert not ai_review_required(ed.VERSION) and ai_review_required(REVIEW_RUNTIME)


def test_topic_gate_releases_every_fact_without_flags():
    gate = topic_gate(canonical(), WINDOW)
    assert gate["eligible"] and gate["reasons"] == []
    assert gate["facts"] == [
        dict(fact_index=0, released=True, drop_reasons=[], flags=[], in_window_date=True)
    ]


def test_topic_gate_still_needs_relevance_and_an_in_window_date():
    off_topic = topic_gate(canonical(relevance="irrelevant"), WINDOW)
    assert not off_topic["eligible"] and "topic_not_relevant" in off_topic["reasons"]
    later = topic_gate(canonical(), {"mindate": "2026-11-01", "maxdate": "2026-11-07"})
    assert later["reasons"] == ["supported_in_window_development_missing"]


def test_classify_scope_draft_without_any_review_call(tmp_path, monkeypatch):
    store, run, provider = setup_run(tmp_path, monkeypatch, [proposal(), scope_assessment(), DRAFT])
    ed.run_calls(store, run["run_id"], provider, workers=1)
    with pytest.raises(ValueError, match="no AI review step"):
        ed.plan_review(store, run["run_id"])
    ed.plan_scope(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="scope", workers=1)

    packet = ed.evidence_packet(store, run["run_id"])
    [article] = packet["articles"]
    assert article["review"] is None and article["eligible_for_unapproved_draft"]
    [evidence] = packet["draft_evidence"]
    assert "review_flags" not in evidence

    ed.plan_draft(store, run["run_id"])
    result = ed.run_calls(store, run["run_id"], provider, role="draft", workers=1)
    assert result["calls_dispatched"] == 3 and result["draft_evidence_facts"] == 1
    assert "review_flags" not in ed.article_messages(WINDOW, packet["draft_evidence"])[0]["content"]


def test_scope_check_still_blocks_a_passing_mention(tmp_path, monkeypatch):
    store, run, provider = setup_run(
        tmp_path,
        monkeypatch,
        [
            proposal(),
            scope_assessment(verdict="mention_only", ageing_context=None, substantive_detail=None),
        ],
    )
    ed.run_calls(store, run["run_id"], provider, workers=1)
    ed.plan_scope(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="scope", workers=1)
    packet = ed.evidence_packet(store, run["run_id"])
    assert not packet["articles"][0]["eligible_for_unapproved_draft"]
    assert packet["draft_evidence"] == []


def test_v18_adopts_only_scope_from_a_reviewed_v17_parent(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "VERSION", REVIEW_RUNTIME)
    store, parent, provider = setup_run(
        tmp_path, monkeypatch, [proposal(), REVIEW, scope_assessment()]
    )
    ed.run_calls(store, parent["run_id"], provider, workers=1)
    ed.plan_review(store, parent["run_id"])
    ed.run_calls(store, parent["run_id"], provider, role="review", workers=1)
    ed.plan_scope(store, parent["run_id"])
    ed.run_calls(store, parent["run_id"], provider, role="scope", workers=1)
    monkeypatch.setattr(runs, "VERSION", ed.VERSION)

    child = child_run(tmp_path, store, provider, call_budget=10)
    ed.adopt_previous(store, child["run_id"], parent["run_id"])
    result = ed.adopt_reviewed(store, child["run_id"], parent["run_id"])

    assert result == dict(
        run_id=child["run_id"],
        parent_run_id=parent["run_id"],
        scope_adopted=1,
        scope_new_calls_needed=0,
    )
    saved = store.load(child["run_id"])
    assert saved["prepared_review_roles"] == ["scope"] and "review_scope" not in saved
    assert {j["role"] for j in ed.jobs(store, saved)} == {"classify", "scope"}
    assert ed.evidence_packet(store, child["run_id"])["articles"][0]["review_gate"]["eligible"]
    assert provider.calls == 3


def test_preview_shows_a_date_taken_from_another_passage():
    evidence = dict(
        evidence_id="E1",
        url="https://example.org/a",
        title="Nguồn",
        quote="Tỉnh mở rộng mô hình chăm sóc tại nhà.",
        date_evidence=dict(quote="Ngày 10/10/2026, tỉnh họp sơ kết."),
    )
    draft = ed.EditorialDraft(
        title="Chăm sóc tại nhà",
        sections=[
            dict(
                title="Mô hình",
                facts=[dict(text="Tỉnh mở rộng mô hình chăm sóc tại nhà.", evidence_ids=["E1"])],
                interpretation="Theo dõi thêm.",
                uncertainty="Chưa có kết quả.",
            )
        ],
    )
    _, html, _ = _draft_body(draft, {"E1": evidence}, True)
    assert "E1 (ngày): Ngày 10/10/2026, tỉnh họp sơ kết." in "".join(html)
