"""Gold-bound full-text AI proposals, failure accounting and cited evidence."""

import json

import pytest

from healthlab import editorial as ed
from healthlab.editorial import runs as editorial_runs
from healthlab.models import IngestionError
from healthlab.provider import Completion, FakeProvider
from healthlab.store import RunStore
from tests.editorial_legacy import canonical_v12
from tests.test_data_pilot import execute, transport

pytestmark = pytest.mark.usefixtures("review_runtime")


WINDOW = {"mindate": "2026-10-03", "maxdate": "2026-10-16"}
QUOTE = "Chương trình thí điểm phục hồi chức năng bắt đầu ngày 10/10/2026 tại Việt Nam."
BODY = QUOTE + " Hỗ trợ nhóm trên 65 tuổi tự sinh hoạt tại nhà."


def row(body=BODY):
    return {
        "document_id": "id",
        "source_id": "source",
        "url": "https://example.org/a",
        "title": "Phục hồi chức năng tại cộng đồng",
        "body": body,
        "publication_date": "2026-10-11",
        "selection_date": "2026-10-11",
        "selection_date_basis": "publication",
        "snapshot_id": "a" * 64,
        "raw_sha256": "b" * 64,
    }


def proposal(**updates):
    return {
        "relevance": "relevant",
        "topics": ["longevity"],
        "jurisdiction": "vietnam",
        "content_kind": "new_development",
        "source_role": "original",
        "summary": "Nguồn mô tả một mô hình thí điểm.",
        "rationale": "Hỗ trợ chức năng cho nhóm trên 65 tuổi.",
        "relevance_quote": BODY,
        "facts": [
            {
                "statement": "Nguồn báo cáo chương trình thí điểm bắt đầu ngày 10/10/2026.",
                "quote": QUOTE,
                "event_date": "2026-10-10",
                "reported_status": "pilot",
            }
        ],
        "uncertainties": ["Chưa có đánh giá hiệu quả."],
    } | updates


def scope_assessment(**updates):
    return {
        "scope_version": "healthy-ageing-scope-1",
        "verdict": "substantive",
        "passage_ids": ["P2"],
        "ageing_context": "Hỗ trợ nhóm trên65 tuổi tự sinh hoạt.",
        "substantive_detail": "Phục hồi chức năng gắn khả năng sinh hoạt.",
        "requires_outside_knowledge": False,
        "rationale": "Liên hệ chức năng có trong nguồn.",
    } | updates


