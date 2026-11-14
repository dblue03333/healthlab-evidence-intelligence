"""gold-editorial-14 context bindings, typed date links and editorial-review-6.

Contract tests pin v14 (fact-context-1, reject on any invalid binding). The native
pipeline tests run the current runtime; v15 repair behaviour is in test_editorial_context_v15.

Literal bindings establish identity with attached support only. They never prove
entailment; release still depends on an explicit per-fact reviewer assessment.
"""

import copy
import json

import pytest
from pydantic import ValidationError

from healthlab import editorial as ed
from healthlab.editorial_review_policy import review_gate, validate_review
from tests.conftest import REVIEW_RUNTIME
from tests.test_editorial import WINDOW, row, scope_assessment, setup_run

pytestmark = pytest.mark.usefixtures("review_runtime")


TEXT = (
    "Ngày 10/10/2026, Bộ Y tế họp về dự thảo sàng lọc cho người từ 60 tuổi. "
    "Dự thảo đề xuất sàng lọc sa sút trí tuệ. Danh mục còn gồm nguy cơ ngã và loãng xương."
)
IDS = ["P1", "P2", "P3"]
NULL_CONTEXT = dict(actor=None, document=None, population=None, status=None)
V14 = "gold-editorial-14"


def bind(passage_id, quote):
    return dict(passage_id=passage_id, quote=quote)


def context(**updates):
    return (
        dict(
            actor=bind("P1", "Bộ Y tế"),
            document=bind("P1", "dự thảo sàng lọc"),
            population=bind("P1", "người từ 60 tuổi"),
            status=bind("P2", "đề xuất"),
        )
        | updates
    )


def raw(**fact_updates):
    fact = (
        dict(
            statement=(
                "Dự thảo sàng lọc của Bộ Y tế, được họp ngày 10/10/2026, đề xuất sàng lọc "
                "sa sút trí tuệ, nguy cơ ngã và loãng xương cho người từ 60 tuổi."
            ),
            passage_id="P2",
            supporting_passage_ids=["P1", "P3"],
            context=context(),
            date_link=dict(
                kind="cross_passage",
                candidate_id="D1",
                rationale="P1 nêu cuộc họp ngày 10/10/2026 về chính dự thảo được mô tả ở P2.",
            ),
            reported_status="proposed",
        )
        | fact_updates
    )
    return dict(
        relevance="relevant",
        topics=["aging_society"],
        jurisdiction="vietnam",
        content_kind="new_development",
        source_role="original",
        summary="Bộ Y tế họp về dự thảo sàng lọc cho người cao tuổi.",
        rationale="Dự thảo sàng lọc dành cho người từ 60 tuổi.",
        relevance_passage_id="P1",
        facts=[fact],
        uncertainties=["Dự thảo chưa được ban hành."],
    )


def canonical(value=None):
    article = row(TEXT)
    return ed.canonical_proposal(
        ed.resolve_reference_ids(value or raw(), article, version=V14), article, WINDOW, version=V14
    )


def assessment(index=0, **updates):
    return (
        dict(
            fact_index=index,
            actor="supported",
            document="supported",
            population="supported",
            status="supported",
            explanation="Bộ Y tế, dự thảo, nhóm từ 60 tuổi và trạng thái đề xuất đều nằm trong đoạn đính kèm.",
        )
        | updates
    )


def review(**updates):
    return (
        dict(
            review_version="editorial-review-6",
            relevance="relevant",
            topic_passage_ids=["P1"],
            supported_fact_indexes=[0],
            supported_date_fact_indexes=[0],
            supported_citation_fact_indexes=[0],
            blocking_findings=[],
            audit_notes=[],
            rationale="Đoạn đính kèm hỗ trợ dự thảo, nhóm đối tượng và ngày họp.",
            context_assessments=[assessment()],
        )
        | updates
    )


# AC02: literal, attached, unique context bindings.


