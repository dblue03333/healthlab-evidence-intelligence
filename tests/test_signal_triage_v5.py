"""Frozen v5/v6 regressions: source coverage, exact dates and independent gates."""

import json

import pytest

from healthlab.provider import Completion, FakeProvider
from healthlab.signal.collection import load_documents
from healthlab.signal.triage import load_triage, prepare_triage, propose_triage, source_passages
from healthlab.signal.triage_v5 import (
    Proposal,
    coverage_plan,
    date_candidates,
    literal_dates,
    resolve,
)
from healthlab.store import json_bytes
from tests.test_signal import collected


@pytest.fixture(autouse=True)
def preserve_v6_runtime_contract(monkeypatch):
    monkeypatch.setattr("healthlab.signal.triage.TRIAGE_PROMPT_VERSION", "signal-triage-prompt-6")


def case(tmp_path, text=None):
    store, collection = collected(tmp_path)
    doc = next(iter(load_documents(store, collection).values()))
    doc.text = text or "On 2026-10-08, Vietnam adopted a new older-person care guideline."
    ref = collection["documents"][0]
    ref["snapshot_id"] = store.put(json_bytes(doc.model_dump()))
    store.save(collection)
    triage = prepare_triage(store, collection["run_id"])
    return store, collection, doc, triage


def proposal(doc, sid, **changes):
    units = source_passages(doc)
    dated = next((p for p in units if "2026-10-08" in p["quote"]), units[-1])
    value = dict(
        snapshot_id=sid,
        decision="include",
        topic="aging_society",
        freshness="current_update",
        reported_status="adopted",
        vietnam_relevance="direct",
        jurisdiction="vietnam",
        source_role="original",
        original_source_url=doc.url,
        provenance_note="Synthetic only",
        event_key="fixture-guideline",
        reason_codes=["eligible"],
        rationale="Synthetic dated guideline",
        passages=[
            dict(
                passage_id=dated["passage_id"],
                purposes=["topic", "development", "status", "vietnam_relevance"],
            )
        ],
        development_summary="New guideline",
        freshness_basis="new_action",
        freshness_explanation="Dated source action",
        event_date_candidate_id=date_candidates(doc)[0]["candidate_id"]
        if date_candidates(doc)
        else None,
    )
    return value | changes


def test_dates_are_literal_complete_and_have_exact_unicode_offsets():
    text = "Ngày 08/10/2026; 2026-10-09; ngày 10 tháng 10 năm 2026; 11 October 2026; October 12, 2026. 13/10; 31/02/2026."
    values = literal_dates(text)
    assert [v["date"] for v in values] == [f"2026-10-{i:02}" for i in range(8, 13)]
    assert all(text[v["start"] : v["end"]] == v["quote"] for v in values)


def test_current_schema_cannot_request_free_quotes_or_publication_outside_label(tmp_path):
    _, c, doc, _ = case(tmp_path)
    p = proposal(doc, c["documents"][0]["snapshot_id"])
    assert "event_date_quote" not in Proposal.model_json_schema()["properties"]
    for change in [
        {"event_date_quote": "invented..."},
        {"freshness": "out_of_window"},
        {"passages": [{"passage_id": "P1", "purposes": ["freshness"]}]},
    ]:
        with pytest.raises(ValueError):
            Proposal.model_validate(p | change)


@pytest.mark.parametrize(
    "role,date_id,reasons",
    [
        ("original", None, ["development_unclear"]),
        ("repost", None, ["repost_unverified", "development_unclear"]),
        ("repost", "D1", ["repost_unverified"]),
    ],
)
def test_provenance_and_date_are_independent_gates(tmp_path, role, date_id, reasons):
    _, c, doc, _ = case(tmp_path)
    p = proposal(
        doc, c["documents"][0]["snapshot_id"], source_role=role, event_date_candidate_id=date_id
    )
    result = resolve(json.dumps(p), doc, c["question"])
    assert result.decision == "needs_review" and result.reason_codes == reasons