def setup_run(tmp_path, monkeypatch, responses):
    from healthlab.data.contracts import Gold, Window

    article = row()
    monkeypatch.setattr(editorial_runs, "supervised_records", lambda *args: [article])
    gold = Gold(
        silver_sha256="a" * 64,
        window=Window(**WINDOW),
        status="ready",
        document_ids=[article["document_id"]],
    )
    monkeypatch.setattr(editorial_runs, "read_dataset", lambda *args: (None, None, None, gold))

    _, candidates = ed.passage_inputs(article)
    date_passages = {d["candidate_id"]: d["passage_id"] for d in candidates}
    fact_counts = []

    def v14_fact(f):
        """Native fixtures emit the current typed date link and explicit context keys."""
        if "context" in f or "date_link" in f:
            return f
        date_id = f.get("event_date_candidate_id")
        same = date_passages.get(date_id) == f["passage_id"]
        link = None
        if date_id is not None:
            link = (
                {"kind": "same_passage", "candidate_id": date_id}
                if same
                else {
                    "kind": "cross_passage",
                    "candidate_id": date_id,
                    "rationale": f.get("date_link_rationale"),
                }
            )
        return {
            "statement": f["statement"],
            "passage_id": f["passage_id"],
            "supporting_passage_ids": f.get("supporting_passage_ids", []),
            "context": {"actor": None, "document": None, "population": None, "status": None},
            "date_link": link,
            "reported_status": f["reported_status"],
        }

    def to_ids(p):
        if isinstance(p, dict) and "review_version" in p:
            count = fact_counts[-1] if fact_counts else 1
            return dict(
                {k: v for k, v in p.items() if k != "newsletter_eligible"},
                review_version="editorial-review-6",
                supported_citation_fact_indexes=p.get(
                    "supported_citation_fact_indexes", p["supported_fact_indexes"]
                ),
                supported_date_fact_indexes=p.get("supported_date_fact_indexes", []),
                context_assessments=p.get(
                    "context_assessments",
                    [
                        {
                            "fact_index": i,
                            "actor": "not_applicable",
                            "document": "not_applicable",
                            "population": "not_applicable",
                            "status": "not_applicable",
                            "explanation": "Fixture without context bindings.",
                        }
                        for i in range(count)
                    ],
                ),
            )
        if isinstance(p, dict) and "relevance_passage_id" in p:
            fact_counts.append(len(p["facts"]))
            return dict(p, facts=[v14_fact(f) for f in p["facts"]])
        if not isinstance(p, dict) or "relevance_quote" not in p:
            return p
        fact_counts.append(len(p["facts"]))
        result = {k: v for k, v in p.items() if k not in {"relevance_quote", "facts"}}
        result["relevance_passage_id"] = "P2"
        result["facts"] = [
            v14_fact(
                {
                    "statement": f["statement"],
                    "passage_id": "P1",
                    "event_date_candidate_id": "D1" if f["event_date"] else None,
                    "reported_status": f["reported_status"],
                }
            )
            for f in p["facts"]
        ]
        return result

    provider = FakeProvider(
        [
            Completion(
                json.dumps(to_ids(p), ensure_ascii=False), "fake-test", "stop", {"total_tokens": 5}
            )
            if isinstance(p, dict)
            else p
            for p in responses
        ]
    )
    store = RunStore(tmp_path / "editorial")
    run = ed.prepare(RunStore(tmp_path / "source"), store, "d" * 64, provider, call_budget=5)
    return store, run, provider


def test_implicit_topic_no_keyword_and_background_are_retained():
    article = row()
    assert "longevity" not in article["body"] and "aging" not in article["body"]
    result = canonical_v12(proposal(), article, WINDOW)
    assert result["newsletter_candidate"]
    background = canonical_v12(proposal(content_kind="background"), article, WINDOW)
    assert background["relevance"] == "relevant" and not background["newsletter_candidate"]
    assert background["resolved_facts"][0]["quote"] == QUOTE


def test_dates_and_quotes_never_silently_inferred():
    p = proposal()
    p["facts"][0]["event_date"] = "2026-10-11"
    r = canonical_v12(p, row(), WINDOW)
    assert r["technical_holds"] and not r["newsletter_candidate"]
    p = proposal(relevance_quote="invented supporting sentence")
    with pytest.raises(ValueError, match="not literal"):
        canonical_v12(p, row(), WINDOW)


def test_full_body_is_sent_and_success_is_reused_without_more_calls(tmp_path, monkeypatch):
    store, run, provider = setup_run(tmp_path, monkeypatch, [proposal()])
    result = ed.run_calls(store, run["run_id"], provider, workers=1)
    assert result["classified"] == 1 and result["relevance"] == {"relevant": 1}
    job = ed.jobs(store, store.load(run["run_id"]))[0]
    prompt = json.loads(store.read(job["prompt_sha256"]))
    content = json.loads(prompt[1]["content"])["article"]
    assert "body" not in content
    assert "".join("".join(p["quote"].split()) for p in content["passages"]) == "".join(
        BODY.split()
    )
    assert ed.run_calls(store, run["run_id"], provider, workers=1)["classified"] == 1
    assert provider.calls == 1


def test_failure_is_not_an_exclusion_or_automatic_retry(tmp_path, monkeypatch):
    store, run, provider = setup_run(
        tmp_path, monkeypatch, [IngestionError("provider_timeout", "timeout")]
    )
    result = ed.run_calls(store, run["run_id"], provider, workers=1)
    assert result["classified"] == 0 and result["errors"][0]["code"] == "provider_timeout"
    assert not result["relevance"]
    ed.run_calls(store, run["run_id"], provider, workers=1)
    assert provider.calls == 1


