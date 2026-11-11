"""Approvals cannot survive unreviewed text, blockers or changed evidence."""

import pytest

from healthlab.editorial_release import approve_article, verify_approval
from healthlab.store import json_bytes


def case():
    article = b"Draft\n\nReported event.\n"
    evidence = json_bytes([{"evidence_id": "F1", "statement": "Reported event."}])
    review = {
        "actor_type": "agent",
        "actor": "reviewer",
        "authorization": "User delegated editorial review",
        "reviewed_at": "2026-11-02",
        "verdict": "approved_for_local_editorial_use",
        "unresolved_findings": [],
        "units": [
            dict(start=0, end=5, text="Draft", kind="metadata", evidence_ids=[], rationale="Title"),
            dict(
                start=7,
                end=22,
                text="Reported event.",
                kind="source_fact",
                evidence_ids=["F1"],
                rationale="Checked against saved source",
            ),
        ],
        "human_approval": False,
    }
    return article, evidence, review, {"source": b"Frozen source"}


def test_exact_version_is_agent_approved_without_human_or_publication_claim():
    args = case()
    approval = approve_article(*args)
    assert verify_approval(approval, *args) == approval
    assert approval["actor_type"] == "agent"
    assert not approval["human_approval"] and not approval["published"]


@pytest.mark.parametrize("field", ["article", "evidence", "review", "dependencies"])
def test_any_changed_review_input_invalidates_existing_approval(field):
    article, evidence, review, dependencies = case()
    approval = approve_article(article, evidence, review, dependencies)
    if field == "article":
        article += b"Unreviewed sentence."
    elif field == "evidence":
        evidence = json_bytes([{"evidence_id": "F1", "statement": "Changed fact"}])
    elif field == "review":
        review["actor"] = "another reviewer"
    else:
        dependencies["source"] = b"Changed frozen source"
    with pytest.raises(ValueError):
        verify_approval(approval, article, evidence, review, dependencies)


def test_open_findings_and_missing_review_coverage_block_approval():
    article, evidence, review, dependencies = case()
    review["unresolved_findings"] = ["Date unresolved"]
    with pytest.raises(ValueError, match="Unresolved"):
        approve_article(article, evidence, review, dependencies)
    review["unresolved_findings"] = []
    review["units"] = review["units"][:1]
    with pytest.raises(ValueError, match="Unreviewed"):
        approve_article(article, evidence, review, dependencies)


@pytest.mark.parametrize("references", [[], ["unknown"]])
def test_fact_without_valid_evidence_cannot_be_approved(references):
    article, evidence, review, dependencies = case()
    review["units"][1]["evidence_ids"] = references
    with pytest.raises(ValueError):
        approve_article(article, evidence, review, dependencies)


def test_agent_approval_cannot_claim_a_human_reviewer():
    article, evidence, review, dependencies = case()
    review["actor_type"] = "human"
    with pytest.raises(ValueError):
        approve_article(article, evidence, review, dependencies)