def test_context_bindings_resolve_to_attached_offsets_and_survive_canonicalization():
    p = canonical()
    assert p["context_contract"] == "fact-context-1"
    f = p["resolved_facts"][0]
    body = row(TEXT)["body"]
    for facet in ("actor", "document", "population", "status"):
        binding = f["context"][facet]
        assert body[binding["start"] : binding["end"]] == binding["quote"]
    assert f["context"]["status"]["passage_id"] == "P2"
    assert [s["passage_id"] for s in f["supporting_passages"]] == ["P2", "P1", "P3"]
    assert f["event_date"] == "2026-10-10" and f["date_evidence"]["passage_id"] == "P1"
    assert p["newsletter_candidate"]


def test_null_context_is_explicit_not_omitted():
    value = raw()
    value["facts"][0].pop("context")
    with pytest.raises(ValidationError):
        canonical(value)
    value["facts"][0]["context"] = {"actor": None, "document": None, "population": None}
    with pytest.raises(ValidationError):
        canonical(value)
    value["facts"][0]["context"] = dict(NULL_CONTEXT, cohort=None)
    with pytest.raises(ValidationError):
        canonical(value)
    value["facts"][0]["context"] = dict(NULL_CONTEXT)
    assert canonical(value)["resolved_facts"][0]["context"] == NULL_CONTEXT


@pytest.mark.parametrize(
    "binding",
    [
        bind("P1", "người cao tuổi"),  # Not literal text.
        bind("P3", "Bộ Y tế"),  # Literal elsewhere, not in the named passage.
        bind("P4", "Bộ Y tế"),  # Unknown passage.
        bind("P1", "t"),  # Not unique within the passage.
    ],
)
def test_nonliteral_misplaced_or_ambiguous_binding_is_rejected(binding):
    with pytest.raises(ValueError, match="Context binding"):
        canonical(raw(context=context(actor=binding)))


def test_binding_to_an_unattached_passage_is_rejected_even_if_literal():
    value = raw(supporting_passage_ids=["P3"])
    value["facts"][0]["date_link"] = None
    with pytest.raises(ValueError, match="Context binding"):
        canonical(value)


@pytest.mark.parametrize("change", ["offset", "quote", "passage", "drop"])
def test_tampered_resolved_context_cannot_feed_a_packet(change):
    value = ed.resolve_reference_ids(raw(), row(TEXT), version=V14)
    binding = value["facts"][0]["context"]["population"]
    if change == "offset":
        binding["start"] += 1
        binding["end"] += 1
    if change == "quote":
        binding["quote"] = "người từ 45 đến 69 tuổi"
    if change == "passage":
        binding["passage_id"] = "P3"
    if change == "drop":
        value["facts"][0].pop("context")
    with pytest.raises(ValueError):
        ed.canonical_proposal(value, row(TEXT), WINDOW, version=V14)


# AC04: typed date links.


def test_same_passage_link_must_point_to_the_primary_passage():
    value = raw(date_link=dict(kind="same_passage", candidate_id="D1"))
    with pytest.raises(ValueError, match="Date link kind"):
        canonical(value)
    primary = raw(
        passage_id="P1",
        supporting_passage_ids=["P2", "P3"],
        date_link=dict(kind="same_passage", candidate_id="D1"),
    )
    f = canonical(primary)["resolved_facts"][0]
    assert f["event_date"] == "2026-10-10" and f["date_evidence"] is None


def test_cross_passage_link_needs_another_passage_and_a_rationale():
    same = raw(
        passage_id="P1",
        supporting_passage_ids=["P2", "P3"],
        date_link=dict(kind="cross_passage", candidate_id="D1", rationale="Cùng sự kiện."),
    )
    with pytest.raises(ValueError, match="Date link kind"):
        canonical(same)
    for link in (
        dict(kind="cross_passage", candidate_id="D1"),
        dict(kind="cross_passage", candidate_id="D1", rationale=""),
    ):
        with pytest.raises(ValidationError):
            canonical(raw(date_link=link))


def test_date_in_additional_support_is_still_cross_passage():
    # D1 sits in P1, which is attached as additional support for primary P2.
    with pytest.raises(ValueError, match="Date link kind"):
        canonical(raw(date_link=dict(kind="same_passage", candidate_id="D1")))


