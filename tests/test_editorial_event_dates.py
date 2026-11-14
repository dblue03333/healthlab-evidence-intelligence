"""Explicit day-first dates, versioned date IDs, and operator diagnostics."""

import json

import pytest

from healthlab import editorial as ed
from healthlab.editorial_review_policy import fact_date_checks, validate_review
from healthlab.store import json_bytes
from tests import test_editorial as fixtures
from tests.test_editorial import WINDOW, row, scope_assessment, setup_run
from tests.test_editorial_context import TEXT, raw, review
from tests.test_editorial_recovery import child_run

pytestmark = pytest.mark.usefixtures("review_runtime")


V15 = "gold-editorial-15"
HYPHENATED = TEXT.replace("10/10/2026", "10-10-2026")


def test_day_first_date_reaches_native_draft_evidence(tmp_path, monkeypatch):
    article = row(HYPHENATED)
    monkeypatch.setattr(fixtures, "row", lambda: article)
    store, run, provider = setup_run(tmp_path, monkeypatch, [raw(), review(), scope_assessment()])
    _, old = ed.passage_inputs(article, version=V15)
    _, current = ed.passage_inputs(article)
    assert old == []
    assert [(d["candidate_id"], d["quote"], d["date"]) for d in current] == [
        ("D1", "10-10-2026", "2026-10-10")
    ]
    ed.run_calls(store, run["run_id"], provider, workers=1)
    ed.plan_review(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="review", workers=1)
    ed.plan_scope(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="scope", workers=1)

    packet = ed.evidence_packet(store, run["run_id"])
    proposal = packet["articles"][0]["proposal"]
    assert packet["articles"][0]["review_gate"]["eligible"]
    [fact] = packet["draft_evidence"]
    assert fact["event_date"] == "2026-10-10" and fact["date_evidence"]["quote"] in HYPHENATED
    assert not validate_review(
        review(supported_date_fact_indexes=[]), proposal, ["P1", "P2", "P3"], WINDOW
    )["newsletter_eligible"]


def test_report_distinguishes_partial_dates_and_non_event_milestones(tmp_path, monkeypatch):
    article = row(
        TEXT.replace("10/10/2026", "10/10") + " Tính đến ngày 28-09-2026, một số đơn vị đã khám. "
        "Đến nay tỷ lệ đạt 95%. Hạn chót là ngày 12-10-2026. "
        "Chương trình kéo dài từ 1-7 đến 15-12-2026. "
        "Ngày 31-02-2026 là ngày không hợp lệ. Tỷ lệ hoàn thành là 13/16."
    )
    monkeypatch.setattr(fixtures, "row", lambda: article)
    store, run, provider = setup_run(
        tmp_path,
        monkeypatch,
        [raw(date_link=None), review(supported_date_fact_indexes=[]), scope_assessment()],
    )
    ed.run_calls(store, run["run_id"], provider, workers=1)
    ed.plan_review(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="review", workers=1)
    ed.plan_scope(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="scope", workers=1)
    packet = ed.evidence_packet(store, run["run_id"])
    assert packet["draft_evidence"] == []
    assert packet["articles"][0]["proposal"]["facts"][0]["event_date"] is None

    directory = store.root / "runs" / run["run_id"]
    [details] = json.loads((directory / "date-review.json").read_text())
    assert {(m["kind"], m.get("date"), m.get("inside_window")) for m in details["mentions"]} == {
        ("yearless", None, None),
        ("as_of_without_date", None, None),
        ("as_of", "2026-09-28", False),
        ("deadline", "2026-10-12", True),
        ("range_endpoint", "2026-12-15", False),
    }
    assert "Đối chiếu lý do giữ bài về ngày" in (directory / "report.html").read_text()


def test_new_review_gets_date_role_without_changing_historical_date_checks():
    article = row(TEXT.replace("Ngày 10/10/2026", "Tính đến ngày 10-10-2026"))
    proposal = ed.canonical_proposal(ed.resolve_reference_ids(raw(), article), article, WINDOW)
    [check] = fact_date_checks(proposal, WINDOW)
    assert check["date_role_hints"] == ["as_of"]
    old = ed.canonical_proposal(
        ed.resolve_reference_ids(raw(), article, version=V15), article, WINDOW, version=V15
    )
    [old_check] = fact_date_checks(old, WINDOW)
    assert "date_role_hints" not in old_check and old_check["event_date"] is None


def test_recovery_skips_historical_ids_when_candidate_catalog_changes(tmp_path, monkeypatch):
    article = row(HYPHENATED + " Ngày 11/10/2026 diễn ra hội nghị khác.")
    monkeypatch.setattr(fixtures, "row", lambda: article)
    store, child, provider = setup_run(tmp_path, monkeypatch, [])
    parent = child_run(tmp_path, store, provider, call_budget=5)
    parent["version"] = V15
    [old_job] = ed.jobs(store, parent)
    old_job.update(
        status="completed",
        finish_reason="stop",
        actual_model=parent["provider"]["model"],
        content_sha256=store.put(json_bytes(raw())),
    )
    store.save(old_job)
    store.save(parent)
    assert ed.passage_inputs(article, version=V15)[1][0]["date"] == "2026-10-11"
    assert ed.passage_inputs(article)[1][0]["date"] == "2026-10-10"
    result = ed.adopt_previous(store, child["run_id"], parent["run_id"])
    assert result["reused"] == 0
    assert ed.jobs(store, child)[0]["status"] == "prepared"