def test_review_disagreement_blocks_drafting_and_does_not_erase_knowledge(tmp_path, monkeypatch):
    review = {
        "relevance": "uncertain",
        "newsletter_eligible": False,
        "supported_fact_indexes": [],
        "review_version": "editorial-review-2",
        "topic_passage_ids": [],
        "blocking_findings": [
            {
                "code": "topic_not_supported",
                "explanation": "Phạm vi chưa rõ",
                "fact_indexes": [],
                "passage_ids": ["P2"],
            }
        ],
        "audit_notes": [],
        "rationale": "Cần kiểm tra.",
    }
    store, run, provider = setup_run(tmp_path, monkeypatch, [proposal(), review])
    ed.run_calls(store, run["run_id"], provider, workers=1)
    ed.plan_review(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="review", workers=1)
    packet = ed.evidence_packet(store, run["run_id"])
    assert packet["articles"][0]["proposal"]["relevance"] == "relevant"
    assert not packet["draft_evidence"]
    ed.plan_draft(store, run["run_id"])
    assert not store.load(run["run_id"])["draft_planned"]


def test_reviewed_fact_can_feed_unapproved_cited_draft(tmp_path, monkeypatch):
    review = {
        "relevance": "relevant",
        "newsletter_eligible": True,
        "supported_fact_indexes": [0],
        "review_version": "editorial-review-2",
        "topic_passage_ids": ["P2"],
        "blocking_findings": [],
        "audit_notes": ["Fact được nguồn hỗ trợ; ghi chú này không phải lỗi."],
        "rationale": "Đoạn trích hỗ trợ đúng mốc và trạng thái thí điểm.",
    }
    draft = {
        "title": "Thử nghiệm hỗ trợ phục hồi chức năng",
        "sections": [
            {
                "title": "Một chương trình thí điểm",
                "facts": [
                    {
                        "text": "Nguồn báo cáo chương trình thí điểm bắt đầu ngày 10/10/2026.",
                        "evidence_ids": ["E1"],
                    }
                ],
                "interpretation": "Có thể theo dõi thêm.",
                "uncertainty": "Chưa có dữ liệu hiệu quả.",
            }
        ],
    }
    store, run, provider = setup_run(
        tmp_path, monkeypatch, [proposal(), review, scope_assessment(), draft]
    )
    ed.run_calls(store, run["run_id"], provider, workers=1)
    ed.plan_review(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="review", workers=1)
    ed.plan_scope(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="scope", workers=1)
    ed.plan_draft(store, run["run_id"])
    result = ed.run_calls(store, run["run_id"], provider, role="draft", workers=1)
    assert result["draft_evidence_facts"] == 1 and result["calls_dispatched"] == 4
    assert not result["human_approval"]
    assert (store.root / "runs" / run["run_id"] / "draft.md").is_file()


def test_oversized_body_fails_before_any_call(tmp_path, monkeypatch):
    store, run, provider = setup_run(tmp_path, monkeypatch, [])
    monkeypatch.setattr(editorial_runs, "supervised_records", lambda *args: [row("x" * 20_001)])
    with pytest.raises(ValueError, match="no silent truncation"):
        ed.prepare(RunStore(tmp_path / "source"), store, "d" * 64, provider)
    assert provider.calls == 0


def test_full_review_records_actual_scope_and_preserves_budget(tmp_path, monkeypatch):
    store, run, provider = setup_run(tmp_path, monkeypatch, [proposal(relevance="irrelevant")])
    ed.run_calls(store, run["run_id"], provider, workers=1)
    planned = ed.plan_review(store, run["run_id"], all_articles=True)
    assert planned["review_scope"]["selected_document_ids"] == ["id"]
    assert planned["review_scope"]["all_articles"]
    assert planned["review_scope"]["irrelevant_sample"] == 1
    assert len(ed.jobs(store, planned)) == 2
    assert len(ed.plan_review(store, run["run_id"], all_articles=True)["jobs"]) == 2


def test_full_review_refuses_to_silently_reduce_scope(tmp_path, monkeypatch):
    store, run, provider = setup_run(tmp_path, monkeypatch, [proposal()])
    ed.run_calls(store, run["run_id"], provider, workers=1)
    saved = store.load(run["run_id"])
    saved["call_budget"] = 2
    store.save(saved)
    with pytest.raises(ValueError, match="No review budget"):
        ed.plan_review(store, run["run_id"], all_articles=True)
    assert len(ed.jobs(store, store.load(run["run_id"]))) == 1


