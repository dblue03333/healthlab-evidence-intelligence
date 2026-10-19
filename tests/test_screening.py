import json

import pytest

from healthlab.models import IngestionError
from healthlab.provider import Completion, FakeProvider
from healthlab.pubmed import parse_records
from healthlab.screening import (
    load_sources,
    screen_run,
    source_passages,
    validate,
    validate_citations,
)
from healthlab.store import json_bytes
from tests.test_extraction import source_run
from tests.test_ingestion import XML


def proposal(text="Sixty participants were randomized.", section=1, **updates):
    return {
        "decision": "maybe",
        "direction": "none_identified",
        "rationale": "Chưa đủ thông tin liên quan.",
        "evidence": [{"kind": "context", "section_index": section, "text": text}],
        **updates,
    }


def reply(payload, finish="stop"):
    payload = {
        **payload,
        "evidence": [
            {"kind": q["kind"], "passage_id": f"s{q['section_index']}p0"}
            for q in payload["evidence"]
        ],
    }
    return Completion(json.dumps(payload), "fake-test", finish, {"total_tokens": 10})


def test_reference_resolution_preserves_unicode_case_and_repeated_sentence_offsets():
    doc = parse_records(XML)[0][0]
    sentence = "However, telehealth needs broadband\u00a0access."
    doc.abstract_sections[0].text = f"{sentence} {sentence}"
    units = source_passages(doc)
    assert units[1]["passage_id"] == "s0p1"
    result, quotes = validate_citations(
        json.dumps(proposal(evidence=[{"kind": "context", "passage_id": "s0p1"}])),
        doc,
        "a" * 64,
    )
    assert result.evidence[0].text == sentence
    assert quotes[0]["start"] == len(sentence) + 1
    assert quotes[0]["end"] == len(sentence) * 2 + 1
    assert quotes[0]["source_passage_id"] == "s0p1"
    assert doc.abstract_sections[0].text[quotes[0]["start"] : quotes[0]["end"]] == sentence


@pytest.mark.parametrize(
    "evidence,updates,code",
    [
        ([{"kind": "context", "passage_id": "s99p0"}], {}, "screening_reference"),
        ([{"kind": "context", "passage_id": "s0p0", "text": "Invented"}], {}, "screening_schema"),
        ([{"kind": "context", "passage_id": "s0p0"}], {"decision": "keep"}, "screening_schema"),
    ],
)
def test_invalid_citations_and_inconsistent_decisions_rejected(evidence, updates, code):
    with pytest.raises(IngestionError) as exc:
        validate_citations(
            json.dumps(proposal(evidence=evidence, **updates)), parse_records(XML)[0][0], "a" * 64
        )
    assert exc.value.code == code


def test_offsets_and_human_fields_are_not_model_decisions():
    doc = parse_records(XML)[0][0]
    result, quotes = validate(json.dumps(proposal()), doc, "a" * 64)
    assert result.decision == "maybe"
    q = quotes[0]
    assert doc.abstract_sections[q["section_index"]].text[q["start"] : q["end"]] == q["text"]
    with pytest.raises(IngestionError):
        validate(json.dumps(proposal(human_label="keep")), doc, "a" * 64)


@pytest.mark.parametrize(
    "change",
    [
        {"decision": "keep"},
        {"direction": "both"},
        {"direction": "unclear", "decision": "not_relevant"},
        {"evidence": []},
    ],
)
def test_inconsistent_policy_rejected(change):
    with pytest.raises(IngestionError):
        validate(json.dumps(proposal(**change)), parse_records(XML)[0][0], "a" * 64)


