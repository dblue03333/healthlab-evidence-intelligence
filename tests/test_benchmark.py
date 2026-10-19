import json

import pytest

from healthlab.benchmark import (
    freeze,
    load_frozen,
    report,
    retrieval_metrics,
    run_baseline,
    score_reviews,
)
from healthlab.models import IngestionError
from healthlab.provider import FakeProvider
from healthlab.store import RunStore
from tests.test_extraction import completion, second_completion
from tests.test_ingestion import connector, normal_handler
from tests.test_orchestration import empty_brief


def test_precision_denominator_missing_and_uncertain():
    metrics = retrieval_metrics(
        [{"label": x} for x in ["direct", "indirect", None, "irrelevant", "uncertain"]]
    )
    assert metrics["precision_at_5"] is None
    assert metrics["judgment_coverage"]["value"] == 3 / 5
    assert metrics["direct_precision_bounds_at_5"] == [0.2, 0.6]
    assert metrics["recall_at_5"] is None
    final = retrieval_metrics([{"label": "direct"}, {"label": "irrelevant"}])
    assert final["precision_at_5"]["value"] == 0.2
    assert final["precision_among_judged"]["value"] == 0.5
    assert retrieval_metrics([])["precision_at_5"]["value"] == 0


def test_factual_denominators_are_not_json_validity():
    rows = [
        {"label": label, "severity": "critical" if label == "incorrect" else None}
        for label in ["correct", "incorrect", "missing", "not_assessable", None]
    ]
    scored = score_reviews(rows)
    assert scored["correct_among_assessable"]["value"] == 1 / 3
    assert scored["reviewed"]["value"] == 4 / 5
    assert scored["critical_errors"] == 1
    assert score_reviews([{"label": None}])["correct_among_assessable"]["value"] is None


def setup(tmp_path):
    suite = {
        "k": 5,
        "cases": [
            {"id": "Q1", "question": "q", "query": "q", "scope": "test", "sort": "relevance"}
        ],
    }
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(json.dumps(suite))
    store = RunStore(tmp_path / "store")
    client, _ = connector(normal_handler)
    freeze(suite_path, tmp_path / "frozen", store, client)
    return store, tmp_path / "frozen/frozen.json"


def test_freeze_run_report_and_resume_without_network(tmp_path):
    store, frozen = setup(tmp_path)
    baseline = run_baseline(
        store,
        frozen,
        tmp_path / "baseline",
        FakeProvider([completion(), second_completion(), empty_brief()]),
    )
    assert baseline["cases"][0]["status"] == "completed"
    assert run_baseline(store, frozen, tmp_path / "baseline", FakeProvider([])) == baseline
    metrics = report(store, frozen, tmp_path / "baseline/baseline.json", tmp_path / "report")
    assert metrics["factual_fields"]["correct_among_assessable"]["value"] is None
    reviews = json.loads((tmp_path / "report/reviews.json").read_text())
    reviews["retrieval"][0].update(
        label="direct", reviewer="model", reviewer_type="ai", rationale="test"
    )
    (tmp_path / "report/reviews.json").write_text(json.dumps(reviews))
    with pytest.raises(ValueError, match="human"):
        report(store, frozen, tmp_path / "baseline/baseline.json", tmp_path / "report")


def test_frozen_manifest_mutation_is_rejected(tmp_path):
    store, frozen = setup(tmp_path)
    loaded = load_frozen(store, frozen)
    ing = store.load(loaded["cases"][0]["ingestion_run_id"])
    ing["question"]["query"] = "changed"
    store.save(ing)
    with pytest.raises(IngestionError, match="changed"):
        load_frozen(store, frozen)


def test_source_failure_is_not_precision_zero(tmp_path):
    import httpx

    store = RunStore(tmp_path / "store")
    suite = tmp_path / "suite.json"
    suite.write_text(
        json.dumps(
            {
                "k": 5,
                "cases": [
                    {"id": "Q1", "question": "q", "query": "q", "scope": "s", "sort": "relevance"}
                ],
            }
        )
    )
    freeze(suite, tmp_path / "frozen", store, connector(lambda _: httpx.Response(400))[0])
    run_baseline(store, tmp_path / "frozen/frozen.json", tmp_path / "baseline", FakeProvider([]))
    metrics = report(
        store,
        tmp_path / "frozen/frozen.json",
        tmp_path / "baseline/baseline.json",
        tmp_path / "report",
    )
    assert metrics["retrieval"]["Q1"] == {"status": "source_failed_not_zero_relevance"}


def test_invalid_labels_do_not_silently_score_as_missing():
    with pytest.raises(ValueError):
        retrieval_metrics([{"label": "relevant"}])
    with pytest.raises(ValueError):
        score_reviews([{"label": "pass"}])


def test_human_reviews_survive_report_regeneration(tmp_path):
    store, frozen = setup(tmp_path)
    run_baseline(
        store,
        frozen,
        tmp_path / "baseline",
        FakeProvider([completion(), second_completion(), empty_brief()]),
    )
    baseline = tmp_path / "baseline/baseline.json"
    report(store, frozen, baseline, tmp_path / "review")
    path = tmp_path / "review/reviews.json"
    reviews = json.loads(path.read_text())
    for index, row in enumerate(reviews["retrieval"]):
        row.update(
            label="direct" if index == 0 else "irrelevant",
            reviewer="fixture-reviewer",
            reviewer_type="human",
            rationale="Synthetic test label, not a clinical judgment",
        )
    path.write_text(json.dumps(reviews))
    result = report(store, frozen, baseline, tmp_path / "review")
    assert result["retrieval"]["Q1"]["precision_at_5"]["value"] == 0.2
    assert json.loads(path.read_text()) == reviews
    reviews["retrieval"][0]["case_id"] = "different"
    path.write_text(json.dumps(reviews))
    with pytest.raises(ValueError, match="metadata"):
        report(store, frozen, baseline, tmp_path / "review")


def test_model_change_requires_new_baseline_directory(tmp_path):
    store, frozen = setup(tmp_path)
    run_baseline(
        store,
        frozen,
        tmp_path / "baseline",
        FakeProvider([completion(), second_completion(), empty_brief()]),
    )
    different = FakeProvider([])
    different.identity = lambda: {"model": "other"}
    with pytest.raises(IngestionError, match="new destination"):
        run_baseline(store, frozen, tmp_path / "baseline", different)


def test_fresh_baseline_bypasses_existing_cache(tmp_path):
    store, frozen = setup(tmp_path)
    run_baseline(
        store,
        frozen,
        tmp_path / "cached",
        FakeProvider([completion(), second_completion(), empty_brief()]),
    )
    fresh = run_baseline(
        store,
        frozen,
        tmp_path / "fresh",
        FakeProvider([completion(), second_completion(), empty_brief()]),
        refresh=True,
    )
    stage = fresh["cases"][0]["stages"]["extraction"]
    assert stage["cache_hits"] == 0
    assert stage["api_calls"] == 2
    assert fresh["cases"][0]["status"] == "completed"
    with pytest.raises(IngestionError, match="changed"):
        run_baseline(store, frozen, tmp_path / "fresh", FakeProvider([]))
