"""Second-model recall check over held-back articles, offline.

Synthetic text only. A fake "second model" rereads every article the run did not release
with the scope prompt. Its flags appear only in the approval preview; the approver writes
the sentence for an article without classifier facts, and the approval records which flags
were promoted.
"""

import json
from types import SimpleNamespace

import pytest

from healthlab import editorial as ed
from healthlab.data.contracts import Gold, Window
from healthlab.editorial import issue as issue_mod
from healthlab.editorial import recall as recall
from healthlab.editorial import runs as editorial_runs
from healthlab.models import IngestionError
from healthlab.provider import Completion, FakeProvider
from healthlab.store import RunStore
from tests.test_editorial import WINDOW, proposal, row, scope_assessment, setup_run
from tests.test_editorial_side_news import DRAFT, HELD

OFF = "off"
OFF_URL = "https://example.org/off"
MENTION = {"verdict": "mention_only", "ageing_context": None, "substantive_detail": None}


def off_topic():
    return proposal(relevance="irrelevant", topics=[], facts=[], content_kind="background")


def finished_issue(tmp_path, monkeypatch):
    """ "id" passes; "held" is on topic but held; "off" is judged off topic with no facts."""
    store, _, provider = setup_run(
        tmp_path,
        monkeypatch,
        [
            proposal(),  # held
            proposal(),  # id
            off_topic(),  # off
            scope_assessment(**MENTION),  # held
            scope_assessment(),  # id
            DRAFT,
        ],
    )
    articles = [
        row(),
        dict(row(), document_id=HELD, url="https://example.org/held"),
        dict(row(), document_id=OFF, url=OFF_URL, title="Một ca phẫu thuật"),
    ]
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
        candidates=3,
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
    return store, run["run_id"], issue


def second_model(*responses):
    return FakeProvider(
        [
            r
            if isinstance(r, Exception)
            else Completion(json.dumps(r, ensure_ascii=False), "fake-test", "stop", {})
            for r in responses
        ]
    )


def preview(store, issue):
    issue_mod.render_preview(store, issue["run_id"])
    root = store.root / "runs" / issue["run_id"]
    return (root / "newsletter.html").read_text(), (root / "newsletter.txt").read_text()


def test_held_back_articles_are_rechecked_and_flags_reach_only_the_preview(tmp_path, monkeypatch):
    store, run_id, issue = finished_issue(tmp_path, monkeypatch)
    assert "Có thể bị sót" not in preview(store, issue)[0]
    provider = second_model(scope_assessment(), scope_assessment())
    record = recall.check(store, run_id, provider)
    # Only the two held-back articles are read; the released one is not.
    assert record["targets"] == [HELD, OFF] and provider.calls == 2
    assert record["status"] == "completed" and len(record["requests"]) == 2
    assert store.load(run_id)["recall_check_id"] == record["run_id"]

    again = recall.check(store, run_id, second_model())
    assert again["calls"] == 2  # Completed results are never asked again.

    result = recall.summary(store, run_id)
    assert [f["document_id"] for f in result["flags"]] == [HELD, OFF]
    assert result["flags"][1]["passages"][0]["passage_id"] == "P2"
    html, text = preview(store, issue)
    assert "Có thể bị sót (AI rà soát thứ 2: fake-test" in html and OFF_URL in html
    assert "2/2 bài bị loại đã được rà" in html
    assert f"--document-id {OFF} --text" in html
    # The held article is already a near miss; it gets a note rather than a second entry.
    assert html.count("https://example.org/held") == 1
    assert "AI rà soát thứ 2 cũng cho rằng" in html
    assert "bị sót" not in text and OFF_URL not in text


def test_mention_only_and_outside_knowledge_are_not_flagged(tmp_path, monkeypatch):
    store, run_id, issue = finished_issue(tmp_path, monkeypatch)
    recall.check(
        store,
        run_id,
        second_model(
            scope_assessment(**MENTION), scope_assessment(requires_outside_knowledge=True)
        ),
    )
    assert recall.summary(store, run_id)["flags"] == []
    html, _ = preview(store, issue)
    assert "Không có bài nào bị gắn cờ" in html and OFF_URL not in html


def test_a_failed_call_is_retried_on_the_next_run_only(tmp_path, monkeypatch):
    store, run_id, issue = finished_issue(tmp_path, monkeypatch)
    failing = second_model(scope_assessment(), IngestionError("provider_timeout", "timed out"))
    record = recall.check(store, run_id, failing)
    assert record["status"] == "partial"
    failed = record["results"][OFF]
    assert (failed["status"], failed["error_code"]) == ("failed", "provider_timeout")
    assert "1/2 bài bị loại đã được rà, 1 bài lỗi chưa rà được" in preview(store, issue)[0]

    retry = second_model(scope_assessment())
    record = recall.check(store, run_id, retry)
    assert retry.calls == 1 and record["status"] == "completed" and record["calls"] == 3


