import json

import httpx
import pytest

from healthlab.__main__ import main
from healthlab.brief import md
from healthlab.extraction import extract_run
from healthlab.models import IngestionError, Question
from healthlab.pipeline import ingest
from healthlab.provider import Completion, FakeProvider
from healthlab.store import RunStore, json_bytes
from healthlab.synthesis import load_bundle, render_saved_brief, synthesize_run
from tests.test_extraction import completion, evidence, second_completion, source_run
from tests.test_ingestion import connector, search_bytes


def extracted_source(tmp_path):
    store, source = source_run(tmp_path)
    run = extract_run(store, source["run_id"], FakeProvider([completion(), second_completion()]))
    return store, source, run


def synthesis_output(items, text="The study reported randomization of sixty participants."):
    return {
        "claims": [
            {
                "claim_id": "C1",
                "kind": "finding",
                "inference": "reported_result",
                "text": text,
                "references": [
                    {
                        "evidence_item_id": items[0]["evidence_item_id"],
                        "passage_id": items[0]["passages"][0]["passage_id"],
                    }
                ],
            }
        ],
        "short_answer_claim_ids": ["C1"],
        "evidence_sufficiency": "limited",
    }


def response(payload, finish="stop"):
    return Completion(json.dumps(payload), "fake-test", finish, {"total_tokens": 100})


def test_full_brief_lineage_links_scope_and_offline_restore(tmp_path, monkeypatch):
    store, source, extraction = extracted_source(tmp_path)
    items = load_bundle(store, extraction["run_id"])[2]
    run = synthesize_run(
        store, extraction["run_id"], FakeProvider([response(synthesis_output(items))])
    )
    assert run["status"] == "completed" and run["counts"]["claims"] == 1
    brief = json.loads(store.read(run["brief_sha256"]))
    assert (
        brief["validation"]["references_valid"]
        and brief["validation"]["semantic_support"] == "not_evaluated"
    )
    assert brief["review_status"] == "AI-generated — not reviewed"
    assert brief["coverage"]["matched"] == 116
    assert brief["coverage"]["search_params"]["sort"] == "relevance"
    assert brief["coverage"]["search_observed_at"].endswith("+00:00")
    preview = store.root / "runs" / run["run_id"] / "brief.md"
    content = preview.read_text()
    assert "https://pubmed.ncbi.nlm.nih.gov/101/" in content
    assert "PubMed metadata/abstracts only" in content
    assert "Population:" in content and "Source context:" in content
    assert len(content.split('<a id="passage-')) - 1 == len(
        {p["passage_id"] for item in items for p in item["passages"]}
    )
    import re

    for target in re.findall(r"\]\(([^)]+)\)", content):
        if not target.startswith(("https:", "#")):
            assert (preview.parent / target).exists(), target
    for claim in brief["synthesis"]["claims"]:
        for ref in claim["references"]:
            item = next(i for i in items if i["evidence_item_id"] == ref["evidence_item_id"])
            assert any(p["passage_id"] == ref["passage_id"] for p in item["passages"])
            snapshot = json.loads(store.read(item["snapshot_id"]))
            assert snapshot["document"]["pmid"] == item["document"]["pmid"]
            store.read(snapshot["raw_record_sha256"])
    preview.write_text("changed preview")
    monkeypatch.setattr(
        httpx.Client, "__init__", lambda *a, **k: pytest.fail("offline render called HTTP")
    )
    assert render_saved_brief(store, run["run_id"]).read_text() == content
    assert main(["--store", str(tmp_path), "render-brief", run["run_id"]]) == 0


@pytest.mark.parametrize("target", ["evidence_item_id", "passage_id"])
def test_fabricated_reference_blocks_brief(tmp_path, target):
    store, _, extraction = extracted_source(tmp_path)
    items = load_bundle(store, extraction["run_id"])[2]
    payload = synthesis_output(items)
    payload["claims"][0]["references"][0][target] = "a" * 64
    run = synthesize_run(store, extraction["run_id"], FakeProvider([response(payload)]))
    assert run["status"] == "failed" and run["errors"][0]["code"] == "invalid_citation"
    assert store.read(run["content_sha256"])
    assert "brief_sha256" not in run
    assert not (tmp_path / "runs" / run["run_id"] / "brief.md").exists()