def test_old_event_cannot_be_new_even_when_original_source_is_known(tmp_path):
    _, c, doc, _ = case(tmp_path, "On 2025-01-02, Vietnam adopted an older-person care guideline.")
    p = proposal(doc, c["documents"][0]["snapshot_id"])
    result = resolve(json.dumps(p), doc, c["question"])
    assert result.decision == "needs_review" and result.freshness == "background"
    assert result.reason_codes == ["event_outside_window"]
    with pytest.raises(ValueError, match="Unknown event date"):
        resolve(json.dumps(p | {"event_date_candidate_id": "D999"}), doc, c["question"])


class ReadingProvider(FakeProvider):
    def __init__(self, doc, sid, *, fail_chunk=None, hidden_citation=False):
        super().__init__([])
        self.doc, self.sid = doc, sid
        self.prompts = []
        self.fail_chunk, self.hidden_citation = fail_chunk, hidden_citation

    def complete(self, messages, audit):
        self.calls += 1
        data = json.loads(messages[1]["content"])
        self.prompts.append(data)
        if data["stage"] == "chunk":
            if self.calls == self.fail_chunk:
                output = {"topic": "aging_society", "evidence_passage_ids": ["P999"]}
            else:
                relevant = [p for p in data["source_passages"] if "care guideline" in p["quote"]]
                output = dict(
                    topic="aging_society" if relevant else "out_of_scope",
                    summary="Dated care guideline" if relevant else "General context",
                    uncertainties="",
                    evidence_passage_ids=[p["passage_id"] for p in relevant],
                    event_date_candidate_ids=[d["candidate_id"] for d in data["date_candidates"]],
                )
        else:
            output = proposal(self.doc, self.sid)
            if self.hidden_citation:
                output["passages"][0]["passage_id"] = "P1"
            if data.get("contract_version") == "signal-triage-prompt-6":
                output = compact(output)
        raw = json.dumps(output).encode()
        audit({"provider": "fake", "http_status": None}, raw)
        return Completion(raw.decode(), "fake-test", "stop", {})


def long_case(tmp_path):
    return case(
        tmp_path,
        ("General context. " * 2300)
        + " On 2026-10-08, Vietnam adopted a new older-person care guideline.",
    )


def test_long_article_reads_every_chunk_and_keeps_evidence_after_20000(tmp_path):
    s, c, doc, t = long_case(tmp_path)
    sid = c["documents"][0]["snapshot_id"]
    provider = ReadingProvider(doc, sid)
    result = propose_triage(s, t["run_id"], provider, expected_head=None, max_documents=1)
    v = load_triage(s, t["run_id"])
    assert result["status"] == "completed" and v["decisions"][sid].decision == "include"
    chunks = [p for p in provider.prompts if p["stage"] == "chunk"]
    assert len(chunks) > 1
    assert [p["passage_id"] for chunk in chunks for p in chunk["source_passages"]] == [
        p["passage_id"] for p in source_passages(doc)
    ]
    assert v["decisions"][sid].passages[0].start > 20_000
    assert provider.calls == len(coverage_plan(doc)["chunks"]) + 1
    assert all(e["reviewer_type"] == "model" for e in v["events"])
    assert "coverage_sha256" in v["events"][0]


def test_incomplete_chunk_proof_cannot_publish_or_replay_a_decision(tmp_path):
    s, c, doc, t = long_case(tmp_path)
    p = ReadingProvider(doc, c["documents"][0]["snapshot_id"])
    propose_triage(s, t["run_id"], p, expected_head=None, max_documents=1)
    run = s.load(t["run_id"])
    event = json.loads(s.read(run["head"]))
    proof = json.loads(s.read(event["coverage_sha256"]))
    proof["steps"].pop(0)
    event["coverage_sha256"] = s.put(json_bytes(proof))
    run["head"] = s.put(json_bytes(event))
    s.save(run)
    with pytest.raises(ValueError, match="Incomplete model chunk coverage"):
        load_triage(s, t["run_id"])


@pytest.mark.parametrize("damage", ["chunk", "hidden_final_citation"])
def test_chunk_error_or_unseen_final_citation_never_becomes_a_decision(tmp_path, damage):
    s, c, doc, t = long_case(tmp_path)
    p = ReadingProvider(
        doc,
        c["documents"][0]["snapshot_id"],
        fail_chunk=2 if damage == "chunk" else None,
        hidden_citation=damage == "hidden_final_citation",
    )
    result = propose_triage(s, t["run_id"], p, expected_head=None, max_documents=1)
    calls = p.calls
    assert result["status"] == "partial" and not load_triage(s, t["run_id"])["decisions"]
    propose_triage(s, t["run_id"], p, expected_head=None, max_documents=1)
    assert p.calls == calls  # no implicit retry or unnoticed publication


