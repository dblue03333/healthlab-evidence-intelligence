"""Fixed-position choices cannot generate duplicate JSON object keys."""

import json

import pytest

from healthlab.provider import Completion, FakeProvider
from healthlab.signal.triage import load_triage, propose_triage, triage_packet
from healthlab.signal.triage_v10 import FIELDS, canonical
from tests.test_signal_triage_v5 import case, long_case
from tests.test_signal_triage_v7 import evidence_payload


class VectorReader(FakeProvider):
    def __init__(self, doc, sid):
        super().__init__([])
        self.doc = doc
        self.sid = sid

    def complete(self, messages, audit):
        self.calls += 1
        data = json.loads(messages[1]["content"])
        if data["stage"] == "chunk":
            pids = [
                p["passage_id"] for p in data["source_passages"] if "care guideline" in p["quote"]
            ]
            value = [
                "aging_society" if pids else "out_of_scope",
                pids,
                [d["candidate_id"] for d in data["date_candidates"]],
            ]
        else:
            payload = evidence_payload(self.doc, self.sid)
            value = [payload[f] for f in FIELDS]
        content = json.dumps(value)
        audit({"provider": "fake", "http_status": None}, content.encode())
        return Completion(content, "fake-test", "stop", {})


def test_vector_chunk_and_final_replay_with_code_owned_explanations(tmp_path):
    store, collection, doc, triage = long_case(tmp_path)
    sid = collection["documents"][0]["snapshot_id"]
    provider = VectorReader(doc, sid)
    result = propose_triage(
        store,
        triage["run_id"],
        provider,
        expected_head=None,
        max_documents=1,
        prompt_version="signal-triage-prompt-10",
    )
    assert result["status"] == "completed"
    view = load_triage(store, triage["run_id"])
    assert view["decisions"][sid].decision == "include"
    assert "Giải thích do code tạo" in view["decisions"][sid].rationale
    row = triage_packet(store, triage["run_id"])["rows"][0]
    assert row["model_assessment"]["explanation_origin"] == "code"
    assert len(row["model_assessment"]["raw_choice_vector"]) == 13
    assert "rationale" not in row["model_assessment"]["proposal"]


@pytest.mark.parametrize(
    "content", ['{"decision":"include","decision":"exclude"}', '["include"]', "[NaN]"]
)
def test_wrong_shapes_and_non_json_are_rejected_without_repair(tmp_path, content):
    _, collection, doc, _ = case(tmp_path)
    with pytest.raises(ValueError):
        canonical(content, doc, collection["question"])


def test_vector_still_checks_unknown_evidence_and_undated_proposals(tmp_path):
    _, collection, doc, _ = case(tmp_path, "Vietnam proposed an older-person care budget.")
    sid = collection["documents"][0]["snapshot_id"]
    payload = evidence_payload(doc, sid, status_basis="proposal")
    out = canonical(json.dumps([payload[f] for f in FIELDS]), doc, collection["question"])
    assert out.reported_status == "proposed" and out.freshness == "unknown"
    payload["topic_passage_id"] = "P999"
    with pytest.raises(ValueError, match="Unknown vector passage"):
        canonical(json.dumps([payload[f] for f in FIELDS]), doc, collection["question"])