@pytest.mark.parametrize(
    "fact_updates",
    [
        dict(date_link=dict(kind="cross_passage", candidate_id="D9", rationale="x")),
        dict(date_link=dict(kind="other", candidate_id="D1")),
        dict(event_date_candidate_id="D1"),
        dict(date_link_rationale="legacy free-standing field"),
    ],
)
def test_unknown_candidate_or_legacy_date_fields_are_rejected(fact_updates):
    with pytest.raises(ValueError):
        canonical(raw(**fact_updates))


def test_undated_fact_stays_undated_and_cannot_release_alone():
    p = canonical(raw(date_link=None))
    assert p["resolved_facts"][0]["event_date"] is None
    assert not p["newsletter_candidate"]


def test_cross_passage_date_still_needs_independent_date_review():
    p = canonical()
    held = validate_review(review(supported_date_fact_indexes=[]), p, IDS, WINDOW)
    assert not held["newsletter_eligible"]
    assert validate_review(review(), p, IDS, WINDOW)["newsletter_eligible"]


# AC03: explicit reviewer assessment per fact and facet.


def test_missing_context_on_a_supported_fact_withholds_release():
    p = canonical()
    r = review(context_assessments=[assessment(population="missing")])
    result = validate_review(r, p, IDS, WINDOW)
    assert not result["newsletter_eligible"]
    assert "context_support_incomplete" in review_gate(p, result, IDS, WINDOW)["reasons"]


def test_missing_context_on_a_rejected_fact_does_not_block_other_facts():
    value = raw()
    second = copy.deepcopy(value["facts"][0])
    second.update(
        statement="Danh mục gồm nguy cơ ngã và loãng xương.",
        passage_id="P3",
        supporting_passage_ids=[],
        context=dict(NULL_CONTEXT),
        date_link=None,
    )
    value["facts"].append(second)
    p = canonical(value)
    r = review(
        context_assessments=[
            assessment(),
            assessment(
                1,
                actor="missing",
                document="missing",
                population="missing",
                status="missing",
                explanation="Câu danh mục đứng riêng không cho biết thuộc dự thảo nào.",
            ),
        ]
    )
    assert validate_review(r, p, IDS, WINDOW)["newsletter_eligible"]
    r["supported_fact_indexes"] = [0, 1]
    r["supported_citation_fact_indexes"] = [0, 1]
    assert not validate_review(r, p, IDS, WINDOW)["newsletter_eligible"]


@pytest.mark.parametrize(
    "assessments",
    [
        [],
        [assessment(), assessment()],
        [assessment(1)],
        [assessment(), assessment(1)],
    ],
)
def test_context_review_needs_exactly_one_assessment_per_fact(assessments):
    with pytest.raises(ValueError, match="one assessment per fact"):
        validate_review(review(context_assessments=assessments), canonical(), IDS, WINDOW)


def test_assessment_contradicting_the_binding_becomes_missing_not_a_failed_review():
    bound = validate_review(
        review(context_assessments=[assessment(actor="not_applicable")]), canonical(), IDS, WINDOW
    )
    assert bound["context_assessments"][0]["actor"] == "missing"
    assert not bound["newsletter_eligible"]
    p = canonical(raw(context=context(actor=None)))
    unbound = validate_review(review(), p, IDS, WINDOW)
    assert unbound["context_assessments"][0]["actor"] == "missing"
    assert not unbound["newsletter_eligible"]
    assert "context_support_incomplete" in review_gate(p, unbound, IDS, WINDOW)["reasons"]
    ok = review(context_assessments=[assessment(actor="not_applicable")])
    assert validate_review(ok, p, IDS, WINDOW)["newsletter_eligible"]
    flagged = review(context_assessments=[assessment(actor="missing")])
    assert not validate_review(flagged, p, IDS, WINDOW)["newsletter_eligible"]


