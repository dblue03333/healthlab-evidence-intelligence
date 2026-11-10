"""Draft exports stay bound to their evidence; offline recovery never loses charged calls."""

import copy
import json

import pytest

from healthlab import editorial as ed
from healthlab.editorial.contracts import ArticleProposal
from healthlab.editorial.report import write_report
from healthlab.store import RunStore, json_bytes
from tests.test_editorial import proposal, scope_assessment, setup_run

REVIEW = dict(
    relevance="relevant",
    newsletter_eligible=True,
    supported_fact_indexes=[0],
    review_version="editorial-review-2",
    topic_passage_ids=["P2"],
    blocking_findings=[],
    audit_notes=[],
    rationale="Đoạn trích hỗ trợ đúng mốc và trạng thái thí điểm.",
)
DRAFT = dict(
    title="Thử nghiệm hỗ trợ phục hồi chức năng",
    sections=[
        dict(
            title="Một chương trình thí điểm",
            facts=[
                dict(
                    text="Nguồn báo cáo chương trình thí điểm bắt đầu ngày 10/10/2026.",
                    evidence_ids=["E1"],
                )
            ],
            interpretation="Có thể theo dõi thêm.",
            uncertainty="Chưa có dữ liệu hiệu quả.",
        )
    ],
)


def drafted_run(tmp_path, monkeypatch):
    store, run, provider = setup_run(
        tmp_path, monkeypatch, [proposal(), REVIEW, scope_assessment(), DRAFT]
    )
    ed.run_calls(store, run["run_id"], provider, workers=1)
    ed.plan_review(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="review", workers=1)
    ed.plan_scope(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="scope", workers=1)
    ed.plan_draft(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="draft", workers=1)
    return store, run, provider


def child_run(tmp_path, store, provider, call_budget):
    return ed.prepare(
        RunStore(tmp_path / "source"), store, "d" * 64, provider, call_budget=call_budget
    )


def store_closed_schema_output(store, parent):
    """Give the parent classification a saved output that recovery accepts as-is."""
    job = ed.jobs(store, store.load(parent["run_id"]))[0]
    saved = json.loads(store.read(job["result_sha256"]))
    job["content_sha256"] = store.put(
        json_bytes({key: saved[key] for key in ArticleProposal.model_fields})
    )
    store.save(job)


def test_draft_export_keeps_the_evidence_the_writer_was_given(tmp_path, monkeypatch):
    store, run, _ = drafted_run(tmp_path, monkeypatch)
    run, records = ed.load(store, run["run_id"])
    calls = ed.jobs(store, run)
    packet = copy.deepcopy(ed.evidence_packet(store, run["run_id"]))
    original = packet["draft_evidence"][0]
    # A later review makes an earlier article eligible, so positional IDs shift.
    earlier = dict(
        original, evidence_id="E1", url="https://example.org/other", title="Bài được duyệt sau"
    )
    packet["draft_evidence"] = [earlier, dict(original, evidence_id="E2")]

    write_report(store, run, records, calls, packet, tmp_path / "export")

    text = (tmp_path / "export" / "draft.md").read_text()
    assert "[E1](https://example.org/a)" in text
    assert "**E1 — Phục hồi chức năng tại cộng đồng**" in text
    assert "example.org/other" not in text and "Bài được duyệt sau" not in text
    assert "E2" not in text


def test_over_budget_recovery_is_rejected_before_any_job_changes(tmp_path, monkeypatch):
    store, parent, provider = drafted_run(tmp_path, monkeypatch)
    store_closed_schema_output(store, parent)
    child = child_run(tmp_path, store, provider, call_budget=2)

    with pytest.raises(ValueError, match="exceed original call budget"):
        ed.adopt_previous(store, child["run_id"], parent["run_id"])

    saved = store.load(child["run_id"])
    job = ed.jobs(store, saved)[0]
    assert job["status"] == "prepared" and "reused_from" not in job
    assert "parent_run_id" not in saved and saved["prior_call_count"] == 0
    # The untouched child still pays for its own classification.
    assert ed.allocated_calls(saved, [job]) == 1


def test_interrupted_recovery_keeps_prior_charges_and_can_resume(tmp_path, monkeypatch):
    store, parent, provider = setup_run(tmp_path, monkeypatch, [proposal()])
    ed.run_calls(store, parent["run_id"], provider, workers=1)
    store_closed_schema_output(store, parent)
    child = child_run(tmp_path, store, provider, call_budget=5)
    save = store.save

    def interrupted(manifest):
        if manifest.get("reused_from"):
            raise OSError("disk full")
        save(manifest)

    monkeypatch.setattr(store, "save", interrupted)
    with pytest.raises(OSError):
        ed.adopt_previous(store, child["run_id"], parent["run_id"])
    monkeypatch.setattr(store, "save", save)

    saved = store.load(child["run_id"])
    assert saved["parent_run_id"] == parent["run_id"] and saved["prior_call_count"] == 1
    assert saved["adoption_status"] == "started"
    assert ed.jobs(store, saved)[0]["status"] == "prepared"
    assert ed.allocated_calls(saved, ed.jobs(store, saved)) == 2

    result = ed.adopt_previous(store, child["run_id"], parent["run_id"])

    assert result["prior_calls_charged"] == 1 and result["reused"] == 1
    saved = store.load(child["run_id"])
    job = ed.jobs(store, saved)[0]
    assert saved["adoption_status"] == "completed"
    assert job["status"] == "completed" and job["reused_from"] and job["charged"] is False
    assert ed.allocated_calls(saved, [job]) == 1
    with pytest.raises(ValueError, match="already recorded"):
        ed.adopt_previous(store, child["run_id"], parent["run_id"])
    assert provider.calls == 1