def test_oversized_article_explicitly_holds_before_any_provider_call(tmp_path):
    s, c, doc, t = case(tmp_path, "General context. " * 15_000)
    p = ReadingProvider(doc, c["documents"][0]["snapshot_id"])
    result = propose_triage(s, t["run_id"], p, expected_head=None, max_documents=1)
    assert p.calls == 0 and result["model_attempts"][0]["error_code"] == "triage_input_limit"
    assert not load_triage(s, t["run_id"])["decisions"]


def test_malformed_model_json_remains_an_output_error_with_actionable_queue(tmp_path):
    from healthlab.signal.triage import triage_packet

    s, c, doc, t = case(tmp_path)
    p = FakeProvider([Completion("not JSON", "fake-test", "stop", {})])
    result = propose_triage(s, t["run_id"], p, expected_head=None, max_documents=1)
    assert result["model_attempts"][0]["error_code"] == "triage_output_invalid"
    packet = triage_packet(s, t["run_id"])
    assert packet["verification_queue"][0]["processing_error"] == "triage_output_invalid"
    assert packet["verification_queue"][0]["automatic_include"] is False


def test_call_budget_reserves_complete_long_document_before_starting(tmp_path):
    s, c, doc, t = long_case(tmp_path)
    p = ReadingProvider(doc, c["documents"][0]["snapshot_id"])
    result = propose_triage(
        s, t["run_id"], p, expected_head=None, max_documents=1, max_model_calls=1
    )
    assert result["budget_blocked"] and result["remaining_unattempted"] == 1
    assert p.calls == 0 and not result["model_attempts"]


def compact(payload):
    keys = [
        "decision",
        "topic",
        "freshness",
        "reported_status",
        "jurisdiction",
        "vietnam_relevance",
        "source_role",
        "development_summary",
        "freshness_basis",
        "freshness_explanation",
        "rationale",
        "event_date_candidate_id",
    ]
    value = {k: payload[k] for k in keys}
    for field, purpose in [
        ("topic_passage_id", "topic"),
        ("development_passage_id", "development"),
        ("status_passage_id", "status"),
        ("vietnam_passage_id", "vietnam_relevance"),
        ("exclusion_passage_id", "exclusion"),
    ]:
        value[field] = next(
            (p["passage_id"] for p in payload["passages"] if purpose in p["purposes"]), None
        )
    return value


def test_v6_operational_codes_are_local_and_cannot_confuse_event_with_publication(tmp_path):
    from healthlab.signal.triage import resolve_model_decision
    from healthlab.signal.triage_v6 import CompactProposal

    s, c, doc, t = case(tmp_path)
    payload = compact(proposal(doc, c["documents"][0]["snapshot_id"], event_date_candidate_id=None))
    result = resolve_model_decision(
        json.dumps(payload),
        doc,
        prompt_version="signal-triage-prompt-6",
        monitoring_window=c["question"],
    )
    assert result.decision == "needs_review" and result.reason_codes == ["development_unclear"]
    for field in ["snapshot_id", "event_key", "reason_codes", "passages", "event_date_quote"]:
        assert field not in CompactProposal.model_json_schema()["properties"]
        with pytest.raises(ValueError):
            CompactProposal.model_validate(payload | {field: "invented"})


def test_v6_same_passage_can_support_every_purpose_without_model_enum_arrays(tmp_path):
    from healthlab.signal.triage import resolve_model_decision

    s, c, doc, t = case(tmp_path)
    sid = c["documents"][0]["snapshot_id"]
    payload = compact(proposal(doc, sid))
    out = resolve_model_decision(
        json.dumps(payload),
        doc,
        prompt_version="signal-triage-prompt-6",
        monitoring_window=c["question"],
    )
    assert out.snapshot_id == sid and out.decision == "include"
    assert len(out.passages) == 1 and set(out.passages[0].purposes) == {
        "topic",
        "development",
        "status",
        "vietnam_relevance",
    }
