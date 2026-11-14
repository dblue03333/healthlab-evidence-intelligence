"""Editorial draft -> version-bound issue -> human approval -> email, offline."""

import json
import smtplib
from email import message_from_bytes, policy
from types import SimpleNamespace

import pytest

from healthlab import editorial as ed
from healthlab.editorial import issue as issue_mod
from healthlab.store import RunStore
from tests.test_editorial import proposal, scope_assessment, setup_run

pytestmark = pytest.mark.usefixtures("review_runtime")


SENDER = "sender@gmail.com"


def drafted(tmp_path, monkeypatch, status="ready", source_status="completed", scope="substantive"):
    review = {
        "relevance": "relevant",
        "newsletter_eligible": True,
        "supported_fact_indexes": [0],
        "review_version": "editorial-review-2",
        "topic_passage_ids": ["P2"],
        "blocking_findings": [],
        "audit_notes": [],
        "rationale": "Đoạn trích hỗ trợ đúng mốc và trạng thái thí điểm.",
    }
    draft = {
        "title": "Thử nghiệm hỗ trợ phục hồi chức năng",
        "sections": [
            {
                "title": "Một chương trình thí điểm",
                "facts": [
                    {
                        "text": "Chương trình thí điểm bắt đầu ngày 10/10/2026.",
                        "evidence_ids": ["E1"],
                    }
                ],
                "interpretation": "Có thể theo dõi thêm.",
                "uncertainty": "Chưa có dữ liệu hiệu quả.",
            }
        ],
    }
    store, run, provider = setup_run(
        tmp_path, monkeypatch, [proposal(), review, scope_assessment(verdict=scope), draft]
    )
    ed.run_calls(store, run["run_id"], provider, workers=1)
    ed.plan_review(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="review", workers=1)
    ed.plan_scope(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="scope", workers=1)
    ed.plan_draft(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="draft", workers=1)
    source = SimpleNamespace(
        source_id="moh_portal",
        organization="Bộ Y tế",
        required=True,
        status=source_status,
        coverage_complete=True,
        candidates=12,
    )
    gold = SimpleNamespace(status=status, document_ids=["moh_portal:a", "who_ageing:b"])
    monkeypatch.setattr(
        issue_mod, "read_dataset", lambda *a: (None, None, SimpleNamespace(sources=[source]), gold)
    )
    return store, run


def create(tmp_path, monkeypatch, **kwargs):
    store, run = drafted(tmp_path, monkeypatch, **kwargs)
    return store, issue_mod.create_issue(store, RunStore(tmp_path / "source"), run["run_id"])


def test_issue_preview_is_unapproved_and_shows_evidence(tmp_path, monkeypatch):
    store, issue = create(tmp_path, monkeypatch)
    directory = store.root / "runs" / issue["run_id"]
    preview = (directory / "newsletter.html").read_text()
    assert "CHƯA ĐƯỢC DUYỆT" in preview and "blockquote" in preview
    assert "Chương trình thí điểm bắt đầu ngày 10/10/2026." in preview
    assert not issue.get("approval_sha256")


def test_email_requires_approval_and_approval_binds_the_version(tmp_path, monkeypatch):
    store, issue = create(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="no human approval"):
        issue_mod.export_email(store, issue["run_id"], SENDER, ["a@example.com"])
    with pytest.raises(ValueError, match="Stale"):
        issue_mod.approve(
            store, issue["run_id"], expected_version="x", reviewer="R", rationale="ok"
        )
    issue_mod.approve(
        store, issue["run_id"], expected_version=issue["issue_sha256"], reviewer="R", rationale="ok"
    )
    [path] = issue_mod.export_email(store, issue["run_id"], SENDER, ["a@example.com"])
    message = message_from_bytes(open(path, "rb").read(), policy=policy.default)
    body = message.get_body(("html",)).get_content()
    assert "Đã duyệt nội bộ" in body and "blockquote" not in body
    assert (
        message["Subject"] == "HealthLab Signal · Thử nghiệm hỗ trợ phục hồi chức năng · 2026-10-16"
    )


def test_revision_clears_approval_and_rejects_unknown_evidence(tmp_path, monkeypatch):
    store, issue = create(tmp_path, monkeypatch)
    issue_mod.approve(
        store, issue["run_id"], expected_version=issue["issue_sha256"], reviewer="R", rationale="ok"
    )
    issue_mod.send_gmail(
        store, issue["run_id"], SENDER, ["a@example.com"], "pw", smtp_factory=FakeSMTP
    )
    assert store.load(issue["run_id"])["stage"] == "sent"
    _, draft, _ = issue_mod.load_issue(store, issue["run_id"])
    content = draft.model_dump()
    bad = json.loads(json.dumps(content))
    bad["sections"][0]["facts"][0]["evidence_ids"] = ["E9"]
    with pytest.raises(ValueError, match="outside the pinned packet"):
        issue_mod.revise(store, issue["run_id"], bad, expected_version=issue["issue_sha256"])
    content["title"] = "Tiêu đề đã sửa"
    revised = issue_mod.revise(
        store, issue["run_id"], content, expected_version=issue["issue_sha256"]
    )
    assert "approval_sha256" not in revised
    assert revised["stage"] == "draft_ready"
    with pytest.raises(ValueError, match="no human approval"):
        issue_mod.export_email(store, issue["run_id"], SENDER, ["a@example.com"])