def test_both_directions_keep_with_two_grounded_relations():
    doc = parse_records(XML)[0][0]
    doc.abstract_sections[
        0
    ].text = "Telehealth removes distance barriers, but poor broadband limits adoption."
    p = proposal(
        decision="keep",
        direction="both",
        evidence=[
            {"kind": "to_telehealth", "section_index": 0, "text": "poor broadband limits adoption"},
            {
                "kind": "overcome_by_telehealth",
                "section_index": 0,
                "text": "Telehealth removes distance barriers",
            },
        ],
    )
    assert validate(json.dumps(p), doc, "b" * 64)[0].decision == "keep"


@pytest.mark.parametrize(
    "text,section", [("Invented quote.", 1), ("Sixty participants were randomized.", 9), ("   ", 0)]
)
def test_bad_quote_rejected(text, section):
    with pytest.raises(IngestionError):
        validate(json.dumps(proposal(text, section)), parse_records(XML)[0][0], "a" * 64)


def test_screen_run_keeps_unknowns_and_separates_labels(tmp_path):
    store, parent = source_run(tmp_path)
    provider = FakeProvider([reply(proposal()), reply(proposal("One full paragraph.", 0))])
    run = screen_run(store, parent["run_id"], provider, tmp_path / "review")
    assert provider.calls == 2 and run["status"] == "partial"
    assert run["counts"] == {"input": 3, "validated_proposals": 2, "needs_review": 1, "failed": 0}
    assert all(r["human_label"] is None and not r["automatically_excluded"] for r in run["results"])
    assert all(
        "barrier_direction" not in json.loads(store.read(r["input_sha256"]))[1]["content"]
        for r in run["results"]
        if r.get("input_sha256")
    )
    with pytest.raises(FileExistsError):
        screen_run(store, parent["run_id"], provider, tmp_path / "review")


def test_failed_completion_is_not_a_maybe_or_exclusion(tmp_path):
    store, parent = source_run(tmp_path)
    run = screen_run(
        store,
        parent["run_id"],
        FakeProvider(
            [reply(proposal(), finish="length"), reply(proposal("One full paragraph.", 0))]
        ),
        tmp_path / "review",
    )
    e = run["results"][0]
    assert e["status"] == "failed" and e["ai_proposal"] is None
    assert e["content_sha256"] and e["error"]["code"] == "screening_incomplete"


def test_quota_stops_further_calls(tmp_path):
    store, parent = source_run(tmp_path)
    provider = FakeProvider([IngestionError("provider_quota_or_rate", "quota")])
    run = screen_run(store, parent["run_id"], provider, tmp_path / "review")
    assert provider.calls == 1
    assert run["counts"]["failed"] == 1 and run["counts"]["needs_review"] == 2


def test_probe_normalized_records_must_match_audited_xml(tmp_path):
    store, _ = source_run(tmp_path)
    docs, _ = parse_records(XML)
    probe = store.new("telehealth_query_probe", {})
    probe.update(
        status="completed",
        pasted_pmids=[d.pmid for d in docs],
        pasted_records_xml_sha256=store.put(XML),
        records_sha256=store.put(json_bytes([d.model_dump() for d in docs])),
    )
    store.save(probe)
    assert load_sources(store, probe["run_id"])[2] == "user_supplied_pmid_set_not_live_ranking"
    docs[0].title = "Modified"
    probe["records_sha256"] = store.put(json_bytes([d.model_dump() for d in docs]))
    store.save(probe)
    with pytest.raises(IngestionError, match="differ"):
        load_sources(store, probe["run_id"])


def test_generic_barrier_direction_does_not_force_keep():
    doc = parse_records(XML)[0][0]
    doc.abstract_sections[0].text = "We discuss barriers to telehealth."
    p = proposal(
        decision="maybe",
        direction="to_telehealth",
        evidence=[
            {
                "kind": "to_telehealth",
                "section_index": 0,
                "text": "We discuss barriers to telehealth.",
            }
        ],
    )
    assert validate(json.dumps(p), doc, "a" * 64)[0].decision == "maybe"
    p["decision"] = "not_relevant"
    with pytest.raises(IngestionError):
        validate(json.dumps(p), doc, "a" * 64)