def test_passage_from_other_evidence_is_not_accepted(tmp_path):
    store, _, extraction = extracted_source(tmp_path)
    items = load_bundle(store, extraction["run_id"])[2]
    payload = synthesis_output(items)
    payload["claims"][0]["references"][0]["passage_id"] = items[1]["passages"][0]["passage_id"]
    run = synthesize_run(store, extraction["run_id"], FakeProvider([response(payload)]))
    assert run["errors"][0]["code"] == "invalid_citation"


@pytest.mark.parametrize(
    "text",
    [
        "See https://fake.example/paper",
        "PMID 123 says yes",
        "DOI 10.1234/fake says yes",
        "[source](javascript:alert(1))",
    ],
)
def test_model_cannot_author_citation_urls(tmp_path, text):
    store, _, extraction = extracted_source(tmp_path)
    items = load_bundle(store, extraction["run_id"])[2]
    run = synthesize_run(
        store, extraction["run_id"], FakeProvider([response(synthesis_output(items, text))])
    )
    assert run["status"] == "failed" and run["errors"][0]["code"] == "brief_schema_invalid"


@pytest.mark.parametrize(
    "mutation", ["empty_refs", "unknown_short_answer", "duplicate_claim", "extra_summary"]
)
def test_every_claim_and_short_answer_has_valid_contract(tmp_path, mutation):
    store, _, extraction = extracted_source(tmp_path)
    payload = synthesis_output(load_bundle(store, extraction["run_id"])[2])
    if mutation == "empty_refs":
        payload["claims"][0]["references"] = []
    if mutation == "unknown_short_answer":
        payload["short_answer_claim_ids"] = ["C9"]
    if mutation == "duplicate_claim":
        payload["claims"].append(payload["claims"][0])
    if mutation == "extra_summary":
        payload["short_answer"] = "Unsupported generated summary"
    run = synthesize_run(store, extraction["run_id"], FakeProvider([response(payload)]))
    assert run["errors"][0]["code"] == "brief_schema_invalid"


def test_no_results_brief_does_not_call_model_or_claim_no_effect(tmp_path, monkeypatch):
    c, _ = connector(lambda req: httpx.Response(200, content=search_bytes([], 0)))
    store = RunStore(tmp_path)
    source = ingest(store, Question(raw_question="q", query="q"), c)
    extraction = extract_run(store, source["run_id"], FakeProvider([]))
    fake = FakeProvider([])
    run = synthesize_run(store, extraction["run_id"], fake)
    assert (
        run["outcome"] == "no_usable_evidence" and run["status"] == "completed" and fake.calls == 0
    )
    content = store.read(run["markdown_sha256"]).decode()
    assert "không chứng minh không có tác dụng" in content
    assert "0 matching records" in content
    monkeypatch.setenv("FPT_API_KEY", "")
    monkeypatch.setattr(
        httpx.Client, "__init__", lambda *a, **k: pytest.fail("empty brief constructed HTTP")
    )
    assert main(["--store", str(tmp_path), "brief", extraction["run_id"]]) == 0


def test_partial_extraction_and_model_failure_preserve_upstream(tmp_path):
    store, source = source_run(tmp_path)
    extraction = extract_run(
        store,
        source["run_id"],
        FakeProvider([IngestionError("provider_timeout", "failed"), second_completion()]),
    )
    items = load_bundle(store, extraction["run_id"])[2]
    run = synthesize_run(
        store, extraction["run_id"], FakeProvider([response(synthesis_output(items))])
    )
    assert run["status"] == "partial"
    assert any("incomplete" in w for w in run["warnings"])
    failed = synthesize_run(
        store, extraction["run_id"], FakeProvider([IngestionError("provider_timeout", "failed")])
    )
    assert failed["status"] == "failed" and failed["errors"][0]["code"] == "provider_timeout"
    assert store.load(source["run_id"])["status"] == "completed"
    assert store.load(extraction["run_id"])["status"] == "partial"


