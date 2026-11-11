"""Agent verification must retain audited evidence and never manufacture human labels."""

import copy
import json

import httpx
import pytest

from healthlab.provider import FakeProvider
from healthlab.signal.collection import load_documents
from healthlab.signal.html_tree import ArticleTree
from healthlab.signal.newsletter import draft_issue, prepare_email
from healthlab.signal.source_http import FetchPolicy, HTMLFetcher
from healthlab.signal.triage import load_triage, prepare_triage, submit_triage, triage_packet
from healthlab.signal.verification import submit_agent_verification
from healthlab.store import json_bytes
from tests.test_signal import collected
from tests.test_signal_triage import decision_for


def test_v2_event_date_citation_must_support_the_date_even_with_traced_original(tmp_path):
    store, collection, triage, bundle = verified_case(tmp_path)
    bundle["assessments"][0]["event_date"] = "2026-10-07"
    # Original is traced and 7 Oct is in-window, but the evidence actually says 8 Oct.
    with pytest.raises(ValueError, match="complete literal date"):
        submit_agent_verification(store, triage["run_id"], bundle, expected_head=None)
    assert load_triage(store, triage["run_id"])["head"] is None


def verified_case(tmp_path, event_date="2026-10-08", footer="https://original.example.org/"):
    store, collection = collected(tmp_path)
    doc = next(iter(load_documents(store, collection).values()))
    shared = "The report describes older adults. The announced report concerns their care."
    doc.text = doc.text.replace("The report describes older adults.", shared)
    doc.text += "\nNguồn: " + footer
    key = store.put(json_bytes(doc.model_dump()))
    collection["documents"][0]["snapshot_id"] = key
    store.save(collection)
    triage = prepare_triage(store, collection["run_id"])
    original = "https://original.example.org/reported-update"
    text = shared + f" Published {event_date}. Announced on {event_date}."
    capture = store.new("signal_source_verification", {"triage_run_id": triage["run_id"]})
    checkpoint = {"responses": {}, "http_attempts": []}
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, headers={"Content-Type": "text/html"}, text="<html><p>" + text + "</p></html>"
            )
        )
    )
    raw, digest = HTMLFetcher(
        client, store, FetchPolicy(max_attempts=1), sleep=lambda _: None
    ).fetch(original, checkpoint, lambda: None)
    capture.update(
        pages={original: {"status": "captured", "raw_sha256": digest}}, checkpoint=checkpoint
    )
    store.save(capture)

    def cite(quote, purpose):
        full = ArticleTree(raw).root.text()
        start = full.index(quote)
        return {
            "capture_run_id": capture["run_id"],
            "url": original,
            "raw_sha256": digest,
            "start": start,
            "end": start + len(quote),
            "quote": quote,
            "purpose": purpose,
        }

    value = decision_for(store, collection, source_role="repost", original_source_url=original)
    value["passages"][0]["quote"] = "The report describes older adults."
    value["passages"][0]["end"] = value["passages"][0]["start"] + len(value["passages"][0]["quote"])
    bundle = {
        "triage_run_id": triage["run_id"],
        "input_sha256": triage["input_sha256"],
        "base_head": None,
        "author": "Synthetic agent fixture",
        "assessments": [
            {
                "decision": value,
                "event_date": event_date,
                "findings": "Synthetic fixture only",
                "evidence": [
                    cite(shared, "original_match"),
                    cite(f"Published {event_date}", "publication"),
                    cite(f"Announced on {event_date}", "event_date"),
                    cite(f"Announced on {event_date}", "status"),
                ],
            }
        ],
    }
    client.close()
    return store, collection, triage, bundle


@pytest.mark.parametrize("footer", ["https://original.example.org/", "original.example.org)"])
def test_agent_verified_repost_can_feed_unapproved_draft_without_human_labels(tmp_path, footer):
    store, collection, triage, bundle = verified_case(tmp_path, footer=footer)
    result = submit_agent_verification(store, triage["run_id"], bundle, expected_head=None)
    packet = triage_packet(store, triage["run_id"])
    assert packet["counts"]["include"] == 1 and packet["unjudged"] == 1
    row = packet["rows"][0]
    assert row["human_decision"] is None and row["decision_provenance"] == "agent"
    assert row["agent_verification"]["actor_type"] == "agent"
    assert result["human_labels_created"] == 0
    assert load_triage(store, triage["run_id"])["events"][0]["reviewer_type"] == "agent"
    # Approval/send gates still apply; an agent proposal is not issue approval.
    from healthlab.provider import Completion
    from tests.test_signal import content

    response = content(bundle["assessments"][0]["decision"]["snapshot_id"])
    draft = draft_issue(
        store,
        collection["run_id"],
        FakeProvider([Completion(json.dumps(response), "fake-test", "stop", {})]),
        triage_id=triage["run_id"],
    )
    assert draft["status"] == "completed" and not draft.get("approval_sha256")
    markup = (store.root / "runs" / draft["run_id"] / "newsletter.html").read_text()
    assert "Bài gốc đã truy" in markup
    assert bundle["assessments"][0]["decision"]["original_source_url"] in markup
    with pytest.raises(ValueError, match="approval"):
        prepare_email(store, draft["run_id"], "sender@gmail.com", "recipient@example.org")


