"""Complete attached support, independent citation review and unchanged date ownership.

Direct contract cases pin gold-editorial-13 / editorial-review-5; the native-run
cases below exercise the current runtime through the shared v14 fixture.
"""

import copy
import json

import pytest
from pydantic import ValidationError

from healthlab import editorial as ed
from healthlab.editorial_review_policy import review_gate, validate_review
from tests.test_editorial import BODY, WINDOW, row, scope_assessment, setup_run
from tests.test_editorial_passages import reference_payload

pytestmark = pytest.mark.usefixtures("review_runtime")


V13 = "gold-editorial-13"

TEXT = (
    "Ngày 10/10/2026, Bộ Y tế họp về dự thảo sàng lọc cho người từ 60 tuổi. "
    "Dự thảo đề xuất sàng lọc sa sút trí tuệ. Danh mục còn gồm nguy cơ ngã và loãng xương."
)


def raw(article=None, **fact_updates):
    article = article or row(TEXT)
    value = reference_payload(article)
    value["facts"][0].update(
        statement="Tại cuộc họp ngày 10/10/2026, dự thảo đề xuất sàng lọc sa sút trí tuệ, nguy cơ ngã và loãng xương cho nhóm từ 60 tuổi.",
        supporting_passage_ids=["P2", "P3"],
        reported_status="proposed",
    )
    value["facts"][0].update(fact_updates)
    return value


def canonical(value=None, article=None):
    article = article or row(TEXT)
    return ed.canonical_proposal(
        ed.resolve_reference_ids(value or raw(article), article, version=V13),
        article,
        WINDOW,
        version=V13,
    )


def review(**updates):
    return (
        dict(
            review_version="editorial-review-5",
            relevance="relevant",
            topic_passage_ids=["P1"],
            supported_fact_indexes=[0],
            supported_date_fact_indexes=[],
            supported_citation_fact_indexes=[0],
            blocking_findings=[],
            audit_notes=[],
            rationale="Exact attached passages contain the full attributed draft statement.",
        )
        | updates
    )


def test_all_support_passages_have_exact_identity_and_survive_canonicalization():
    p = canonical()
    f = p["resolved_facts"][0]
    body = row(TEXT)["body"]
    assert [s["passage_id"] for s in f["supporting_passages"]] == ["P1", "P2", "P3"]
    for s in f["supporting_passages"]:
        assert body[s["start"] : s["end"]] == s["quote"]
    assert f["quote"] == f["supporting_passages"][0]["quote"] and f["start"] == 0
    assert p["newsletter_candidate"] and f["reported_status"] == "proposed"


@pytest.mark.parametrize("ids", [["P1"], ["P2", "P2"], ["P999"], ["P2", "P3", "P4", "P5"]])
def test_unknown_repeated_or_unbounded_support_is_rejected(ids):
    with pytest.raises(ValueError):
        canonical(raw(supporting_passage_ids=ids))


def test_new_raw_schema_requires_support_field_even_if_it_is_empty():
    value = raw()
    value["facts"][0].pop("supporting_passage_ids")
    with pytest.raises(ValidationError):
        ed.resolve_reference_ids(value, row(TEXT), version=V13)
    old = ed.resolve_reference_ids(value, row(TEXT), version="gold-editorial-12")
    assert "supporting_passages" not in old["facts"][0]


@pytest.mark.parametrize("change", ["offset", "quote", "unknown", "repeat", "primary"])
def test_tampering_resolved_support_cannot_feed_a_packet(change):
    value = ed.resolve_reference_ids(raw(), row(TEXT), version=V13)
    spans = value["facts"][0]["supporting_passages"]
    if change == "offset":
        spans[1]["start"] += 1
    if change == "quote":
        spans[1]["quote"] = "Invented content that is not in this source."
    if change == "unknown":
        spans[1]["passage_id"] = "P999"
    if change == "repeat":
        spans[1] = copy.deepcopy(spans[0])
    if change == "primary":
        spans[0], spans[1] = spans[1], spans[0]
    with pytest.raises(ValueError):
        ed.canonical_proposal(value, row(TEXT), WINDOW, version=V13)


def test_repeated_literal_text_uses_selected_id_offset_not_first_string_occurrence():
    text = "Người cao tuổi được tư vấn. Người cao tuổi được tư vấn."
    article = row(text)
    value = reference_payload(article, passage_id="P2", date_id=None)
    value["facts"][0]["supporting_passage_ids"] = []
    value["facts"][0]["statement"] = "Nguồn thuật người cao tuổi được tư vấn."
    p = canonical(value, article)
    assert p["resolved_facts"][0]["start"] == len("Người cao tuổi được tư vấn. ")


def test_additional_support_does_not_remove_cross_passage_date_rationale_or_review():
    value = raw(passage_id="P2", supporting_passage_ids=["P1", "P3"])
    with pytest.raises(ValueError, match="Cross-passage date"):
        canonical(value)
    value["facts"][0]["date_link_rationale"] = "Cùng cuộc họp và dự thảo được nêu ở P1."
    p = canonical(value)
    assert not validate_review(review(), p, ["P1", "P2", "P3"], WINDOW)["newsletter_eligible"]
    approved = validate_review(
        review(supported_date_fact_indexes=[0]), p, ["P1", "P2", "P3"], WINDOW
    )
    assert approved["newsletter_eligible"]


