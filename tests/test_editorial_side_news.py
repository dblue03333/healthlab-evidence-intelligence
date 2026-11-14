"""Near misses in the approval preview and approver-promoted side news, offline.

Synthetic text only. The approver, not a model, decides whether a held-back article's
source-bound facts go into the side-news section; promotion makes no model call.
"""

from types import SimpleNamespace

import pytest

from healthlab import editorial as ed
from healthlab import editorial_issue as issue_mod
from healthlab.data.contracts import Gold, Window
from healthlab.editorial import runs as editorial_runs
from healthlab.store import RunStore
from tests.test_editorial import WINDOW, proposal, row, scope_assessment, setup_run

DRAFT = dict(
    title="Thử nghiệm hỗ trợ phục hồi chức năng",
    sections=[
        dict(
            title="Một chương trình thí điểm",
            facts=[
                dict(text="Chương trình thí điểm bắt đầu ngày 10/10/2026.", evidence_ids=["E1"])
            ],
            interpretation="Có thể theo dõi thêm.",
            uncertainty="Chưa có dữ liệu hiệu quả.",
        )
    ],
)
HELD = "held"


def issue_with_near_miss(tmp_path, monkeypatch):
    """One article passes; a second is on topic but its scope check says mention only."""
    store, _, provider = setup_run(
        tmp_path,
        monkeypatch,
        [
            proposal(),
            proposal(),
            # Scope jobs run in document order: "held" before "id".
            scope_assessment(verdict="mention_only", ageing_context=None, substantive_detail=None),
            scope_assessment(),
            DRAFT,
        ],
    )
    articles = [row(), dict(row(), document_id=HELD, url="https://example.org/held")]
    monkeypatch.setattr(editorial_runs, "supervised_records", lambda *a: articles)
    gold = Gold(
        silver_sha256="a" * 64,
        window=Window(**WINDOW),
        status="ready",
        document_ids=[a["document_id"] for a in articles],
    )
    monkeypatch.setattr(editorial_runs, "read_dataset", lambda *a: (None, None, None, gold))
    run = ed.prepare(RunStore(tmp_path / "source"), store, "d" * 64, provider, call_budget=10)
    ed.run_calls(store, run["run_id"], provider, workers=1)
    ed.plan_scope(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="scope", workers=1)
    ed.plan_draft(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="draft", workers=1)
    source = SimpleNamespace(
        source_id="moh_portal",
        organization="Bộ Y tế",
        required=True,
        status="completed",
        coverage_complete=True,
        candidates=2,
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
    issue = issue_mod.create_issue(store, RunStore(tmp_path / "source"), run["run_id"])
    return store, issue, provider


def preview(store, issue):
    return (store.root / "runs" / issue["run_id"] / "newsletter.html").read_text()


def test_preview_lists_the_held_back_article_with_a_promote_command(tmp_path, monkeypatch):
    store, issue, _ = issue_with_near_miss(tmp_path, monkeypatch)
    packet = ed.evidence_packet(store, issue["editorial_run_id"])
    held = {a["document_id"]: a["eligible_for_unapproved_draft"] for a in packet["articles"]}
    assert held == {"id": True, HELD: False}
    html = preview(store, issue)
    assert "Bài đúng chủ đề nhưng bị chặn" in html and "https://example.org/held" in html
    assert issue_mod.REASONS["scope_not_substantive"] in html
    assert f"promote {issue['run_id']} --expected-version {issue['issue_sha256']}" in html
    assert f"--document-id {HELD}" in html
    text = (store.root / "runs" / issue["run_id"] / "newsletter.txt").read_text()
    assert "bị chặn" not in text  # The email text never carries the near-miss list.


def test_promote_adds_side_news_without_a_model_call_and_clears_approval(tmp_path, monkeypatch):
    store, issue, provider = issue_with_near_miss(tmp_path, monkeypatch)
    calls = provider.calls
    approved = issue_mod.approve(
        store, issue["run_id"], expected_version=issue["issue_sha256"], reviewer="r", rationale="ok"
    )
    assert approved["approval_sha256"]
    promoted = issue_mod.promote(
        store, issue["run_id"], HELD, expected_version=issue["issue_sha256"]
    )
    assert provider.calls == calls
    assert "approval_sha256" not in promoted and promoted["stage"] == "draft_ready"
    assert promoted["revisions"][-1]["promoted"] == {"document_id": HELD, "fact_indexes": [0]}

    _, draft, by_id = issue_mod.load_issue(store, issue["run_id"])
    [item] = draft.side_news
    assert item.evidence_ids == ["S1"] and by_id["S1"]["document_id"] == HELD
    assert item.text == proposal()["facts"][0]["statement"]
    html = preview(store, promoted)
    assert issue_mod.SIDE_NEWS_TITLE in html and "https://example.org/held" in html
    assert "Bài đúng chủ đề nhưng bị chặn" not in html  # Promoted articles leave the list.
    text, _ = issue_mod.render(promoted, draft, by_id)
    assert issue_mod.SIDE_NEWS_TITLE.upper() in text and "[2]" in text
    with pytest.raises(ValueError, match="no human approval"):
        issue_mod.prepare_email(store, issue["run_id"], "a@gmail.com", "b@example.org")


def test_promote_refuses_stale_versions_passed_articles_and_repeats(tmp_path, monkeypatch):
    store, issue, _ = issue_with_near_miss(tmp_path, monkeypatch)
    version = issue["issue_sha256"]
    with pytest.raises(ValueError, match="already in the issue"):
        issue_mod.promote(store, issue["run_id"], "id", expected_version=version)
    with pytest.raises(ValueError, match="not in this editorial run"):
        issue_mod.promote(store, issue["run_id"], "other", expected_version=version)
    with pytest.raises(ValueError, match="facts 0..0"):
        issue_mod.promote(store, issue["run_id"], HELD, expected_version=version, fact_indexes=[3])
    promoted = issue_mod.promote(store, issue["run_id"], HELD, expected_version=version)
    with pytest.raises(ValueError, match="Stale"):
        issue_mod.promote(store, issue["run_id"], HELD, expected_version=version)
    with pytest.raises(ValueError, match="already in the issue"):
        issue_mod.promote(store, issue["run_id"], HELD, expected_version=promoted["issue_sha256"])


def test_revision_keeps_side_news_bound_to_its_evidence(tmp_path, monkeypatch):
    store, issue, _ = issue_with_near_miss(tmp_path, monkeypatch)
    promoted = issue_mod.promote(
        store, issue["run_id"], HELD, expected_version=issue["issue_sha256"]
    )
    _, draft, _ = issue_mod.load_issue(store, issue["run_id"])
    content = draft.model_dump()
    content["side_news"][0]["text"] = "Câu đã sửa theo nguồn."
    revised = issue_mod.revise(
        store, issue["run_id"], content, expected_version=promoted["issue_sha256"]
    )
    assert issue_mod.load_issue(store, issue["run_id"])[1].side_news[0].text.startswith("Câu")
    content["side_news"][0]["evidence_ids"] = ["S9"]
    with pytest.raises(ValueError, match="outside the pinned packet"):
        issue_mod.revise(store, issue["run_id"], content, expected_version=revised["issue_sha256"])