@pytest.mark.parametrize(
    "damage",
    ["quote", "url", "homepage", "shared", "event_date", "capture", "http", "input", "head"],
)
def test_agent_verification_rejects_missing_or_mismatched_evidence(tmp_path, damage):
    store, collection, triage, bundle = verified_case(tmp_path)
    bad = copy.deepcopy(bundle)
    assessment = bad["assessments"][0]
    citation = assessment["evidence"][0]
    if damage == "quote":
        citation["quote"] = "fabricated external content"
    elif damage == "url":
        citation["url"] = "https://unrelated.example.org/news"
    elif damage == "homepage":
        assessment["decision"]["original_source_url"] = "https://original.example.org/"
    elif damage == "shared":
        assessment["evidence"] = assessment["evidence"][1:]
    elif damage == "event_date":
        assessment["event_date"] = "2025-01-01"
    elif damage == "capture":
        citation["capture_run_id"] = collection["run_id"]
    elif damage == "http":
        run = store.load(citation["capture_run_id"])
        run["checkpoint"]["http_attempts"] = []
        store.save(run)
    elif damage == "input":
        bad["input_sha256"] = "0" * 64
    elif damage == "head":
        bad["base_head"] = "0" * 64
    with pytest.raises(ValueError):
        submit_agent_verification(store, triage["run_id"], bad, expected_head=None)
    assert store.load(triage["run_id"])["head"] is None


def test_agent_never_overwrites_human_and_old_event_requires_real_date(tmp_path):
    store, collection, triage, bundle = verified_case(tmp_path)
    value = bundle["assessments"][0]["decision"]
    result = submit_triage(
        store,
        triage["run_id"],
        [value],
        expected_head=None,
        reviewer="Synthetic human fixture",
        rationale="Fixture only",
    )
    bundle["base_head"] = result["head"]
    with pytest.raises(ValueError, match="human decision"):
        submit_agent_verification(store, triage["run_id"], bundle, expected_head=result["head"])
    assert triage_packet(store, triage["run_id"])["rows"][0]["decision_provenance"] == "human"


def test_event_outside_window_is_valid_distinct_from_in_window_publication(tmp_path):
    store, collection, triage, bundle = verified_case(tmp_path, event_date="2026-10-01")
    a = bundle["assessments"][0]
    a["decision"].update(
        decision="exclude", freshness="background", reason_codes=["event_outside_window"]
    )
    a["event_date"] = "2026-10-01"
    result = submit_agent_verification(store, triage["run_id"], bundle, expected_head=None)
    assert triage_packet(store, triage["run_id"])["counts"]["exclude"] == 1
    assert load_triage(store, triage["run_id"])["head"] == result["head"]


def test_saved_agent_bundle_and_raw_are_rechecked_when_reading_history(tmp_path):
    store, collection, triage, bundle = verified_case(tmp_path)
    submit_agent_verification(store, triage["run_id"], bundle, expected_head=None)
    citation = bundle["assessments"][0]["evidence"][0]
    capture = store.load(citation["capture_run_id"])
    capture["pages"][citation["url"]]["status"] = "failed"
    store.save(capture)
    with pytest.raises(ValueError, match="audited captured"):
        load_triage(store, triage["run_id"])


def test_agent_reassessment_reuses_captured_evidence_only_for_same_frozen_collection(tmp_path):
    store, collection, old, bundle = verified_case(tmp_path)
    new = prepare_triage(store, collection["run_id"])
    payload = copy.deepcopy(bundle)
    payload.update(triage_run_id=new["run_id"], input_sha256=new["input_sha256"])
    submit_agent_verification(store, new["run_id"], payload, expected_head=None)
    assert triage_packet(store, new["run_id"])["counts"]["include"] == 1
    # An identical text in another collection is not the same audited corpus.
    other = store.new("signal_collection", collection["question"])
    other.update({k: v for k, v in collection.items() if k != "run_id"})
    store.save(other)
    separate = prepare_triage(store, other["run_id"])
    payload.update(triage_run_id=separate["run_id"], input_sha256=separate["input_sha256"])
    with pytest.raises(ValueError, match="audited captured"):
        submit_agent_verification(store, separate["run_id"], payload, expected_head=None)


def test_agent_cannot_bypass_domestic_geography_gate(tmp_path):
    store, collection, triage, bundle = verified_case(tmp_path)
    bundle["assessments"][0]["decision"]["jurisdiction"] = "international"
    with pytest.raises(ValueError, match="Domestic scope"):
        submit_agent_verification(store, triage["run_id"], bundle, expected_head=None)
    assert load_triage(store, triage["run_id"])["head"] is None


def test_bare_domain_footer_supports_research_verification_without_rewriting_model_traces():
    from types import SimpleNamespace

    from healthlab.signal.triage import source_attribution
    from healthlab.signal.verification import verification_attribution

    doc = SimpleNamespace(text="Body. (Nguồn: suckhoedoisong.vn)", url="https://moh.gov.vn/article")
    assert source_attribution(doc) == []  # Legacy measured prompts/gates stay pinned.
    found = verification_attribution(doc)
    assert len(found) == 1 and found[0]["url"] == "https://suckhoedoisong.vn/"
    assert found[0]["external"] and found[0]["homepage_only"]
    assert not found[0]["original_article_verified"]
    assert doc.text[found[0]["start"] : found[0]["end"]] == found[0]["quote"]
    for text in [
        "Nguồn: just prose",
        "Discuss suckhoedoisong.vn without an attribution.",
        "Nguồn: https://suckhoedoisong.vn/article",
    ]:
        doc.text = text
        assert len(verification_attribution(doc)) == len(source_attribution(doc))
