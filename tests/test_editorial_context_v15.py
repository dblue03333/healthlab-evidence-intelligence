"""gold-editorial-15 / fact-context-2: repair or drop one facet, never the whole article.

Synthetic text only. The failure modes mirror the 11 Nov 2026 measurement
(reports/data-pipeline/2026-11-11/context-date/audit.md): case mismatch, quote from
another passage, ellipsis, paraphrase, repeated phrase, whole-sentence facets and a
yearless ceremony linked to the end of an anniversary range.
"""

import copy
import json

import pytest
from pydantic import ValidationError

from healthlab import editorial as ed
from healthlab.editorial_context import (
    MAX_CONTEXT_CHARS,
    date_is_range_endpoint,
    rationale_date_conflict,
)
from healthlab.editorial_review_policy import fact_date_checks, review_gate, validate_review
from tests.test_editorial import WINDOW, row, scope_assessment, setup_run
from tests.test_editorial_context import IDS, TEXT, assessment, bind, context, raw, review

V15 = "gold-editorial-15"
CEREMONY = (
    "Ngày 24/10, Sở Y tế dự lễ kỷ niệm 50 năm (27/10/1976 – 27/10/2026) của Bệnh viện Lão khoa. "
    "Bệnh viện ra mắt Đề án chăm sóc người cao tuổi tại nhà. "
    "Đề án đang thí điểm cho người từ 75 tuổi."
)
LONG = (
    "Ngày 10/10/2026, Bộ Y tế công bố dự thảo hướng dẫn chăm sóc dài hạn cho người từ 60 tuổi "
    "sống một mình tại các phường, xã có tỷ lệ già hóa cao nhất cả nước."
)


def resolve(value=None, text=TEXT):
    return ed.resolve_reference_ids(value or raw(), row(text), version=V15)


def canonical(value=None, text=TEXT):
    return ed.canonical_proposal(resolve(value, text), row(text), WINDOW, version=V15)


def repairs(p):
    return [(r["field"], r["action"], r["reason"]) for r in p["binding_repairs"]]


# Facet repair: only surface differences that still resolve to one exact source span.


def test_valid_bindings_need_no_repair_and_record_the_contract():
    p = canonical()
    assert p["context_contract"] == "fact-context-2"
    assert p["binding_repairs"] == []
    assert p["newsletter_candidate"]


def test_case_or_whitespace_difference_resolves_to_the_exact_source_span():
    p = canonical(raw(context=context(actor=bind("P1", "bộ  y tế"))))
    actor = p["resolved_facts"][0]["context"]["actor"]
    assert actor["quote"] == "Bộ Y tế"
    assert row(TEXT)["body"][actor["start"] : actor["end"]] == "Bộ Y tế"
    assert repairs(p) == [("actor", "normalized", "case_or_whitespace")]


def test_quote_found_once_in_another_attached_passage_is_rebound():
    p = canonical(raw(context=context(actor=bind("P3", "Bộ Y tế"))))
    assert p["resolved_facts"][0]["context"]["actor"]["passage_id"] == "P1"
    assert repairs(p) == [("actor", "rebound", "found_in_other_attached_passage")]


@pytest.mark.parametrize(
    ("binding", "reason"),
    [
        (bind("P1", "người cao tuổi"), "not_found_in_attached_support"),  # paraphrase
        (bind("P1", "Bộ Y tế ... 60 tuổi"), "ellipsis_or_joined_fragments"),
        (bind("P1", "Bộ Y tế…tuổi"), "ellipsis_or_joined_fragments"),
        (bind("P1", "t"), "ambiguous_in_bound_passage"),
        (bind("P2", "y"), "ambiguous_in_attached_support"),  # absent from P2; P1 and P3 both contain it
    ],
)
def test_unrepairable_binding_drops_only_that_facet(binding, reason):
    p = canonical(raw(context=context(actor=binding)))
    fact = p["resolved_facts"][0]
    assert fact["context"]["actor"] is None
    assert fact["context"]["population"]["quote"] == "người từ 60 tuổi"
    assert repairs(p) == [("actor", "dropped", reason)]
    assert p["relevance"] == "relevant"