def test_review_versions_cannot_cross_context_contracts():
    p = canonical()
    old = {k: v for k, v in review().items() if k != "context_assessments"}
    old["review_version"] = "editorial-review-5"
    with pytest.raises(ValueError, match="matching context review"):
        validate_review(old, p, IDS, WINDOW)
    assert review_gate(p, old, IDS, WINDOW)["reasons"] == ["review_contract_invalid"]
    v13 = ed.canonical_proposal(
        ed.resolve_reference_ids(
            dict(
                raw(),
                facts=[
                    dict(
                        statement=raw()["facts"][0]["statement"],
                        passage_id="P2",
                        supporting_passage_ids=["P1", "P3"],
                        event_date_candidate_id="D1",
                        date_link_rationale="Cùng cuộc họp về dự thảo.",
                        reported_status="proposed",
                    )
                ],
            ),
            row(TEXT),
            version="gold-editorial-13",
        ),
        row(TEXT),
        WINDOW,
        version="gold-editorial-13",
    )
    with pytest.raises(ValueError, match="matching context review"):
        validate_review(review(), v13, IDS, WINDOW)


def test_saved_eligibility_must_match_derived_context_gate():
    r = review(context_assessments=[assessment(status="missing")], newsletter_eligible=True)
    with pytest.raises(ValueError, match="eligibility"):
        validate_review(r, canonical(), IDS, WINDOW)


# AC05: prompts, versions and the native writer packet.


def test_v14_prompts_request_context_and_typed_date_links():
    c, schema = ed.messages("classify", row(TEXT), WINDOW, version=V14)
    assert schema is ed.ContextReferenceProposal
    system = c[0]["content"]
    assert '"kind":"cross_passage"' in system and "actor, document, population and status" in system
    fact_schema = schema.model_json_schema()["$defs"]["ContextIDFact"]
    assert {"context", "date_link"} <= set(fact_schema["required"])
    assert "event_date_candidate_id" not in fact_schema["properties"]
    r, schema = ed.messages("review", row(TEXT), WINDOW, canonical(), version=V14)
    assert schema is ed.ContextReview and "editorial-review-6" in r[0]["content"]
    assert "context_assessments" in schema.model_json_schema()["required"]
    assert json.loads(r[1]["content"])["first_proposal"]["resolved_facts"][0]["context"]


def test_v13_classify_prompt_is_unchanged_by_v14_section_replacement():
    v13, schema = ed.messages("classify", row(TEXT), WINDOW, version="gold-editorial-13")
    assert schema is ed.SupportedReferenceProposal
    assert "date_link_rationale rule relative to the primary passage" in v13[0]["content"]
    assert "context_assessments" not in v13[0]["content"]
    assert ed.passage_policy("gold-editorial-14") == ed.passage_policy("gold-editorial-13")
    assert {"gold-editorial-13", "gold-editorial-14"} <= ed.READ_VERSIONS


def test_native_run_carries_context_to_review_and_writer_packet(tmp_path, monkeypatch):
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
                    actor=None,
                    document=bind("P1", "Chương trình thí điểm phục hồi chức năng"),
                    population=bind("P2", "nhóm trên 65 tuổi"),
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
    assert run["version"] == REVIEW_RUNTIME
    ed.run_calls(store, run["run_id"], provider, workers=1)
    ed.plan_review(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="review", workers=1)
    ed.plan_scope(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="scope", workers=1)
    packet = ed.evidence_packet(store, run["run_id"])
    assert packet["articles"][0]["review_gate"]["eligible"]
    evidence = packet["draft_evidence"]
    assert len(evidence) == 1
    assert evidence[0]["context"]["population"]["quote"] == "nhóm trên 65 tuổi"
    prompt = ed.article_messages(run["question"], evidence)
    assert json.loads(prompt[1]["content"])["evidence"][0]["context"] == evidence[0]["context"]


def test_native_review_marking_context_missing_flags_fact_in_packet(tmp_path, monkeypatch):
    # gold-editorial-17 (fact-flags-1): missing context is shown to the approver, not a block.
    r = review(
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
    from tests.test_editorial import proposal

    store, run, provider = setup_run(tmp_path, monkeypatch, [proposal(), r, scope_assessment()])
    ed.run_calls(store, run["run_id"], provider, workers=1)
    ed.plan_review(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="review", workers=1)
    ed.plan_scope(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="scope", workers=1)
    packet = ed.evidence_packet(store, run["run_id"])
    gate = packet["articles"][0]["review_gate"]
    assert gate["eligible"] and not gate["review_clean"]
    [evidence] = packet["draft_evidence"]
    assert evidence["review_flags"] == ["context_missing:population"]
