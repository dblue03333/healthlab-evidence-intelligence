"""Review notes, genuine blockers, exact evidence references and archive safety."""

import hashlib

import pytest

from healthlab import editorial as ed
from healthlab.editorial.review_policy import review_gate as policy_gate
from tests.test_editorial import WINDOW, proposal, setup_run


def review_gate(proposal_value, review_value, passage_ids):
    return policy_gate(proposal_value, review_value, passage_ids, WINDOW)


def review(**updates):
    return (
        dict(
            review_version="editorial-review-2",
            relevance="relevant",
            newsletter_eligible=True,
            topic_passage_ids=["P2"],
            supported_fact_indexes=[0],
            blocking_findings=[],
            audit_notes=["Fact supported", "No outcome evaluation provided"],
            rationale="Nguồn có đoạn hỗ trợ.",
        )
        | updates
    )


def candidate():
    return dict(proposal(), newsletter_candidate=True)


def test_positive_notes_do_not_block_a_supported_candidate():
    assert review_gate(candidate(), review(), ["P1", "P2"])["eligible"]


def test_real_blocker_is_never_cleared_by_positive_notes():
    r = review(
        newsletter_eligible=False,
        blocking_findings=[
            dict(
                code="status_overstated",
                explanation="Kế hoạch bị viết thành kết quả",
                fact_indexes=[0],
                passage_ids=["P1"],
            )
        ],
    )
    gate = review_gate(candidate(), r, ["P1", "P2"])
    assert not gate["eligible"] and "blocking_findings" in gate["reasons"]


def test_legacy_free_text_is_not_guessed_even_if_it_says_no_issue():
    for issues in [[], ["No issue, fact fully supported"], ["Unsupported claim"]]:
        r = dict(
            relevance="relevant",
            newsletter_eligible=True,
            supported_fact_indexes=[0],
            issues=issues,
            rationale="Legacy",
        )
        assert review_gate(candidate(), r, ["P1", "P2"])["reasons"] == [
            "legacy_review_requires_explicit_assessment"
        ]


@pytest.mark.parametrize(
    "updates",
    [
        dict(topic_passage_ids=["P999"]),
        dict(supported_fact_indexes=[99]),
        dict(supported_fact_indexes=[True]),
        dict(supported_fact_indexes=[0, 0]),
        dict(newsletter_eligible=True, relevance="irrelevant"),
        dict(
            newsletter_eligible=True,
            blocking_findings=[
                dict(code="other", explanation="Unresolved", fact_indexes=[], passage_ids=[])
            ],
        ),
    ],
)
def test_invalid_review_cannot_release_evidence(updates):
    assert not review_gate(candidate(), review(**updates), ["P1", "P2"])["eligible"]


def test_missing_topic_or_background_candidate_stays_blocked():
    p = candidate()
    p["newsletter_candidate"] = False
    assert not review_gate(p, review(), ["P1", "P2"])["eligible"]


@pytest.mark.parametrize(
    "archive_version",
    [
        "gold-editorial-2",
        "gold-editorial-3",
        "gold-editorial-4",
        "gold-editorial-5",
        "gold-editorial-6",
        "gold-editorial-7",
        "gold-editorial-8",
        "gold-editorial-9",
        "gold-editorial-10",
    ],
)
def test_known_archive_is_readable_but_not_mutable_and_export_preserves_old_files(
    tmp_path, monkeypatch, archive_version
):
    store, run, provider = setup_run(tmp_path, monkeypatch, [proposal()])
    ed.run_calls(store, run["run_id"], provider, workers=1)
    saved = store.load(run["run_id"])
    saved.update(version=archive_version, runtime_sha256="a" * 64)
    # This synthetic archive must use the historical fact contract, not v13/v14 fields.
    for job in ed.jobs(store, saved):
        if job.get("result_sha256"):
            import json

            value = json.loads(store.read(job["result_sha256"]))
            for field in ("facts", "resolved_facts"):
                for fact in value.get(field, []):
                    fact.pop("supporting_passages", None)
                    fact.pop("context", None)
            value.pop("context_contract", None)
            job["result_sha256"] = store.put(ed.json_bytes(value))
            store.save(job)
    saved.pop("passage_policy")  # Historic manifests predate explicit passage policy metadata.
    store.save(saved)
    directory = store.root / "runs" / run["run_id"]
    before = {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in directory.iterdir()
        if p.is_file()
    }
    assert ed.load(store, run["run_id"])[0]["version"] == archive_version
    with pytest.raises(ValueError, match="read-only"):
        ed.run_calls(store, run["run_id"], provider, workers=1)
    ed.export_report(store, run["run_id"])
    assert before == {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in directory.iterdir()
        if p.is_file()
    }
    assert (store.root / "inspections" / run["run_id"] / "report.html").is_file()


def test_unsupported_dated_fact_cannot_release_only_undated_supported_facts():
    p = candidate()
    p["facts"].append(
        dict(
            statement="Undated",
            quote="Hỗ trợ nhóm trên 65 tuổi tự sinh hoạt tại nhà.",
            event_date=None,
            reported_status="pilot",
        )
    )
    r = review(supported_fact_indexes=[1])
    assert "supported_in_window_development_missing" in review_gate(p, r, ["P1", "P2"])["reasons"]