def test_quote_only_in_an_unattached_passage_is_dropped_not_borrowed():
    value = raw(supporting_passage_ids=["P3"], date_link=None)
    p = canonical(value)
    ctx = p["resolved_facts"][0]["context"]
    assert ctx["actor"] is ctx["document"] is ctx["population"] is None
    assert ctx["status"]["quote"] == "đề xuất"
    assert {r[0] for r in repairs(p)} == {"actor", "document", "population"}


def test_whole_sentence_facet_is_dropped_as_overlong():
    assert len(LONG) > MAX_CONTEXT_CHARS
    value = raw(
        passage_id="P1",
        supporting_passage_ids=[],
        context=dict(actor=bind("P1", LONG), document=None, population=None, status=None),
        date_link=dict(kind="same_passage", candidate_id="D1"),
    )
    p = canonical(value, LONG)
    assert p["resolved_facts"][0]["context"]["actor"] is None
    assert repairs(p) == [("actor", "dropped", "overlong_not_a_phrase")]


def test_every_facet_invalid_still_yields_a_valid_article():
    garbage = dict(
        actor=bind("P2", "Bộ Y tế ... đề xuất"),
        document=bind("P2", "Nghị định 99"),
        population=bind("P2", "người cao tuổi"),
        status=bind("P2", "đã ban hành"),
    )
    p = canonical(raw(context=garbage))
    assert all(v is None for v in p["resolved_facts"][0]["context"].values())
    assert len(p["binding_repairs"]) == 4


def test_overlong_or_tampered_binding_cannot_reenter_canonical_proposal():
    value = resolve(
        raw(
            passage_id="P1",
            supporting_passage_ids=[],
            context=dict(actor=None, document=None, population=None, status=None),
            date_link=None,
        ),
        LONG,
    )
    fact = value["facts"][0]
    start = fact["supporting_passages"][0]["start"]
    fact["context"]["actor"] = dict(passage_id="P1", quote=LONG, start=start, end=start + len(LONG))
    with pytest.raises(ValueError, match="longer than a context phrase"):
        ed.canonical_proposal(value, row(LONG), WINDOW, version=V15)
    tampered = resolve()
    tampered["facts"][0]["context"]["actor"]["start"] += 1
    with pytest.raises(ValueError):
        ed.canonical_proposal(tampered, row(TEXT), WINDOW, version=V15)
    bad = resolve()
    bad["binding_repairs"] = [dict(fact_index=0, field="cohort", action="dropped", reason="x")]
    with pytest.raises(ValidationError):
        ed.canonical_proposal(bad, row(TEXT), WINDOW, version=V15)


def test_v14_still_rejects_the_same_surface_errors():
    value = raw(context=context(actor=bind("P1", "bộ y tế")))
    with pytest.raises(ValueError, match="Context binding"):
        ed.resolve_reference_ids(value, row(TEXT), version="gold-editorial-14")


# Review: a dropped facet is null, so the reviewer must judge whether the claim needs it.


def test_dropped_needed_facet_withholds_release_through_review():
    p = canonical(raw(context=context(population=bind("P1", "người cao tuổi"))))
    downgraded = validate_review(review(), p, IDS, WINDOW)
    assert downgraded["context_assessments"][0]["population"] == "missing"
    assert not downgraded["newsletter_eligible"]
    held = validate_review(review(context_assessments=[assessment(population="missing")]), p, IDS, WINDOW)
    assert not held["newsletter_eligible"]
    assert "context_support_incomplete" in review_gate(p, held, IDS, WINDOW)["reasons"]


def test_a_dropped_facet_on_one_fact_does_not_block_a_complete_sibling():
    value = raw()
    second = copy.deepcopy(value["facts"][0])
    second.update(
        statement="Danh mục gồm nguy cơ ngã và loãng xương.",
        passage_id="P3",
        supporting_passage_ids=[],
        context=dict(actor=bind("P3", "Bộ Y tế"), document=None, population=None, status=None),
        date_link=None,
    )
    value["facts"].append(second)
    p = canonical(value)
    assert repairs(p) == [("actor", "dropped", "not_found_in_attached_support")]
    r = review(
        context_assessments=[
            assessment(),
            assessment(1, actor="missing", document="missing", population="missing", status="missing"),
        ]
    )
    assert validate_review(r, p, IDS, WINDOW)["newsletter_eligible"]


# Date links: drop a link whose rationale names another day; flag range endpoints.