def test_full_source_support_cannot_substitute_for_missing_attached_details():
    p = canonical(raw(supporting_passage_ids=[]))
    r = review(
        supported_citation_fact_indexes=[],
        blocking_findings=[
            dict(
                code="citation_incomplete",
                explanation="P2/P3 details are not attached.",
                fact_indexes=[0],
                passage_ids=["P2", "P3"],
            )
        ],
    )
    result = validate_review(r, p, ["P1", "P2", "P3"], WINDOW)
    assert not result["newsletter_eligible"]
    gate = review_gate(p, result, ["P1", "P2", "P3"], WINDOW)
    assert "citation_support_incomplete" in gate["reasons"]
    assert not validate_review(
        review(supported_citation_fact_indexes=[]), p, ["P1", "P2", "P3"], WINDOW
    )["newsletter_eligible"]
    assert validate_review(review(), canonical(), ["P1", "P2", "P3"], WINDOW)["newsletter_eligible"]


@pytest.mark.parametrize("indexes", [[True], [9], [0, 0]])
def test_invalid_citation_attestation_indexes_are_rejected(indexes):
    with pytest.raises(ValueError):
        validate_review(
            review(supported_citation_fact_indexes=indexes), canonical(), ["P1", "P2", "P3"], WINDOW
        )


def test_conflicting_citation_finding_or_empty_fact_reference_is_rejected():
    f = dict(
        code="citation_incomplete",
        explanation="Detail missing",
        fact_indexes=[0],
        passage_ids=["P2"],
    )
    with pytest.raises(ValueError, match="contradicts"):
        validate_review(review(blocking_findings=[f]), canonical(), ["P1", "P2", "P3"], WINDOW)
    f["fact_indexes"] = []
    with pytest.raises(ValueError, match="concrete"):
        validate_review(
            review(supported_citation_fact_indexes=[], blocking_findings=[f]),
            canonical(),
            ["P1", "P2", "P3"],
            WINDOW,
        )


def test_legacy_review_cannot_release_a_new_multi_passage_proposal():
    old = {k: v for k, v in review().items() if k != "supported_citation_fact_indexes"}
    old["review_version"] = "editorial-review-4"
    assert review_gate(canonical(), old, ["P1", "P2", "P3"], WINDOW)["reasons"] == [
        "citation_review_required"
    ]


def test_native_current_review_scope_and_writer_packet_keep_all_support(tmp_path, monkeypatch):
    value = raw(row(BODY), supporting_passage_ids=["P2"])
    value["facts"][0]["statement"] = (
        "Theo nguồn, thí điểm bắt đầu ngày 10/10/2026 và hỗ trợ nhóm trên 65 tuổi tự sinh hoạt tại nhà."
    )
    store, run, provider = setup_run(
        tmp_path, monkeypatch, [value, review(topic_passage_ids=["P2"]), scope_assessment()]
    )
    ed.run_calls(store, run["run_id"], provider, workers=1)
    ed.plan_review(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="review", workers=1)
    ed.plan_scope(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="scope", workers=1)
    packet = ed.evidence_packet(store, run["run_id"])
    assert len(packet["draft_evidence"]) == 1
    spans = packet["draft_evidence"][0]["supporting_passages"]
    assert len(spans) == 2
    prompt = ed.article_messages(run["question"], packet["draft_evidence"])
    assert json.loads(prompt[1]["content"])["evidence"][0]["supporting_passages"] == spans


def test_same_schema_archive_keeps_citations_and_exports_to_inspections(tmp_path, monkeypatch):
    value = raw(row(BODY), supporting_passage_ids=["P2"])
    store, run, provider = setup_run(tmp_path, monkeypatch, [value])
    run_id = run["run_id"]
    ed.run_calls(store, run_id, provider, workers=1)

    saved = store.load(run_id)
    saved["runtime_sha256"] = "0" * 64  # Same schema, earlier implementation.
    store.save(saved)
    directory = store.root / "runs" / run_id
    before = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
    packet = ed.evidence_packet(store, run_id)
    spans = packet["articles"][0]["proposal"]["resolved_facts"][0]["supporting_passages"]
    assert [span["passage_id"] for span in spans] == ["P1", "P2"]

    with pytest.raises(ValueError, match="read-only"):
        ed.run_calls(store, run_id, provider, workers=1)
    ed.export_report(store, run_id)

    inspection = store.root / "inspections" / run_id
    assert (inspection / "knowledge.json").read_bytes() == before["knowledge.json"]
    assert (inspection / "report.html").read_bytes() == before["report.html"]
    after = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
    assert after == before


def test_current_prompts_distinguish_maturity_relevance_and_citation_coverage():
    c, schema = ed.messages("classify", row(TEXT), WINDOW, version=V13)
    assert schema is ed.SupportedReferenceProposal
    assert "SEPARATELY from implementation maturity" in c[0]["content"]
    r, schema = ed.messages("review", row(TEXT), WINDOW, canonical(), version=V13)
    assert schema is ed.CitationReview and "Do NOT silently borrow" in r[0]["content"]
    assert "supported_citation_fact_indexes" in schema.model_json_schema()["required"]
    assert (
        ed.passage_policy("gold-editorial-11")
        == ed.passage_policy("gold-editorial-12")
        == "editorial-passages-2"
    )
