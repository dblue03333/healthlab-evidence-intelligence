"""Draft exports stay bound to their evidence; offline recovery never loses charged calls."""

import copy
import json

import pytest

from healthlab import editorial as ed
from healthlab.editorial.contracts import ArticleProposal
from healthlab.editorial.report import write_report
from healthlab.provider import Completion
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


def live_output():
    """The classification as the model returns it: passages and dates cited by ID."""
    value = {k: v for k, v in proposal().items() if k not in {"relevance_quote", "facts"}}
    fact = proposal()["facts"][0]
    return dict(
        value,
        relevance_passage_id="P2",
        facts=[
            dict(
                statement=fact["statement"],
                passage_id="P1",
                supporting_passage_ids=[],
                event_date_candidate_id="D1",
                date_link_rationale=None,
                reported_status=fact["reported_status"],
            )
        ],
    )


def adopted_job(store, child):
    return ed.jobs(store, store.load(child["run_id"]))[0]


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


def test_recovery_reuses_live_output_that_cites_passages_by_id(tmp_path, monkeypatch):
    store, parent, provider = setup_run(tmp_path, monkeypatch, [proposal()])
    ed.run_calls(store, parent["run_id"], provider, workers=1)
    original = ed.jobs(store, store.load(parent["run_id"]))[0]
    child = child_run(tmp_path, store, provider, call_budget=5)

    result = ed.adopt_previous(store, child["run_id"], parent["run_id"])

    assert result["reused"] == 1 and result["new_primary_calls_planned"] == 0
    job = adopted_job(store, child)
    assert job["revalidation_route"] == "original_closed_schema"
    assert job["result_sha256"] == original["result_sha256"]
    assert provider.calls == 1


def test_recovery_unwraps_one_named_wrapper_around_live_output(tmp_path, monkeypatch):
    wrapped = Completion(
        json.dumps({"json": live_output()}, ensure_ascii=False),
        "fake-test",
        "stop",
        {"total_tokens": 5},
    )
    store, parent, provider = setup_run(tmp_path, monkeypatch, [wrapped])
    assert ed.run_calls(store, parent["run_id"], provider, workers=1)["classified"] == 0
    child = child_run(tmp_path, store, provider, call_budget=5)

    result = ed.adopt_previous(store, child["run_id"], parent["run_id"])

    assert result["prior_calls_charged"] == 1 and result["reused"] == 1
    job = adopted_job(store, child)
    assert job["status"] == "completed" and job["charged"] is False
    assert job["revalidation_route"] == "explicit_single_wrapper_json"
    packet = ed.evidence_packet(store, child["run_id"])
    assert packet["articles"][0]["proposal"]["relevance"] == "relevant"
    assert provider.calls == 1


def test_recovery_still_accepts_an_already_resolved_closed_schema(tmp_path, monkeypatch):
    store, parent, provider = setup_run(tmp_path, monkeypatch, [proposal()])
    ed.run_calls(store, parent["run_id"], provider, workers=1)
    original = ed.jobs(store, store.load(parent["run_id"]))[0]
    saved = json.loads(store.read(original["result_sha256"]))
    original["content_sha256"] = store.put(
        json_bytes({key: saved[key] for key in ArticleProposal.model_fields})
    )
    store.save(original)
    child = child_run(tmp_path, store, provider, call_budget=5)

    assert ed.adopt_previous(store, child["run_id"], parent["run_id"])["reused"] == 1
    assert adopted_job(store, child)["result_sha256"] == original["result_sha256"]


def test_recovery_never_reuses_output_citing_an_unknown_passage(tmp_path, monkeypatch):
    invalid = Completion(
        json.dumps(dict(live_output(), relevance_passage_id="P99"), ensure_ascii=False),
        "fake-test",
        "stop",
        {"total_tokens": 5},
    )
    store, parent, provider = setup_run(tmp_path, monkeypatch, [invalid])
    ed.run_calls(store, parent["run_id"], provider, workers=1)
    child = child_run(tmp_path, store, provider, call_budget=5)

    result = ed.adopt_previous(store, child["run_id"], parent["run_id"])

    assert result["reused"] == 0 and result["new_primary_calls_planned"] == 1
    assert adopted_job(store, child)["status"] == "prepared"