def test_all_extractions_failed_is_not_no_search_results(tmp_path):
    store, source = source_run(tmp_path)
    extraction = extract_run(
        store,
        source["run_id"],
        FakeProvider(
            [
                IngestionError("provider_timeout", "failed"),
                IngestionError("provider_timeout", "failed"),
            ]
        ),
    )
    run = synthesize_run(store, extraction["run_id"], FakeProvider([]))
    assert run["status"] == "partial" and run["outcome"] == "no_usable_evidence"
    brief = json.loads(store.read(run["brief_sha256"]))
    assert (
        brief["coverage"]["matched"] == 116
        and brief["coverage"]["extraction_counts"]["failed"] == 2
    )


def test_regeneration_creates_new_unreviewed_version(tmp_path):
    store, _, extraction = extracted_source(tmp_path)
    payload = synthesis_output(load_bundle(store, extraction["run_id"])[2])
    one = synthesize_run(store, extraction["run_id"], FakeProvider([response(payload)]))
    original = (tmp_path / "runs" / one["run_id"] / "manifest.json").read_bytes()
    two = synthesize_run(store, extraction["run_id"], FakeProvider([response(payload)]))
    assert one["brief_version_id"] != two["brief_version_id"]
    assert one["brief_sha256"] != two["brief_sha256"]
    assert two["review_status"] == "AI-generated — not reviewed"
    assert (tmp_path / "runs" / one["run_id"] / "manifest.json").read_bytes() == original


def test_source_tampering_blocks_synthesis_before_model(tmp_path):
    store, _, extraction = extracted_source(tmp_path)
    item = extraction["results"][0]
    (tmp_path / "objects" / item["evidence_item_id"]).write_text("{}")
    fake = FakeProvider([])
    run = synthesize_run(store, extraction["run_id"], fake)
    assert run["status"] == "failed" and run["errors"][0]["code"] == "cache_corrupt"
    assert fake.calls == 0


def test_modified_passage_offsets_are_revalidated(tmp_path):
    store, _, extraction = extracted_source(tmp_path)
    entry = extraction["results"][0]
    evidence_item = json.loads(store.read(entry["evidence_item_id"]))
    evidence_item["passages"][0]["start"] = 42
    entry["evidence_item_id"] = store.put(json_bytes(evidence_item))
    store.save(extraction)
    run = synthesize_run(store, extraction["run_id"], FakeProvider([]))
    assert run["errors"][0]["code"] == "invalid_lineage"


def test_manual_test_records_are_excluded(tmp_path):
    store, source, extraction = extracted_source(tmp_path)
    source["selection"]["manual_test_pmids"] = ["101"]
    store.save(source)
    items = load_bundle(store, extraction["run_id"])[2]
    assert all(i["document"]["pmid"] != "101" for i in items)


def test_conflicts_require_two_studies_and_render_both_contexts(tmp_path):
    store, _, extraction = extracted_source(tmp_path)
    items = load_bundle(store, extraction["run_id"])[2]
    payload = synthesis_output(items, "Results differ across the two study contexts.")
    payload["claims"][0]["kind"] = "conflict"
    failed = synthesize_run(store, extraction["run_id"], FakeProvider([response(payload)]))
    assert failed["errors"][0]["code"] == "invalid_conflict"
    payload["claims"][0]["references"].append(
        {
            "evidence_item_id": items[1]["evidence_item_id"],
            "passage_id": items[1]["passages"][0]["passage_id"],
        }
    )
    run = synthesize_run(store, extraction["run_id"], FakeProvider([response(payload)]))
    assert run["status"] == "completed"
    content = store.read(run["markdown_sha256"]).decode()
    assert content.count("Population:") == 2 and content.count("Source context:") == 2


def test_observational_causation_guardrail(tmp_path):
    store, source = source_run(tmp_path)
    extraction = extract_run(
        store,
        source["run_id"],
        FakeProvider([completion(evidence(study_type="cohort study")), second_completion()]),
    )
    items = load_bundle(store, extraction["run_id"])[2]
    payload = synthesis_output(items, "The treatment causes a better outcome.")
    payload["claims"][0]["inference"] = "causal"
    run = synthesize_run(store, extraction["run_id"], FakeProvider([response(payload)]))
    assert run["errors"][0]["code"] == "causal_overreach"