def test_a_wrong_model_or_an_unknown_passage_is_a_failed_result(tmp_path, monkeypatch):
    store, run_id, _ = finished_issue(tmp_path, monkeypatch)
    provider = FakeProvider(
        [
            Completion(json.dumps(scope_assessment()), "other-model", "stop", {}),
            Completion(json.dumps(scope_assessment(passage_ids=["P99"])), "fake-test", "stop", {}),
        ]
    )
    record = recall.check(store, run_id, provider)
    assert [record["results"][d]["status"] for d in (HELD, OFF)] == ["failed", "failed"]
    assert record["results"][HELD]["error_code"] == "recall_model_identity"
    assert recall.summary(store, run_id)["flags"] == []


def test_a_check_cannot_switch_models_midway(tmp_path, monkeypatch):
    store, run_id, _ = finished_issue(tmp_path, monkeypatch)
    recall.check(store, run_id, second_model(scope_assessment()), max_calls=1)
    other = second_model()
    other.identity = lambda: {"provider": "fake", "model": "another"}
    with pytest.raises(ValueError, match="another model"):
        recall.check(store, run_id, other)


def test_approver_writes_the_sentence_for_a_flagged_article_without_facts(tmp_path, monkeypatch):
    store, run_id, issue = finished_issue(tmp_path, monkeypatch)
    recall.check(store, run_id, second_model(scope_assessment(**MENTION), scope_assessment()))
    version = issue["issue_sha256"]
    with pytest.raises(ValueError, match="together"):
        issue_mod.promote(store, issue["run_id"], OFF, expected_version=version, text="Câu.")
    with pytest.raises(ValueError, match="not flagged"):
        issue_mod.promote(
            store, issue["run_id"], HELD, expected_version=version, text="Câu.", status="pilot"
        )
    with pytest.raises(ValueError, match="not both"):
        issue_mod.promote(
            store,
            issue["run_id"],
            OFF,
            expected_version=version,
            fact_indexes=[0],
            text="Câu.",
            status="pilot",
        )
    # Without --text the article has no classifier facts to promote.
    with pytest.raises(ValueError, match="no classifier facts"):
        issue_mod.promote(store, issue["run_id"], OFF, expected_version=version)

    sentence = "Theo bài đăng ngày 11/10/2026, một chương trình hỗ trợ người trên 65 tuổi tại nhà."
    promoted = issue_mod.promote(
        store, issue["run_id"], OFF, expected_version=version, text=sentence, status="operational"
    )
    assert promoted["revisions"][-1]["promoted"]["text_by"] == "approver"
    _, draft, by_id = issue_mod.load_issue(store, issue["run_id"])
    [item] = draft.side_news
    assert item.text == sentence and item.evidence_ids == ["S1"]
    evidence = by_id["S1"]
    assert evidence["document_id"] == OFF and evidence["passage_id"] == "P2"
    assert evidence["time_basis"] == "publication_date"
    html, text = preview(store, promoted)
    assert sentence in text and "Có thể bị sót" in html
    assert "Không có bài nào bị gắn cờ" in html  # The promoted article leaves the list.

    approved = issue_mod.approve(
        store,
        issue["run_id"],
        expected_version=promoted["issue_sha256"],
        reviewer="r",
        rationale="ok",
    )
    event = json.loads(store.read(approved["approval_sha256"]))
    assert event["recall_outcomes"]["flags"] == [{"document_id": OFF, "included": True}]
    assert event["recall_outcomes"]["model"] == "fake-test"


def test_an_ignored_flag_is_recorded_as_not_included(tmp_path, monkeypatch):
    store, run_id, issue = finished_issue(tmp_path, monkeypatch)
    recall.check(store, run_id, second_model(scope_assessment(), scope_assessment()))
    approved = issue_mod.approve(
        store, issue["run_id"], expected_version=issue["issue_sha256"], reviewer="r", rationale="ok"
    )
    event = json.loads(store.read(approved["approval_sha256"]))
    assert event["recall_outcomes"]["flags"] == [
        {"document_id": HELD, "included": False},
        {"document_id": OFF, "included": False},
    ]


def test_the_recall_check_is_outside_the_editorial_runtime_fingerprint():
    # Adding or changing this check must not turn a finished editorial run read-only.
    for name in ("editorial/recall.py", "editorial/issue.py", "editorial/mail.py"):
        assert name not in editorial_runs._RUNTIME_FILES


def test_recall_provider_uses_glm_with_provider_default_thinking(monkeypatch):
    monkeypatch.setenv("FPT_API_KEY", "test-key")
    monkeypatch.delenv("HEALTHLAB_RECALL_MODEL", raising=False)
    provider = recall.recall_provider()
    try:
        identity = provider.identity()
        assert identity["model"] == "GLM-5.2" and identity["request_options"] == {}
    finally:
        provider.close()