@pytest.mark.parametrize(
    ("status", "source_status"), [("partial", "completed"), ("ready", "failed")]
)
def test_required_source_gap_blocks_approval(tmp_path, monkeypatch, status, source_status):
    store, issue = create(tmp_path, monkeypatch, status=status, source_status=source_status)
    with pytest.raises(ValueError, match="Required-source"):
        issue_mod.approve(
            store,
            issue["run_id"],
            expected_version=issue["issue_sha256"],
            reviewer="R",
            rationale="ok",
        )


def test_unfinished_run_cannot_become_an_issue(tmp_path, monkeypatch):
    store, run, _ = setup_run(tmp_path, monkeypatch, [])
    with pytest.raises(ValueError, match="unfinished"):
        issue_mod.create_issue(store, RunStore(tmp_path / "source"), run["run_id"])


def test_failed_call_is_not_reported_as_no_news(tmp_path, monkeypatch):
    store, run, provider = setup_run(tmp_path, monkeypatch, ["not json"])
    ed.run_calls(store, run["run_id"], provider, workers=1)
    with pytest.raises(ValueError, match="failed calls"):
        issue_mod.create_issue(store, RunStore(tmp_path / "source"), run["run_id"])


def test_finished_run_without_release_becomes_an_approvable_digest(tmp_path, monkeypatch):
    store, issue = create(tmp_path, monkeypatch, scope="mention_only")
    assert issue["kind"] == "no_release" and "draft_job_id" not in issue
    preview = (store.root / "runs" / issue["run_id"] / "newsletter.html").read_text()
    assert issue_mod.NO_RELEASE_TITLE in preview and "CHƯA ĐƯỢC DUYỆT" in preview
    assert "chưa có nội dung cụ thể" in preview
    assert "không xếp bài là ứng viên" not in preview
    assert "Bộ Y tế: hoàn tất, 1 bài trong kỳ (12 mục đã liệt kê khi thu thập)" in preview
    with pytest.raises(ValueError, match="cannot be revised"):
        issue_mod.revise(store, issue["run_id"], {}, expected_version=issue["issue_sha256"])
    issue_mod.approve(
        store, issue["run_id"], expected_version=issue["issue_sha256"], reviewer="R", rationale="ok"
    )
    [path] = issue_mod.export_email(store, issue["run_id"], SENDER, ["a@example.com"])
    message = message_from_bytes(open(path, "rb").read(), policy=policy.default)
    assert message["Subject"].startswith(f"HealthLab Signal · {issue_mod.NO_RELEASE_TITLE} · ")
    assert "Đã duyệt nội bộ" in message.get_body(("html",)).get_content()


class FakeSMTP:
    sent = []

    def login(self, user, password):
        pass

    def send_message(self, message, from_addr, to_addrs):
        FakeSMTP.sent.append(to_addrs[0])
        return {}

    def close(self):
        pass


def test_send_is_idempotent_per_recipient(tmp_path, monkeypatch):
    store, issue = create(tmp_path, monkeypatch)
    issue_mod.approve(
        store, issue["run_id"], expected_version=issue["issue_sha256"], reviewer="R", rationale="ok"
    )
    FakeSMTP.sent = []
    first = issue_mod.send_gmail(
        store, issue["run_id"], SENDER, ["a@example.com"], "pw", smtp_factory=FakeSMTP
    )
    again = issue_mod.send_gmail(
        store, issue["run_id"], SENDER, ["a@example.com"], "pw", smtp_factory=FakeSMTP
    )
    assert first[0]["status"] == "accepted" and again[0].get("skipped")
    assert FakeSMTP.sent == ["a@example.com"]
    assert store.load(issue["run_id"])["stage"] == "sent"


def test_transport_failure_after_login_is_unknown_and_not_resent(tmp_path, monkeypatch):
    store, issue = create(tmp_path, monkeypatch)
    issue_mod.approve(
        store, issue["run_id"], expected_version=issue["issue_sha256"], reviewer="R", rationale="ok"
    )

    class Broken(FakeSMTP):
        def send_message(self, *a, **k):
            raise smtplib.SMTPServerDisconnected("gone")

    [outcome] = issue_mod.send_gmail(
        store, issue["run_id"], SENDER, ["a@example.com"], "pw", smtp_factory=Broken
    )
    assert outcome["status"] == "unknown"
    [again] = issue_mod.send_gmail(
        store, issue["run_id"], SENDER, ["a@example.com"], "pw", smtp_factory=FakeSMTP
    )
    assert again == {"key": outcome["key"], "status": "unknown", "skipped": True}