def test_narrative_writer_cannot_start_with_empty_evidence():
    with pytest.raises(ValueError, match="No reviewed evidence"):
        ed.article_messages(WINDOW, [])


def test_real_gold_readiness_is_required(tmp_path):
    source = RunStore(tmp_path / "source")
    client, _ = transport()
    with client:
        result = execute(source, client)
    provider = FakeProvider([])
    store = RunStore(tmp_path / "editorial")
    run = ed.prepare(source, store, result["dataset_id"], provider)
    assert len(run["jobs"]) == result["documents"] and not run["keyword_prefilter"]


def test_id_resolution_rejects_missing_passages_and_cross_passage_dates():
    payload = {
        "relevance": "relevant",
        "topics": ["longevity"],
        "jurisdiction": "vietnam",
        "content_kind": "new_development",
        "source_role": "original",
        "summary": "Summary",
        "rationale": "Rationale",
        "relevance_passage_id": "P2",
        "facts": [
            {
                "statement": "Statement",
                "passage_id": "P1",
                "event_date_candidate_id": "D1",
                "date_context_passage_id": None,
                "date_link_rationale": None,
                "reported_status": "pilot",
            }
        ],
        "uncertainties": [],
    }
    resolved = ed.resolve_ids(payload, row())
    assert resolved["facts"][0]["quote"] == QUOTE
    assert resolved["facts"][0]["event_date"] == "2026-10-10"
    payload["facts"][0]["passage_id"] = "P2"
    with pytest.raises(ValueError, match="Date candidate"):
        ed.resolve_ids(payload, row())
    payload["relevance_passage_id"] = "P999"
    with pytest.raises(ValueError, match="Unknown relevance"):
        ed.resolve_ids(payload, row())


def linked_proposal():
    article = row(
        "Ngày 10/10/2026, Bộ tổ chức cuộc họp tại Việt Nam. "
        "Tại cuộc họp, lãnh đạo yêu cầu đào tạo chuyên biệt chăm sóc người cao tuổi."
    )
    payload = {
        "relevance": "relevant",
        "topics": ["aging_society"],
        "jurisdiction": "vietnam",
        "content_kind": "new_development",
        "source_role": "original",
        "summary": "Phát biểu định hướng, chưa phải triển khai.",
        "rationale": "Đào tạo chăm sóc người cao tuổi.",
        "relevance_passage_id": "P2",
        "facts": [
            {
                "statement": "Tại cuộc họp, lãnh đạo yêu cầu đào tạo chăm sóc người cao tuổi.",
                "passage_id": "P2",
                "event_date_candidate_id": "D1",
                "date_context_passage_id": "P1",
                "date_link_rationale": "P2 nêu tại cuộc họp vừa được định ngày ở P1.",
                "reported_status": "announced",
            }
        ],
        "uncertainties": ["Chưa chứng minh có triển khai."],
    }
    return article, payload


@pytest.mark.parametrize("missing", ["date_context_passage_id", "date_link_rationale"])
def test_live_contract_cannot_silently_omit_date_context_fields(missing):
    article, payload = linked_proposal()
    del payload["facts"][0][missing]
    with pytest.raises(ValueError, match=missing):
        ed.resolve_ids(payload, article)
    required = ed.IDArticleProposal.model_json_schema()["$defs"]["IDFact"]["required"]
    assert missing in required


def test_linked_event_date_requires_source_identity_and_review_confirmation():
    from healthlab.editorial.review_policy import review_gate
    from tests.test_editorial_review_policy import review

    article, payload = linked_proposal()
    p = canonical_v12(ed.resolve_ids(payload, article), article, WINDOW)
    assert p["newsletter_candidate"] and p["facts"][0]["event_date"] == "2026-10-10"
    r = review(review_version="editorial-review-3")
    assert not review_gate(p, r, ["P1", "P2"], WINDOW)["eligible"]
    r["supported_date_fact_indexes"] = [0]
    assert review_gate(p, r, ["P1", "P2"], WINDOW)["eligible"]
    p["facts"][0]["date_evidence"]["start"] = 1
    with pytest.raises(ValueError, match="Date evidence differs"):
        canonical_v12({k: p[k] for k in ed.ArticleProposal.model_fields}, article, WINDOW)


