"""gold-editorial-19 / publication-window-1: the issue reports what sources published.

Synthetic text only. An on-topic article published in the period no longer needs a literal
in-window event date. Code gives each fact a time basis; the writer must follow it, and the
approval is refused while the draft uses relative time words. An article whose every fact
is dated before the period stays held back.
"""

from types import SimpleNamespace

import pytest

from healthlab import editorial as ed
from healthlab.data.contracts import Gold, Window
from healthlab.editorial import issue as issue_mod
from healthlab.editorial import runs as editorial_runs
from healthlab.editorial.prompts import PUBLICATION_WINDOW_DRAFT
from healthlab.editorial.review_policy import (
    PUBLICATION_WINDOW_POLICY,
    RELATIVE_TIME_WORDS,
    TOPIC_ONLY_POLICY,
    ai_review_required,
    current_basis,
    fact_time_basis,
    relative_time_words,
    topic_gate,
)
from healthlab.store import RunStore
from tests.conftest import TOPIC_ONLY_RUNTIME
from tests.test_editorial import BODY, WINDOW, proposal, row, scope_assessment, setup_run
from tests.test_editorial_recovery import live_output

UNDATED = "Hỗ trợ nhóm trên 65 tuổi tự sinh hoạt tại nhà."
UNDATED_FACT = {
    "statement": "Chương trình hỗ trợ nhóm trên 65 tuổi tự sinh hoạt tại nhà.",
    "quote": UNDATED,
    "event_date": None,
    "reported_status": "operational",
}
# The fixture article is published on 2026-10-11 and dated 2026-10-10 in its first passage.
LATER = {"mindate": "2026-10-11", "maxdate": "2026-10-17"}


def draft(text):
    return {
        "title": "Hỗ trợ sinh hoạt tại nhà cho người cao tuổi",
        "sections": [
            {
                "title": "Chăm sóc tại nhà",
                "facts": [{"text": text, "evidence_ids": ["E1"]}],
                "interpretation": "Cần theo dõi cách chương trình được mở rộng.",
                "uncertainty": "Nguồn chưa nêu kết quả.",
            }
        ],
    }


def canonical(version=ed.VERSION, window=WINDOW, *, undated=False):
    article = row()
    value = live_output()
    if undated:
        value["facts"][0].update(passage_id="P2", date_link=None)
    return ed.canonical_proposal(
        ed.resolve_reference_ids(value, article, version=version),
        article,
        window,
        version=version,
    )


def test_gold_editorial_19_is_current_and_has_no_ai_review():
    assert ed.VERSION == "gold-editorial-19" and TOPIC_ONLY_RUNTIME in ed.READ_VERSIONS
    assert canonical()["release_policy"] == PUBLICATION_WINDOW_POLICY
    assert canonical(TOPIC_ONLY_RUNTIME)["release_policy"] == TOPIC_ONLY_POLICY
    assert not ai_review_required(ed.VERSION)


@pytest.mark.parametrize(
    ("event_date", "literal", "published", "expected", "current"),
    [
        ("2026-10-10", True, "2026-10-11", "event_date_in_window", True),
        ("2026-10-01", True, "2026-10-11", "event_date_before_window", False),
        ("2026-12-01", True, "2026-10-11", "event_date_after_window", True),
        ("2026-12-01", True, "2026-09-30", "event_date_after_window", False),
        (None, False, "2026-10-11", "publication_date", True),
        (None, False, "2026-09-30", None, False),
        (None, False, None, None, False),
        ("2026-10-10", False, "2026-10-11", None, False),
    ],
)
def test_time_basis_is_derived_by_code(event_date, literal, published, expected, current):
    basis = fact_time_basis(event_date, literal, published, WINDOW)
    assert basis == expected
    assert current_basis(basis, published, WINDOW) is current


def test_undated_fact_in_an_article_published_in_the_period_is_current_news():
    p = canonical(undated=True)
    assert p["facts"][0]["event_date"] is None and p["newsletter_candidate"]
    gate = topic_gate(p, WINDOW)
    assert gate["eligible"] and gate["release_policy"] == PUBLICATION_WINDOW_POLICY
    assert gate["facts"][0] | {} == {
        "fact_index": 0,
        "released": True,
        "drop_reasons": [],
        "flags": [],
        "in_window_date": False,
        "time_basis": "publication_date",
        "current": True,
    }
    # The same classification under v18 still needs a literal in-window event date.
    old = canonical(TOPIC_ONLY_RUNTIME, undated=True)
    assert not old["newsletter_candidate"]
    assert "supported_in_window_development_missing" in topic_gate(old, WINDOW)["reasons"]


def test_an_article_dated_only_before_the_period_stays_held():
    p = canonical(window=LATER)
    assert not p["newsletter_candidate"]
    gate = topic_gate(p, LATER)
    assert gate["reasons"] == ["primary_not_newsletter_candidate", "current_development_missing"]
    assert gate["facts"][0]["time_basis"] == "event_date_before_window"