def ceremony_fact(link):
    return raw(
        statement="Bệnh viện Lão khoa ra mắt Đề án chăm sóc người cao tuổi tại nhà.",
        passage_id="P2",
        supporting_passage_ids=["P1"],
        context=dict(
            actor=bind("P1", "Bệnh viện Lão khoa"),
            document=bind("P2", "Đề án chăm sóc người cao tuổi tại nhà"),
            population=None,
            status=bind("P2", "ra mắt"),
        ),
        date_link=link,
        reported_status="announced",
    )


def test_rationale_with_an_inferred_year_or_other_day_drops_the_date_link():
    link = dict(kind="cross_passage", candidate_id="D1", rationale="Lễ diễn ra ngày 24/10/2026 tại bệnh viện.")
    p = canonical(ceremony_fact(link), CEREMONY)
    fact = p["resolved_facts"][0]
    assert fact["event_date"] is None and fact["date_evidence"] is None
    assert repairs(p) == [("date_link", "dropped", "rationale_names_other_date")]
    assert p["relevance"] == "relevant"


def test_range_endpoint_date_is_flagged_for_review_only_in_v15():
    link = dict(kind="cross_passage", candidate_id="D1", rationale="Cùng buổi lễ kỷ niệm của bệnh viện.")
    p = canonical(ceremony_fact(link), CEREMONY)
    assert p["resolved_facts"][0]["event_date"] == "2026-10-27"
    assert fact_date_checks(p, WINDOW)[0]["date_is_range_endpoint"] is True
    prompt, _ = ed.messages("review", row(CEREMONY), WINDOW, p, version=V15)
    content = json.loads(prompt[1]["content"])
    assert content["code_date_checks"][0]["date_is_range_endpoint"] is True
    assert content["first_proposal"]["binding_repairs"] == []
    v14 = dict(p, context_contract="fact-context-1")
    assert "date_is_range_endpoint" not in fact_date_checks(v14, WINDOW)[0]
    assert fact_date_checks(canonical(), WINDOW)[0]["date_is_range_endpoint"] is False


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        (dict(kind="same_passage", candidate_id="D1"), ("date_link", "dropped", "cross_passage_without_rationale")),
        (dict(kind="cross_passage", candidate_id="D9", rationale="x"), ("date_link", "dropped", "unknown_candidate")),
    ],
)
def test_structurally_wrong_date_link_drops_only_the_link(link, expected):
    p = canonical(raw(date_link=link))
    assert p["resolved_facts"][0]["event_date"] is None
    assert repairs(p) == [expected]


def test_cross_passage_kind_for_a_primary_passage_date_is_retyped():
    value = raw(
        passage_id="P1",
        supporting_passage_ids=["P2", "P3"],
        date_link=dict(kind="cross_passage", candidate_id="D1", rationale="Cùng cuộc họp."),
    )
    p = canonical(value)
    fact = p["resolved_facts"][0]
    assert fact["event_date"] == "2026-10-10" and fact["date_evidence"] is None
    assert repairs(p) == [("date_link", "retyped", "candidate_in_primary_passage")]


def test_legacy_date_fields_are_still_schema_errors():
    with pytest.raises(ValidationError):
        canonical(raw(event_date_candidate_id="D1"))


@pytest.mark.parametrize(
    ("rationale", "conflict"),
    [
        ("Lễ ngày 24/10 thuộc dịp kỷ niệm.", True),
        ("Lễ diễn ra ngày 24/10/2026.", True),
        ("Cuộc họp ngày 27/10/2025.", True),
        ("Ngày 24 tháng 10 năm 2026 tổ chức lễ.", True),
        ("Cuộc họp ngày 27/10/2026 về chính đề án.", False),
        ("Ngày 27 tháng 10, cùng sự kiện.", False),
        ("Cùng sự kiện, 3.000 người tham dự.", False),
        ("Hội nghị ngày 27/10/2026, tỷ lệ tăng 5.6%.", False),
        ("Hội nghị ngày 27/10/2026, tỷ lệ tăng 5.6 %.", False),
        ("Cùng sự kiện tháng 10/2026.", False),
        (None, False),
    ],
)
def test_rationale_date_conflict(rationale, conflict):
    assert rationale_date_conflict(rationale, "2026-10-27") is conflict


