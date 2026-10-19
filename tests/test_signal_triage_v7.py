"""V7 schema requests, source-bound facets, strict failures and trace compatibility."""

import json

import pytest

from healthlab.provider import Completion, FakeProvider
from healthlab.signal.triage import (
    load_triage,
    propose_triage,
    resolve_model_decision,
)
from healthlab.signal.triage_v7 import EvidenceProposal, response_schema
from healthlab.store import json_bytes
from tests.test_signal_triage_v5 import ReadingProvider, case, compact, long_case, proposal


@pytest.fixture(autouse=True)
def experimental_v7(monkeypatch):
    monkeypatch.setattr("healthlab.signal.triage.TRIAGE_PROMPT_VERSION", "signal-triage-prompt-7")


def evidence_payload(doc, sid, **changes):
    value = compact(proposal(doc, sid))
    for key in ["freshness", "reported_status", "freshness_basis"]:
        value.pop(key)
    value.update(
        development_basis="new_action",
        status_subject="Older-person care guideline",
        status_basis="instrument_issued",
    )
    return value | changes


def resolve(doc, collection, payload):
    return resolve_model_decision(
        json.dumps(payload),
        doc,
        prompt_version="signal-triage-prompt-7",
        monitoring_window=collection["question"],
    )


@pytest.mark.parametrize(
    "text,basis,status",
    [
        ("Vietnam proposed a budget for older-person care", "proposal", "proposed"),
        ("Vietnam issued an older-person care plan", "instrument_issued", "adopted"),
        ("An older-person care pilot opened in Vietnam", "pilot_activity", "pilot"),
        (
            "A new ageing research report was published in Vietnam",
            "report_release",
            "published_report",
        ),
    ],
)
def test_status_is_about_the_evidenced_subject_and_independent_of_freshness(
    tmp_path, text, basis, status
):
    _, c, doc, _ = case(tmp_path, f"On 2026-10-08, {text}.")
    p = evidence_payload(
        doc, c["documents"][0]["snapshot_id"], status_basis=basis, status_subject=text
    )
    out = resolve(doc, c, p)
    assert out.reported_status == status and out.freshness == "current_update"
    assert out.decision == "include"  # A proposal is eligible without being implemented.


def test_old_issued_instrument_keeps_adopted_status_but_cannot_be_current(tmp_path):
    _, c, doc, _ = case(tmp_path, "On 2025-01-02, Vietnam issued an older-person care plan.")
    p = evidence_payload(doc, c["documents"][0]["snapshot_id"], decision="exclude")
    out = resolve(doc, c, p)
    assert out.reported_status == "adopted" and out.freshness == "background"
    assert out.reason_codes == ["event_outside_window"]


def test_undated_proposal_holds_instead_of_inheriting_publication_or_becoming_background(tmp_path):
    _, c, doc, _ = case(tmp_path, "Vietnam proposed a budget for older-person care.")
    p = evidence_payload(doc, c["documents"][0]["snapshot_id"], status_basis="proposal")
    out = resolve(doc, c, p)
    assert out.reported_status == "proposed" and out.freshness == "unknown"
    assert out.decision == "needs_review" and "development_unclear" in out.reason_codes


@pytest.mark.parametrize("missing", ["status_passage_id", "development_passage_id"])
def test_missing_status_or_development_evidence_blocks_include(tmp_path, missing):
    _, c, doc, _ = case(tmp_path)
    out = resolve(
        doc, c, evidence_payload(doc, c["documents"][0]["snapshot_id"], **{missing: None})
    )
    assert out.decision == "needs_review"
    if missing == "status_passage_id":
        assert out.reported_status == "unknown"
    else:
        assert out.freshness == "unknown"


def test_model_cannot_override_server_derived_facets(tmp_path):
    _, c, doc, _ = case(tmp_path)
    p = evidence_payload(doc, c["documents"][0]["snapshot_id"])
    for extra in [
        {"freshness": "current_update"},
        {"reported_status": "operational"},
        {"freshness_basis": "new_report"},
        {"status_subject": " "},
    ]:
        with pytest.raises(ValueError):
            EvidenceProposal.model_validate(p | extra)


class EvidenceReadingProvider(ReadingProvider):
    def complete(self, messages, audit):
        data = json.loads(messages[1]["content"])
        if data["stage"] == "chunk":
            return super().complete(messages, audit)
        self.calls += 1
        self.prompts.append(data)
        raw = json.dumps(evidence_payload(self.doc, self.sid))
        audit({"provider": "fake", "http_status": None}, raw.encode())
        return Completion(raw, "fake-test", "stop", {})


