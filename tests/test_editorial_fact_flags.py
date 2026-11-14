"""gold-editorial-17 / fact-flags-1: reviewer doubts flag facts instead of blocking articles.

Synthetic text only. Topic, literal quotes and code-checked in-window dates still gate
release; a reviewer finding drops only the facts it names.
"""

import copy
import json
from types import SimpleNamespace

import pytest

from healthlab import editorial as ed
from healthlab.editorial.issue import _draft_body
from healthlab.editorial.review_policy import RELEASE_POLICY, review_gate
from tests.test_editorial import WINDOW, row
from tests.test_editorial_context import IDS, NULL_CONTEXT, TEXT, assessment, context, raw, review

pytestmark = pytest.mark.usefixtures("review_runtime")


V16 = "gold-editorial-16"
V17 = "gold-editorial-17"


def canonical(value=None, version=V17):
    article = row(TEXT)
    return ed.canonical_proposal(
        ed.resolve_reference_ids(value or raw(), article, version=version),
        article,
        WINDOW,
        version=version,
    )


def same_passage(**updates):
    return raw(
        passage_id="P1",
        supporting_passage_ids=[],
        context=context(status=None),
        date_link=dict(kind="same_passage", candidate_id="D1"),
        **updates,
    )


def with_undated_second_fact(value):
    second = copy.deepcopy(value["facts"][0])
    second.update(
        statement="Danh mục gồm nguy cơ ngã và loãng xương.",
        passage_id="P3",
        supporting_passage_ids=[],
        context=dict(NULL_CONTEXT),
        date_link=None,
    )
    value["facts"].append(second)
    return value


NA = dict(actor="not_applicable", document="not_applicable")
NA_ALL = NA | dict(population="not_applicable", status="not_applicable")


def finding(code, indexes):
    return dict(code=code, explanation="Lý do tổng hợp.", fact_indexes=indexes, passage_ids=[])


def test_policy_is_recorded_only_from_gold_editorial_17():
    assert {V16, V17} <= ed.READ_VERSIONS
    assert canonical()["release_policy"] == RELEASE_POLICY
    assert "release_policy" not in canonical(version=V16)


def test_missing_context_flags_under_v17_but_still_blocks_archived_v16():
    r = review(context_assessments=[assessment(population="missing")])
    gate = review_gate(canonical(), r, IDS, WINDOW)
    assert gate["eligible"] and not gate["review_clean"]
    assert gate["facts"][0]["flags"] == ["context_missing:population"]
    old = review_gate(canonical(version=V16), r, IDS, WINDOW)
    assert not old["eligible"] and "context_support_incomplete" in old["reasons"]


def test_unconfirmed_fact_and_citation_are_flags_not_blocks():
    r = review(
        supported_fact_indexes=[],
        supported_date_fact_indexes=[],
        supported_citation_fact_indexes=[],
        context_assessments=[assessment(status="not_applicable")],
    )
    gate = review_gate(canonical(same_passage()), r, IDS, WINDOW)
    assert gate["eligible"]
    assert gate["facts"][0]["flags"] == ["reviewer_not_confirmed", "citation_incomplete"]


def test_cross_passage_date_without_reviewer_confirmation_drops_the_fact():
    gate = review_gate(canonical(), review(supported_date_fact_indexes=[]), IDS, WINDOW)
    assert not gate["eligible"]
    assert gate["facts"][0]["drop_reasons"] == ["cross_passage_date_unconfirmed"]
    assert "supported_in_window_development_missing" in gate["reasons"]


def test_finding_drops_only_named_facts():
    p = canonical(with_undated_second_fact(raw()))
    base = dict(
        supported_fact_indexes=[0, 1],
        supported_citation_fact_indexes=[0, 1],
        context_assessments=[assessment(), assessment(1, **NA_ALL)],
    )
    side = review_gate(
        p, review(**base, blocking_findings=[finding("claim_not_supported", [1])]), IDS, WINDOW
    )
    assert side["eligible"] and not side["review_clean"]
    assert [f["released"] for f in side["facts"]] == [True, False]
    assert side["facts"][1]["drop_reasons"] == ["reviewer_finding:claim_not_supported"]
    dated = review_gate(
        p, review(**base, blocking_findings=[finding("status_overstated", [0])]), IDS, WINDOW
    )
    assert not dated["eligible"]
    assert dated["reasons"] == ["supported_in_window_development_missing"]


def test_article_level_findings_still_block():
    for item in (finding("other", []), finding("origin_unverified", [0])):
        gate = review_gate(canonical(), review(blocking_findings=[item]), IDS, WINDOW)
        assert not gate["eligible"] and "article_level_finding" in gate["reasons"]


def test_topic_disagreement_still_blocks():
    gate = review_gate(canonical(), review(relevance="uncertain"), IDS, WINDOW)
    assert not gate["eligible"] and "topic_unresolved_or_disagreed" in gate["reasons"]


def test_writer_caution_only_when_flags_exist():
    evidence = [dict(evidence_id="E1", quote="q")]
    plain = ed.article_messages(WINDOW, evidence)[0]["content"]
    flagged = ed.article_messages(
        WINDOW, [dict(evidence[0], review_flags=["context_missing:status"])]
    )[0]["content"]
    assert "review_flags" not in plain and "review_flags" in flagged
    assert flagged.startswith(plain)


def test_flags_show_in_approval_preview_but_not_in_email():
    evidence = dict(
        evidence_id="E1",
        url="https://example.org/a",
        title="Bài",
        quote="Trích dẫn.",
        review_flags=["context_missing:status"],
    )
    claim = SimpleNamespace(text="Câu.", evidence_ids=["E1"])
    section = SimpleNamespace(
        title="Mục", facts=[claim], interpretation="Ý nghĩa.", uncertainty="Giới hạn."
    )
    draft = SimpleNamespace(sections=[section])
    _, preview, _ = _draft_body(draft, {"E1": evidence}, True)
    _, email, _ = _draft_body(draft, {"E1": evidence}, False)
    assert any("Cần xem (E1): Thiếu trạng thái" in p for p in preview)
    assert not any("Cần xem" in p for p in email)
    assert json.dumps(email, ensure_ascii=False).count("Trích dẫn.") == 0