@pytest.mark.parametrize(
    ("text", "endpoint"),
    [
        ("kỷ niệm 50 năm (27/10/1976 – 27/10/2026) của bệnh viện", True),
        ("giai đoạn từ 01/10/2026 đến 27/10/2026", True),
        ("từ 27/10/2026 - 30/10/2026", True),
        ("Ngày 27/10/2026, Bộ Y tế họp.", False),
        ("Năm 2026 - 27/10/2026 là hạn chót", True),
    ],
)
def test_date_is_range_endpoint(text, endpoint):
    assert date_is_range_endpoint(text, "2026-10-27") is endpoint


# Prompts and native runtime.


def test_v15_prompts_default_to_null_and_name_repairs():
    c, schema = ed.messages("classify", row(TEXT), WINDOW, version=V15)
    system = c[0]["content"]
    assert schema is ed.ContextReferenceProposal
    assert "Default each facet to null" in system and "Use null ONLY if" not in system
    assert "same capitalisation" in system and "anniversary period" in system
    v14, _ = ed.messages("classify", row(TEXT), WINDOW, version="gold-editorial-14")
    assert "Use null ONLY if" in v14[0]["content"] and "Default each facet" not in v14[0]["content"]
    r, schema = ed.messages("review", row(TEXT), WINDOW, canonical(), version=V15)
    assert schema is ed.ContextReview
    assert "binding_repairs" in r[0]["content"] and "date_is_range_endpoint" in r[0]["content"]
    s15, _ = ed.messages("scope", row(TEXT), WINDOW, version=V15)
    s14, _ = ed.messages("scope", row(TEXT), WINDOW, version="gold-editorial-14")
    assert s15 == s14
    assert ed.VERSION == V15 and "gold-editorial-14" in ed.READ_VERSIONS


def test_native_v15_run_repairs_instead_of_failing_and_packet_keeps_the_record(tmp_path, monkeypatch):
    value = dict(
        raw(),
        relevance_passage_id="P2",
        facts=[
            dict(
                statement=(
                    "Chương trình thí điểm phục hồi chức năng bắt đầu ngày 10/10/2026, "
                    "hỗ trợ nhóm trên 65 tuổi tự sinh hoạt tại nhà."
                ),
                passage_id="P1",
                supporting_passage_ids=["P2"],
                context=dict(
                    actor=bind("P2", "Bộ Y tế"),
                    document=bind("P1", "Chương trình thí điểm phục hồi chức năng"),
                    population=bind("P2", "Nhóm trên 65 tuổi"),
                    status=bind("P1", "thí điểm"),
                ),
                date_link=dict(kind="same_passage", candidate_id="D1"),
                reported_status="pilot",
            )
        ],
    )
    r = review(
        topic_passage_ids=["P2"],
        supported_date_fact_indexes=[],
        context_assessments=[assessment(actor="not_applicable")],
    )
    store, run, provider = setup_run(tmp_path, monkeypatch, [value, r, scope_assessment()])
    assert run["version"] == V15
    ed.run_calls(store, run["run_id"], provider, workers=1)
    ed.plan_review(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="review", workers=1)
    ed.plan_scope(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="scope", workers=1)
    packet = ed.evidence_packet(store, run["run_id"])
    article = packet["articles"][0]
    assert article["review_gate"]["eligible"]
    assert repairs(article["proposal"]) == [
        ("actor", "dropped", "not_found_in_attached_support"),
        ("population", "normalized", "case_or_whitespace"),
    ]
    assert packet["draft_evidence"][0]["context"]["population"]["quote"] == "nhóm trên 65 tuổi"


def test_decomposed_source_text_still_matches_a_composed_quote():
    import unicodedata

    from healthlab.editorial_context import _folded_matches

    text = unicodedata.normalize("NFD", "Chăm sóc người cao tuổi tại nhà.")
    quote = unicodedata.normalize("NFC", "người cao tuổi")
    [(start, end)] = _folded_matches(quote, {"quote": text})
    assert unicodedata.normalize("NFC", text[start:end]) == quote


def test_whitespace_only_cross_passage_rationale_is_a_schema_error():
    from pydantic import ValidationError

    from healthlab.editorial.contracts import CrossPassageDate

    with pytest.raises(ValidationError):
        CrossPassageDate(kind="cross_passage", candidate_id="D1", rationale="  ")
    assert CrossPassageDate(kind="cross_passage", candidate_id="D1", rationale="Cùng hội nghị").rationale