def test_chunk_and_final_requests_have_separate_pinned_schemas_and_replay(tmp_path):
    store, collection, doc, triage = long_case(tmp_path)
    sid = collection["documents"][0]["snapshot_id"]
    provider = EvidenceReadingProvider(doc, sid)
    result = propose_triage(store, triage["run_id"], provider, expected_head=None, max_documents=1)
    assert result["status"] == "completed"
    view = load_triage(store, triage["run_id"])
    assert view["decisions"][sid].decision == "include"
    attempt = view["run"]["model_attempts"][0]
    assert len(provider.response_schemas) == provider.calls == len(attempt["steps"])
    for step, schema in zip(attempt["steps"], provider.response_schemas, strict=True):
        data = json.loads(json.loads(store.read(step["prompt_sha256"]))[1]["content"])
        assert (
            json.loads(store.read(step["response_schema_sha256"]))
            == response_schema(data)
            == schema
        )
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
    event = json.loads(store.read(view["head"]))
    proof = json.loads(store.read(event["coverage_sha256"]))
    schema = provider.response_schemas[-1] | {"additionalProperties": True}
    proof["steps"][-1]["response_schema_sha256"] = store.put(json_bytes(schema))
    event["coverage_sha256"] = store.put(json_bytes(proof))
    view["run"]["head"] = store.put(json_bytes(event))
    store.save(view["run"])
    with pytest.raises(ValueError, match="schema trace"):
        load_triage(store, triage["run_id"])


def test_malformed_schema_response_still_fails_without_repair_or_retry(tmp_path):
    store, _, _, triage = case(tmp_path)
    provider = FakeProvider([Completion('{**"decision":"include"**}', "fake-test", "stop", {})])
    result = propose_triage(store, triage["run_id"], provider, expected_head=None, max_documents=1)
    assert result["model_attempts"][0]["error_code"] == "triage_output_invalid"
    assert not load_triage(store, triage["run_id"])["decisions"]
    propose_triage(store, triage["run_id"], provider, expected_head=None, max_documents=1)
    assert provider.calls == 1


def test_no_schema_provider_does_not_fall_back_to_text(tmp_path):
    class TextOnly:
        def identity(self):
            return {"provider": "text_only"}

        def complete(self, messages, audit):
            raise AssertionError("Must not fall back to unconstrained text")

    store, _, _, triage = case(tmp_path)
    result = propose_triage(
        store, triage["run_id"], TextOnly(), expected_head=None, max_documents=1
    )
    assert result["model_attempts"][0]["error_code"] == "triage_model_failed"
    assert not store.load(triage["run_id"])["requests"]


def test_wire_schema_restricts_ids_to_the_actual_request():
    data = dict(stage="final", source_passages=[{"passage_id": "P2"}], date_candidates=[])
    schema = response_schema(data)
    assert schema["properties"]["event_date_candidate_id"] == {"type": "null"}
    assert schema["properties"]["topic_passage_id"]["anyOf"][0]["enum"] == ["P2"]


def test_v8_explicit_json_object_mode_preserves_local_contract_and_trace(tmp_path):
    store, collection, doc, triage = long_case(tmp_path)
    sid = collection["documents"][0]["snapshot_id"]
    provider = EvidenceReadingProvider(doc, sid)
    result = propose_triage(
        store,
        triage["run_id"],
        provider,
        expected_head=None,
        max_documents=1,
        prompt_version="signal-triage-prompt-8",
    )
    assert result["status"] == "completed"
    view = load_triage(store, triage["run_id"])
    assert view["decisions"][sid].reported_status == "adopted"
    assert all(
        step["response_format"] == "json_object"
        for step in view["run"]["model_attempts"][0]["steps"]
    )


def test_v9_explicit_text_mode_keeps_the_evidence_contract_without_native_json_calls(tmp_path):
    store, collection, doc, triage = case(tmp_path)
    sid = collection["documents"][0]["snapshot_id"]

    class TextEvidence(EvidenceReadingProvider):
        def complete_json(self, *args, **kwargs):
            raise AssertionError("Explicit text mode must not silently request JSON Schema")

        def complete_json_object(self, *args, **kwargs):
            raise AssertionError("Explicit text mode must not silently request JSON-object mode")

    provider = TextEvidence(doc, sid)
    result = propose_triage(
        store,
        triage["run_id"],
        provider,
        expected_head=None,
        prompt_version="signal-triage-prompt-9",
        max_documents=1,
    )
    assert result["status"] == "completed"
    view = load_triage(store, triage["run_id"])
    assert view["decisions"][sid].reported_status == "adopted"
    assert view["run"]["model_attempts"][0]["steps"][0]["response_format"] == "text"