@pytest.mark.parametrize(
    "text",
    [
        "There is no evidence for this intervention.",
        "This is clinically proven.",
        "Approved by HealthLab.",
    ],
)
def test_overconfident_coverage_or_review_claim_rejected(tmp_path, text):
    store, _, extraction = extracted_source(tmp_path)
    items = load_bundle(store, extraction["run_id"])[2]
    run = synthesize_run(
        store, extraction["run_id"], FakeProvider([response(synthesis_output(items, text))])
    )
    assert run["errors"][0]["code"] == "unsupported_conclusion"


def test_empty_irrelevant_evidence_selection_produces_insufficient_answer(tmp_path):
    store, _, extraction = extracted_source(tmp_path)
    payload = {"claims": [], "short_answer_claim_ids": [], "evidence_sufficiency": "insufficient"}
    run = synthesize_run(store, extraction["run_id"], FakeProvider([response(payload)]))
    assert run["status"] == "completed" and run["counts"]["claims"] == 0


@pytest.mark.parametrize(
    "content,finish,code",
    [
        ("{", "stop", "invalid_json"),
        ("{}", "stop", "brief_schema_invalid"),
        ("{}", "length", "synthesis_incomplete"),
    ],
)
def test_bad_model_output_never_publishes_brief(tmp_path, content, finish, code):
    store, _, extraction = extracted_source(tmp_path)
    run = synthesize_run(
        store, extraction["run_id"], FakeProvider([Completion(content, "fake", finish, {})])
    )
    assert run["errors"][0]["code"] == code and "brief_sha256" not in run
    with pytest.raises(IngestionError):
        render_saved_brief(store, run["run_id"])


def test_render_escapes_source_and_generated_markup():
    result = md("<script>alert(1)</script> [bad](javascript:x) | data")
    assert "<script>" not in result and "[bad](" not in result and r"\|" in result


def test_source_context_is_bound_to_snapshot_and_not_a_finding(tmp_path):
    store, _, extraction = extracted_source(tmp_path)
    items = load_bundle(store, extraction["run_id"])[2]
    context = next(p for p in items[0]["passages"] if p["field"].startswith("source_context."))
    source = items[0]["document"]["abstract_sections"][context["section_index"]]["text"]
    assert context["text"] == source and context["start"] == 0 and context["end"] == len(source)
    payload = synthesis_output(items)
    payload["claims"][0]["references"][0]["passage_id"] = context["passage_id"]
    run = synthesize_run(store, extraction["run_id"], FakeProvider([response(payload)]))
    assert run["errors"][0]["code"] == "unsupported_finding"


def test_gap_requires_author_limitation_not_any_valid_quote(tmp_path):
    store, _, extraction = extracted_source(tmp_path)
    items = load_bundle(store, extraction["run_id"])[2]
    payload = synthesis_output(items)
    payload["claims"][0]["kind"] = "evidence_gap"
    run = synthesize_run(store, extraction["run_id"], FakeProvider([response(payload)]))
    assert run["errors"][0]["code"] == "unsupported_gap"


def test_no_finding_is_excluded_before_synthesis(tmp_path):
    store, source = source_run(tmp_path)
    null_evidence = evidence(
        main_finding=None, supporting_passage=None, section_index=None, sample_sizes=[]
    )
    extraction = extract_run(
        store, source["run_id"], FakeProvider([completion(null_evidence), second_completion()])
    )
    _, _, items, coverage, _ = load_bundle(store, extraction["run_id"])
    assert len(items) == 1 and any(
        e["reason"] == "no_extracted_finding" for e in coverage["excluded"]
    )


def test_reasoning_only_truncation_is_not_unknown_envelope(tmp_path):
    from tests.test_extraction import fpt

    store, _, extraction = extracted_source(tmp_path)
    provider = fpt(
        lambda request: httpx.Response(
            200,
            json={
                "model": "fixture-model",
                "usage": {"total_tokens": 5000},
                "choices": [{"finish_reason": "length", "message": {"content": None}}],
            },
        )
    )
    run = synthesize_run(store, extraction["run_id"], provider)
    assert run["status"] == "failed" and run["errors"][0]["code"] == "synthesis_incomplete"
    assert run["finish_reason"] == "length" and run["usage"]["total_tokens"] == 5000
    assert "brief_sha256" not in run
    assert store.load(extraction["run_id"])["status"] == "completed"