def test_classify_and_scope_prompts_match_v18_so_its_results_can_be_adopted():
    article = row()
    for role in ("classify", "scope"):
        assert ed.messages(role, article, WINDOW, version=ed.VERSION) == ed.messages(
            role, article, WINDOW, version=TOPIC_ONLY_RUNTIME
        )


def test_only_the_v19_draft_prompt_carries_the_time_basis_rules():
    evidence = [{"evidence_id": "E1", "quote": UNDATED}]
    new, _ = ed.messages("draft", None, WINDOW, evidence=evidence, version=ed.VERSION)
    old, _ = ed.messages("draft", None, WINDOW, evidence=evidence, version=TOPIC_ONLY_RUNTIME)
    assert PUBLICATION_WINDOW_DRAFT in new[0]["content"]
    assert "time_basis" not in old[0]["content"]
    assert all(f'"{w}"' in PUBLICATION_WINDOW_DRAFT for w in RELATIVE_TIME_WORDS)


def test_relative_time_words_are_found_case_insensitively_as_whole_words():
    text = "Đến nay tỉnh đã khám; theo bài đăng ngày 05/11/2026, hiện nay. Tuần này!"
    assert relative_time_words(text) == ["Đến nay", "hiện nay", "Tuần này"]
    assert relative_time_words("Tính đến ngày 28/10/2026, xã đã khám 500 người.") == []


def test_packet_gives_the_writer_each_facts_time_basis(tmp_path, monkeypatch):
    dated = proposal()["facts"][0]
    store, run, provider = setup_run(
        tmp_path,
        monkeypatch,
        [proposal(facts=[dated, UNDATED_FACT]), scope_assessment()],
    )
    ed.run_calls(store, run["run_id"], provider, workers=1)
    ed.plan_scope(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="scope", workers=1)
    evidence = ed.evidence_packet(store, run["run_id"])["draft_evidence"]
    assert [(e["time_basis"], e["publication_date"]) for e in evidence] == [
        ("event_date_in_window", "2026-10-11"),
        ("publication_date", "2026-10-11"),
    ]


def issue(tmp_path, monkeypatch, text):
    store, run, provider = setup_run(
        tmp_path,
        monkeypatch,
        [proposal(facts=[UNDATED_FACT]), scope_assessment(), draft(text)],
    )
    ed.run_calls(store, run["run_id"], provider, workers=1)
    ed.plan_scope(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="scope", workers=1)
    ed.plan_draft(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="draft", workers=1)
    gold = Gold(
        silver_sha256="a" * 64, window=Window(**WINDOW), status="ready", document_ids=["id"]
    )
    source = SimpleNamespace(
        source_id="moh_portal",
        organization="Bộ Y tế",
        required=True,
        status="completed",
        coverage_complete=True,
        candidates=1,
    )
    monkeypatch.setattr(
        issue_mod,
        "read_dataset",
        lambda *a: (
            None,
            None,
            SimpleNamespace(sources=[source]),
            SimpleNamespace(**gold.__dict__),
        ),
    )
    monkeypatch.setattr(editorial_runs, "supervised_records", lambda *a: [row()])
    return store, issue_mod.create_issue(store, RunStore(tmp_path / "source"), run["run_id"])


def preview(store, created):
    return (store.root / "runs" / created["run_id"] / "newsletter.html").read_text()


def test_relative_time_words_block_approval_until_revised(tmp_path, monkeypatch):
    store, created = issue(
        tmp_path, monkeypatch, "Hiện nay chương trình hỗ trợ người trên 65 tuổi tại nhà."
    )
    html = preview(store, created)
    assert "Chưa duyệt được" in html and "«Hiện nay»" in html
    assert "viết theo bài đăng ngày 2026-10-11" in html
    with pytest.raises(ValueError, match="relative time words"):
        issue_mod.approve(
            store,
            created["run_id"],
            expected_version=created["issue_sha256"],
            reviewer="r",
            rationale="ok",
        )

    fixed = draft("Theo bài đăng ngày 11/10/2026, chương trình hỗ trợ người trên 65 tuổi tại nhà.")
    revised = issue_mod.revise(
        store, created["run_id"], fixed, expected_version=created["issue_sha256"]
    )
    assert "Chưa duyệt được" not in preview(store, revised)
    approved = issue_mod.approve(
        store,
        created["run_id"],
        expected_version=revised["issue_sha256"],
        reviewer="r",
        rationale="ok",
    )
    assert approved["approval_sha256"]
    text = (store.root / "runs" / created["run_id"] / "newsletter.txt").read_text()
    assert "Mốc thời gian" not in text  # The time basis is for the approver, not the email.


def test_v18_issues_are_not_checked_for_time_words():
    evidence = {
        "E1": {"evidence_id": "E1", "url": "https://example.org/a", "title": "t", "quote": BODY}
    }
    assert not issue_mod._time_dated(evidence)
    assert issue_mod.time_word_findings(
        ed.EditorialDraft.model_validate(draft("Hiện nay có."))
    ) == [("Mục «Chăm sóc tại nhà»", "Hiện nay")]