@pytest.mark.parametrize(
    "field,value",
    [
        ("date_context_passage_id", "P999"),
        ("date_context_passage_id", "P2"),
        ("date_link_rationale", None),
    ],
)
def test_unexplained_or_mismatched_date_context_is_rejected(field, value):
    article, payload = linked_proposal()
    payload["facts"][0][field] = value
    with pytest.raises(ValueError, match="Date context"):
        ed.resolve_ids(payload, article)


def test_date_without_year_does_not_become_a_candidate_from_publication_date():
    article = row("Ngày 10/10, lãnh đạo yêu cầu đào tạo chăm sóc người cao tuổi.")
    _, dates = ed.passage_inputs(article, version="gold-editorial-15")
    assert dates == []


def test_current_policy_resolves_a_yearless_date_in_code_and_marks_its_basis():
    article = row("Ngày 10/10, lãnh đạo yêu cầu đào tạo chăm sóc người cao tuổi.")
    _, dates = ed.passage_inputs(article)
    assert [(d["date"], d["basis"]) for d in dates] == [
        ("2026-10-10", "inferred_from_publication_year")
    ]


def test_only_independently_confirmed_linked_dates_reach_writer(tmp_path, monkeypatch):
    from tests.test_editorial_review_policy import review

    article, payload = linked_proposal()
    payload["facts"].append(dict(payload["facts"][0], statement="Một fact chưa rà ngày."))
    store, run, provider = setup_run(
        tmp_path,
        monkeypatch,
        [
            payload,
            review(
                review_version="editorial-review-3",
                supported_fact_indexes=[0, 1],
                supported_date_fact_indexes=[0],
            ),
            scope_assessment(),
        ],
    )
    # Replace the prepared frozen input before inference in this synthetic test only.
    run["input_sha256"] = store.put(ed.json_bytes([article]))
    store.save(run)
    ed.run_calls(store, run["run_id"], provider, workers=1)
    ed.plan_review(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="review", workers=1)
    ed.plan_scope(store, run["run_id"])
    ed.run_calls(store, run["run_id"], provider, role="scope", workers=1)
    packet = ed.evidence_packet(store, run["run_id"])
    assert len(packet["draft_evidence"]) == 1
    assert packet["draft_evidence"][0]["statement"] != "Một fact chưa rà ngày."


def test_full_review_covers_all_negatives_or_rejects_insufficient_budget(tmp_path, monkeypatch):
    from healthlab.store import json_bytes

    store, run, provider = setup_run(tmp_path, monkeypatch, [proposal(relevance="irrelevant")] * 3)
    saved = store.load(run["run_id"])
    records = [dict(row(), document_id=f"id{i}") for i in range(3)]
    saved["input_sha256"] = store.put(json_bytes(records))
    saved["jobs"] = []
    saved["call_budget"] = 6  # Three classify + only two review + reserved writer.
    for article in records:
        job = store.new("editorial_call", WINDOW)
        job.update(role="classify", document_id=article["document_id"], status="prepared")
        store.save(job)
        saved["jobs"].append(job["run_id"])
    store.save(saved)
    ed.run_calls(store, saved["run_id"], provider, workers=1)
    with pytest.raises(ValueError, match="Budget cannot cover full review"):
        ed.plan_review(store, saved["run_id"], all_articles=True)
    assert len(store.load(saved["run_id"])["jobs"]) == 3
    saved = store.load(saved["run_id"])
    saved["call_budget"] = 7
    store.save(saved)
    planned = ed.plan_review(store, saved["run_id"], all_articles=True)
    assert planned["review_scope"]["selected_document_ids"] == ["id0", "id1", "id2"]
    assert planned["review_scope"]["irrelevant_sample"] == 3
    assert len(planned["jobs"]) == 6
