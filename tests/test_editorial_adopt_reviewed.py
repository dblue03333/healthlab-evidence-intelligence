"""Review and scope results move to a new run only when its prompt is byte-identical."""

import pytest

from healthlab import editorial as ed
from tests.test_editorial import WINDOW, proposal, row, scope_assessment, setup_run
from tests.test_editorial_context import assessment, review
from tests.test_editorial_recovery import child_run, live_output

pytestmark = pytest.mark.usefixtures("review_runtime")


REVIEW = review(
    topic_passage_ids=["P2"],
    supported_date_fact_indexes=[],
    context_assessments=[
        assessment(
            actor="not_applicable",
            document="not_applicable",
            population="missing",
            status="not_applicable",
        )
    ],
)


def reviewed_parent(tmp_path, monkeypatch):
    store, parent, provider = setup_run(
        tmp_path, monkeypatch, [proposal(), REVIEW, scope_assessment()]
    )
    ed.run_calls(store, parent["run_id"], provider, workers=1)
    ed.plan_review(store, parent["run_id"])
    ed.run_calls(store, parent["run_id"], provider, role="review", workers=1)
    ed.plan_scope(store, parent["run_id"])
    ed.run_calls(store, parent["run_id"], provider, role="scope", workers=1)
    return store, parent, provider


def test_identical_prompts_adopt_review_and_scope_without_new_calls(tmp_path, monkeypatch):
    store, parent, provider = reviewed_parent(tmp_path, monkeypatch)
    child = child_run(tmp_path, store, provider, call_budget=10)
    ed.adopt_previous(store, child["run_id"], parent["run_id"])

    result = ed.adopt_reviewed(store, child["run_id"], parent["run_id"])

    assert result["review_adopted"] == 1 and result["scope_adopted"] == 1
    assert result["review_new_calls_needed"] == 0 and result["scope_new_calls_needed"] == 0
    saved = store.load(child["run_id"])
    calls = ed.jobs(store, saved)
    assert all(j["status"] == "completed" and j["charged"] is False for j in calls)
    assert ed.allocated_calls(saved, calls) == 3
    packet = ed.evidence_packet(store, child["run_id"])
    assert packet["articles"][0]["review_gate"]["eligible"]
    assert provider.calls == 3
    with pytest.raises(ValueError, match="already planned"):
        ed.adopt_reviewed(store, child["run_id"], parent["run_id"])


def test_a_different_prompt_leaves_the_call_for_a_new_dispatch(tmp_path, monkeypatch):
    store, parent, provider = reviewed_parent(tmp_path, monkeypatch)
    [old] = [j for j in ed.jobs(store, store.load(parent["run_id"])) if j["role"] == "review"]
    old["prompt_sha256"] = "0" * 64
    store.save(old)
    child = child_run(tmp_path, store, provider, call_budget=10)
    ed.adopt_previous(store, child["run_id"], parent["run_id"])

    result = ed.adopt_reviewed(store, child["run_id"], parent["run_id"])

    assert result["review_adopted"] == 0 and result["review_new_calls_needed"] == 1
    [job] = [j for j in ed.jobs(store, store.load(child["run_id"])) if j["role"] == "review"]
    assert job["status"] == "prepared" and "reused_from" not in job


def test_review_adoption_needs_classifications_adopted_first(tmp_path, monkeypatch):
    store, parent, provider = reviewed_parent(tmp_path, monkeypatch)
    child = child_run(tmp_path, store, provider, call_budget=10)
    with pytest.raises(ValueError, match="classifications first"):
        ed.adopt_reviewed(store, child["run_id"], parent["run_id"])


def test_release_policy_never_reaches_the_review_prompt():
    article = row()
    value = ed.canonical_proposal(ed.resolve_reference_ids(live_output(), article), article, WINDOW)
    assert "release_policy" in value
    bare = {k: v for k, v in value.items() if k != "release_policy"}
    assert ed.messages("review", article, WINDOW, value) == ed.messages(
        "review", article, WINDOW, bare
    )
