"""Editorial gate contracts. Synthetic labels are not human quality evaluation."""

import json
from datetime import date

import pytest

from healthlab.models import IngestionError
from healthlab.provider import Completion, FakeProvider
from healthlab.signal.collection import collect, load_documents
from healthlab.signal.newsletter import (
    draft_issue,
    load_issue,
    messages,
    prepare_email,
    render_preview,
    revise,
)
from healthlab.signal.storage import CollectionStorage
from healthlab.signal.triage import (
    TriageDecision,
    draft_selection,
    load_triage,
    prepare_triage,
    submit_triage,
    triage_packet,
)
from healthlab.store import RunStore
from tests.test_signal import FEED, collected, content, human_accept, issue, registry
from tests.test_signal_html_sources import TEXT, article
from tests.test_signal_recovery import (
    DATES,
    LISTING,
    POLICY,
    MemoryObjects,
    MemoryState,
    client_for,
    ok,
)
from tests.test_signal_recovery import (
    registry as html_registry,
)


@pytest.fixture(autouse=True)
def preserve_v4_runtime_contract(monkeypatch):
    """These regression fixtures exercise historical v1–v4 behavior unchanged.

    Current v5 runtime/date/coverage gates are tested in test_signal_triage_v5.py.
    """
    monkeypatch.setattr("healthlab.signal.triage.TRIAGE_PROMPT_VERSION", "signal-triage-prompt-4")


def decision_for(store, collection, snapshot_id=None, **changes):
    snapshot_id = snapshot_id or collection["documents"][0]["snapshot_id"]
    document = load_documents(store, collection)[snapshot_id]
    quote = (
        "The report describes older adults."
        if "The report describes older adults." in document.text
        else TEXT
    )
    start = document.text.index(quote)
    value = {
        "snapshot_id": snapshot_id,
        "decision": "include",
        "topic": "aging_society",
        "freshness": "current_update",
        "reported_status": "published_report",
        "vietnam_relevance": "direct",
        "jurisdiction": "vietnam",
        "source_role": "original",
        "provenance_note": "Synthetic interpretation of framework relevance, not verified applicability",
        "event_key": "synthetic-event",
        "reason_codes": ["eligible"],
        "rationale": "Synthetic reviewed relevance for test fixture only",
        "passages": [
            {
                "start": start,
                "end": start + len(quote),
                "quote": quote,
                "purposes": ["topic", "development", "status", "vietnam_relevance"],
            }
        ],
    }
    return value | changes


def submit(store, triage_id, decisions, expected_head=None):
    return submit_triage(
        store,
        triage_id,
        decisions,
        expected_head=expected_head,
        reviewer="Synthetic reviewer",
        rationale="Fixture only, no real human labels",
    )


def test_prepare_keeps_keyword_matches_unjudged_and_draft_cannot_bypass_gate(tmp_path):
    store, collection = collected(tmp_path)
    triage = prepare_triage(store, collection["run_id"])
    packet = triage_packet(store, triage["run_id"])
    assert packet["head"] is None and packet["unjudged"] == 1
    assert packet["counts"] == {"include": 0, "exclude": 0, "needs_review": 0}
    assert packet["rows"][0]["topic_hint"]["topic"] == "aging_society"
    assert packet["rows"][0]["human_decision"] is None
    assert packet["rows"][0]["suggested_decision"] == "needs_review"
    fake = FakeProvider([])
    with pytest.raises(ValueError, match="triage"):
        draft_issue(store, collection["run_id"], fake)
    draft = draft_issue(store, collection["run_id"], fake, triage_id=triage["run_id"])
    assert draft["status"] == "completed" and fake.calls == 0
    assert not load_issue(store, draft["run_id"])[1].signals
    with pytest.raises(ValueError, match="empty"):
        human_accept(store, draft)


def test_outside_window_suggestion_is_not_a_human_exclusion_and_cannot_be_included(tmp_path):
    store, collection = collected(tmp_path)
    triage = prepare_triage(
        store, collection["run_id"], mindate=date(2026, 10, 12), maxdate=date(2026, 10, 18)
    )
    packet = triage_packet(store, triage["run_id"])
    assert not packet["collection_window_covers_monitoring"]
    assert packet["rows"][0]["suggested_decision"] == "exclude" and packet["unjudged"] == 1
    with pytest.raises(ValueError, match="publication"):
        submit(store, triage["run_id"], [decision_for(store, collection)])
    assert load_triage(store, triage["run_id"])["head"] is None


def test_missing_publication_date_stays_reviewable_and_cannot_be_included(tmp_path):
    feed = FEED.replace(b"<pubDate>Fri, 09 Oct 2026 00:00:00 GMT</pubDate>", b"")
    store, collection = collected(tmp_path, feed=feed)
    triage = prepare_triage(store, collection["run_id"])
    row = triage_packet(store, triage["run_id"])["rows"][0]
    assert row["publication_day"] is None and not row["date_eligible"]
    with pytest.raises(ValueError, match="publication"):
        submit(store, triage["run_id"], [decision_for(store, collection)])


@pytest.mark.parametrize(
    "change",
    [
        {"topic": "out_of_scope"},
        {"topic": "uncertain"},
        {"freshness": "background"},
        {"reported_status": "unknown"},
        {"vietnam_relevance": "unclear"},
        {"source_role": "unknown"},
        {"source_role": "repost"},
        {"event_key": None},
        {"reason_codes": ["status_unclear"]},
        {"rationale": "   "},
        {"passages": []},
    ],
)
def test_inconsistent_include_is_rejected_by_contract(tmp_path, change):
    store, collection = collected(tmp_path)
    with pytest.raises(ValueError):
        TriageDecision.model_validate(decision_for(store, collection, **change))


@pytest.mark.parametrize("damage", ["offset", "quote", "snapshot"])
def test_invalid_passage_or_snapshot_does_not_publish_a_decision(tmp_path, damage):
    store, collection = collected(tmp_path)
    triage = prepare_triage(store, collection["run_id"])
    value = decision_for(store, collection)
    if damage == "offset":
        value["passages"][0]["start"] += 1
    elif damage == "quote":
        value["passages"][0]["quote"] = "Invented quote"
    else:
        value["snapshot_id"] = "0" * 64
    with pytest.raises(ValueError):
        submit(store, triage["run_id"], [value])
    assert triage_packet(store, triage["run_id"])["head"] is None


def test_general_health_exclusion_needs_evidence_and_never_reaches_model(tmp_path):
    store, collection = collected(
        tmp_path, feed=FEED.replace(b"older adults", b"general hospital services")
    )
    triage = prepare_triage(store, collection["run_id"])
    document = next(iter(load_documents(store, collection).values()))
    quote = "The report describes general hospital services."
    start = document.text.index(quote)
    value = {
        "snapshot_id": collection["documents"][0]["snapshot_id"],
        "decision": "exclude",
        "topic": "out_of_scope",
        "freshness": "current_update",
        "reported_status": "not_applicable",
        "vietnam_relevance": "none",
        "source_role": "original",
        "reason_codes": ["general_health"],
        "rationale": "Synthetic general-health exclusion",
        "passages": [],
    }
    with pytest.raises(ValueError, match="passage"):
        submit(store, triage["run_id"], [value])
    value["passages"] = [
        {"start": start, "end": start + len(quote), "quote": quote, "purposes": ["exclusion"]}
    ]
    submit(store, triage["run_id"], [value])
    provider = FakeProvider([])
    draft = draft_issue(store, collection["run_id"], provider, triage_id=triage["run_id"])
    assert draft["status"] == "completed" and provider.calls == 0
    assert not draft["selected_snapshot_ids"]


def test_revisions_preserve_old_event_and_stale_submit_fails(tmp_path):
    store, collection = collected(tmp_path)
    triage = prepare_triage(store, collection["run_id"])
    value = decision_for(store, collection)
    first = submit(store, triage["run_id"], [value])
    with pytest.raises(ValueError, match="Stale"):
        submit(store, triage["run_id"], [value])
    second_value = value | {
        "decision": "needs_review",
        "reason_codes": ["status_unclear"],
        "reported_status": "unknown",
    }
    second = submit(store, triage["run_id"], [second_value], first["head"])
    view = load_triage(store, triage["run_id"])
    assert (
        len(view["events"]) == 2
        and view["decisions"][value["snapshot_id"]].decision == "needs_review"
    )
    pinned = load_triage(store, triage["run_id"], head=first["head"])
    assert pinned["decisions"][value["snapshot_id"]].decision == "include"
    assert second["previous"] == first["head"]


def test_triage_changes_block_approval_revision_and_email_while_historical_preview_survives(
    tmp_path,
):
    store, draft, value = issue(tmp_path)
    human_accept(store, draft)
    view = load_triage(store, draft["triage_run_id"])
    decision = next(iter(view["decisions"].values())).model_dump()
    decision.update(decision="needs_review", reason_codes=["development_unclear"])
    submit(store, draft["triage_run_id"], [decision], draft["triage_head"])
    assert render_preview(store, draft["run_id"]).exists()
    with pytest.raises(ValueError, match="Triage changed"):
        human_accept(store, draft)
    with pytest.raises(ValueError, match="Triage changed"):
        revise(store, draft["run_id"], value, expected_version=draft["issue_sha256"])
    with pytest.raises(ValueError, match="Triage changed"):
        prepare_email(store, draft["run_id"], "sender@gmail.com", "recipient@example.org")


def test_different_collection_and_corrupt_source_fail_before_model(tmp_path):
    store, collection = collected(tmp_path)
    triage = prepare_triage(store, collection["run_id"])
    value = decision_for(store, collection)
    submit(store, triage["run_id"], [value])
    with pytest.raises(ValueError, match="different collection"):
        draft_selection(store, "0" * 32, triage["run_id"])
    (store.root / "objects" / value["snapshot_id"]).write_bytes(b"corrupt")
    provider = FakeProvider([])
    with pytest.raises(IngestionError):
        draft_issue(store, collection["run_id"], provider, triage_id=triage["run_id"])
    assert provider.calls == 0


def test_proposed_status_is_passed_to_prompt_and_cannot_be_promoted_to_operational(tmp_path):
    store, collection = collected(tmp_path)
    triage = prepare_triage(store, collection["run_id"])
    value = decision_for(store, collection, reported_status="proposed")
    submit(store, triage["run_id"], [value])
    selected, view = draft_selection(store, collection["run_id"], triage["run_id"])
    prompt = json.loads(messages(selected, view["decisions"])[1]["content"])
    assert prompt[0]["triage"]["reported_status"] == "proposed"
    response = content(value["snapshot_id"])
    response["signals"][0]["what_happened"] = "Mô hình đã triển khai tại Việt Nam."
    fake = FakeProvider([Completion(json.dumps(response), "fake-test", "stop", {})])
    draft = draft_issue(store, collection["run_id"], fake, triage_id=triage["run_id"])
    assert draft["status"] == "failed" and not draft.get("issue_sha256")


def test_model_cannot_add_topic_or_quote_outside_reviewed_passage(tmp_path):
    store, collection = collected(tmp_path)
    triage = prepare_triage(store, collection["run_id"])
    value = decision_for(store, collection)
    submit(store, triage["run_id"], [value])
    for change in [{"topic": "longevity"}, {"supporting_passage": "Ageing report"}]:
        response = content(value["snapshot_id"])
        response["signals"][0].update(change)
        fake = FakeProvider([Completion(json.dumps(response), "fake-test", "stop", {})])
        assert (
            draft_issue(store, collection["run_id"], fake, triage_id=triage["run_id"])["status"]
            == "failed"
        )


def test_needs_review_and_unjudged_documents_are_not_in_model_input(tmp_path):
    item = FEED.split(b"<item>")[1].split(b"</item>")[0]
    second = item.replace(b"example.org/ageing", b"example.org/second").replace(
        b"Ageing report", b"Second report"
    )
    third = item.replace(b"example.org/ageing", b"example.org/third").replace(
        b"Ageing report", b"Third report"
    )
    feed = (
        b'<rss version="2.0"><channel><item>'
        + item
        + b"</item><item>"
        + second
        + b"</item><item>"
        + third
        + b"</item></channel></rss>"
    )
    store, collection = collected(tmp_path, feed=feed)
    triage = prepare_triage(store, collection["run_id"])
    first_id, second_id, third_id = [ref["snapshot_id"] for ref in collection["documents"]]
    submit(
        store,
        triage["run_id"],
        [
            decision_for(store, collection, first_id),
            decision_for(
                store,
                collection,
                second_id,
                decision="needs_review",
                reported_status="unknown",
                reason_codes=["status_unclear"],
            ),
        ],
    )
    response = content(first_id)
    provider = FakeProvider([Completion(json.dumps(response), "fake-test", "stop", {})])
    draft = draft_issue(store, collection["run_id"], provider, triage_id=triage["run_id"])
    assert draft["selected_snapshot_ids"] == [first_id] and draft["triage_unjudged"] == 1
    prompts = json.loads(store.read(draft["input_sha256"]))
    assert second_id not in json.dumps(prompts) and third_id not in json.dumps(prompts)


@pytest.mark.parametrize("same_text", [True, False])
def test_duplicate_text_or_event_cannot_have_two_included_representatives(tmp_path, same_text):
    item = FEED.split(b"<item>")[1].split(b"</item>")[0]
    second = item.replace(b"example.org/ageing", b"example.org/second")
    if not same_text:
        second = second.replace(b"Ageing report", b"Second report")
    feed = (
        b'<rss version="2.0"><channel><item>'
        + item
        + b"</item><item>"
        + second
        + b"</item></channel></rss>"
    )
    store, collection = collected(tmp_path, feed=feed)
    triage = prepare_triage(store, collection["run_id"])
    first_id, second_id = [ref["snapshot_id"] for ref in collection["documents"]]
    with pytest.raises(ValueError, match="representative"):
        submit(
            store,
            triage["run_id"],
            [
                decision_for(store, collection, first_id),
                decision_for(
                    store,
                    collection,
                    second_id,
                    event_key="different-event" if same_text else "synthetic-event",
                ),
            ],
        )
    assert load_triage(store, triage["run_id"])["head"] is None


def test_non_filesystem_storage_supports_triage_prepare_submit_and_selection():
    storage = CollectionStorage(MemoryObjects(), MemoryState())
    with client_for(lambda r: ok(LISTING if r.url.path == "/" else article())) as client:
        collection = collect(
            storage, html_registry(), **DATES, rss_client=client, fetch_policy=POLICY
        )
    triage = prepare_triage(storage, collection["run_id"])
    value = decision_for(storage, collection)
    submit(storage, triage["run_id"], [value])
    selected, _ = draft_selection(storage, collection["run_id"], triage["run_id"])
    assert list(selected) == [value["snapshot_id"]]


def test_packet_render_and_cli_preserve_null_labels_and_no_credentials_needed(tmp_path, capsys):
    from healthlab.signal.__main__ import main

    store, collection = collected(tmp_path)
    common = ["--store", str(store.root)]
    assert main([*common, "triage", "prepare", collection["run_id"]]) == 0
    result = json.loads(capsys.readouterr().out)
    triage_id = result["triage_run_id"]
    assert main([*common, "triage", "packet", triage_id, "--html"]) == 0
    preview = json.loads(capsys.readouterr().out)["preview"]
    assert preview.endswith("triage.html")
    assert triage_packet(store, triage_id)["unjudged"] == 1
    assert main([*common, "draft", collection["run_id"], "--triage-run", triage_id]) == 0
    empty = json.loads(capsys.readouterr().out)
    assert empty["status"] == "completed" and not store.load(empty["run_id"])["requests"]


def test_required_source_partial_still_blocks_approval_after_triage(tmp_path):
    store = RunStore(tmp_path)
    config = registry()
    config.sources.append(
        config.sources[0].model_copy(
            update={"source_id": "missing", "url": "https://example.org/missing"}
        )
    )
    import httpx

    with client_for(
        lambda r: (
            httpx.Response(200, content=FEED) if r.url.path == "/feed" else httpx.Response(404)
        )
    ) as client:
        collection = collect(store, config, **DATES, rss_client=client)
    assert collection["status"] == "partial"
    triage = prepare_triage(store, collection["run_id"])
    value = decision_for(store, collection)
    submit(store, triage["run_id"], [value])
    fake = FakeProvider(
        [Completion(json.dumps(content(value["snapshot_id"])), "fake-test", "stop", {})]
    )
    draft = draft_issue(store, collection["run_id"], fake, triage_id=triage["run_id"])
    assert draft["status"] == "completed"
    with pytest.raises(ValueError, match="coverage"):
        human_accept(store, draft)


def test_html_include_requires_saved_date_evidence_not_just_snapshot_date(tmp_path):
    store = RunStore(tmp_path)
    with client_for(lambda r: ok(LISTING if r.url.path == "/" else article())) as client:
        collection = collect(
            store, html_registry(), **DATES, rss_client=client, fetch_policy=POLICY
        )
    from healthlab.store import json_bytes

    check = collection["source_checks"][0]["article_checks"][0]
    extraction = json.loads(store.read(check["extraction_sha256"]))
    extraction["publication_date_evidence"] = []
    check["extraction_sha256"] = store.put(json_bytes(extraction))
    store.save(collection)
    triage = prepare_triage(store, collection["run_id"])
    value = decision_for(store, collection)
    with pytest.raises(ValueError, match="publication"):
        submit(store, triage["run_id"], [value])


def test_legacy_issue_can_render_but_cannot_be_newly_approved_without_triage(tmp_path):
    store, draft, _ = issue(tmp_path)
    historical = store.load(draft["run_id"])
    historical["prompt_version"] = "signal-draft-1"
    for key in [key for key in historical if key.startswith("triage_")]:
        historical.pop(key)
    store.save(historical)
    assert render_preview(store, historical["run_id"]).exists()
    with pytest.raises(ValueError, match="Triage is required"):
        human_accept(store, historical)


def test_adding_decision_during_generation_prevents_publishing_stale_draft(tmp_path):
    store, collection = collected(tmp_path)
    triage = prepare_triage(store, collection["run_id"])
    value = decision_for(store, collection)
    first = submit(store, triage["run_id"], [value])

    class ChangingProvider(FakeProvider):
        def complete(self, messages, audit):
            submit(
                store,
                triage["run_id"],
                [value | {"decision": "needs_review", "reason_codes": ["development_unclear"]}],
                first["head"],
            )
            return super().complete(messages, audit)

    fake = ChangingProvider(
        [Completion(json.dumps(content(value["snapshot_id"])), "fake-test", "stop", {})]
    )
    draft = draft_issue(store, collection["run_id"], fake, triage_id=triage["run_id"])
    assert draft["status"] == "failed" and not draft.get("issue_sha256")


def citation_completion(value, document):
    from healthlab.signal.triage import source_passages

    payload = dict(value)
    payload["original_source_url"] = payload.get("original_source_url")
    payload.update(
        development_summary="Synthetic new report for fixture only",
        freshness_basis="new_report",
        freshness_explanation="Synthetic report release, not a human label",
        event_date=None,
        event_date_passage_id=None,
        event_date_quote=None,
    )
    units = source_passages(document)
    payload["passages"] = [
        {
            "passage_id": next(p["passage_id"] for p in units if item["quote"] in p["quote"]),
            "purposes": item["purposes"],
        }
        for item in value["passages"]
    ]
    return Completion(json.dumps(payload), "fake-test", "stop", {"total_tokens": 10})


def test_model_include_can_feed_unapproved_draft_but_never_creates_human_labels(tmp_path):
    from healthlab.signal.triage import propose_triage

    store, collection = collected(tmp_path)
    triage = prepare_triage(store, collection["run_id"])
    document = next(iter(load_documents(store, collection).values()))
    value = decision_for(store, collection, original_source_url=document.url)
    provider = FakeProvider([citation_completion(value, document)])
    result = propose_triage(store, triage["run_id"], provider, expected_head=None)
    assert result["status"] == "completed" and result["human_labels_created"] == 0
    packet = triage_packet(store, triage["run_id"])
    assert (
        packet["counts"]["include"] == 1 and packet["unjudged"] == 1 and packet["unresolved"] == 0
    )
    assert packet["rows"][0]["human_decision"] is None
    assert packet["rows"][0]["decision_provenance"] == "model"
    response = content(value["snapshot_id"])
    draft_provider = FakeProvider([Completion(json.dumps(response), "fake-test", "stop", {})])
    draft = draft_issue(store, collection["run_id"], draft_provider, triage_id=triage["run_id"])
    assert draft["status"] == "completed" and not draft.get("approval_sha256")
    assert draft["triage_unjudged"] == 1
    with pytest.raises(ValueError, match="approval"):
        prepare_email(store, draft["run_id"], "sender@gmail.com", "recipient@example.org")
    event = load_triage(store, triage["run_id"])["events"][0]
    assert json.loads(store.read(event["model_response_sha256"]))["actual_model"] == "fake-test"
    assert store.read(event["prompt_sha256"])


def test_model_never_overrides_human_decisions_or_repeats_an_attempt(tmp_path):
    from healthlab.signal.triage import propose_triage

    store, collection = collected(tmp_path)
    triage = prepare_triage(store, collection["run_id"])
    value = decision_for(store, collection)
    event = submit(store, triage["run_id"], [value])
    provider = FakeProvider([])
    result = propose_triage(store, triage["run_id"], provider, expected_head=event["head"])
    assert provider.calls == 0 and result["head"] == event["head"]
    other = prepare_triage(store, collection["run_id"])
    bad = FakeProvider([Completion('{"bad":true}', "fake-test", "stop", {})])
    failed = propose_triage(store, other["run_id"], bad, expected_head=None)
    assert failed["status"] == "partial" and failed["head"] is None
    assert propose_triage(store, other["run_id"], bad, expected_head=None)["head"] is None
    assert bad.calls == 1
    assert triage_packet(store, other["run_id"])["unresolved"] == 1


def test_model_triage_skips_out_of_window_without_api_or_credentials(tmp_path):
    from healthlab.signal.triage import propose_triage

    store, collection = collected(tmp_path)
    triage = prepare_triage(
        store, collection["run_id"], mindate=date(2026, 10, 12), maxdate=date(2026, 10, 18)
    )
    result = propose_triage(store, triage["run_id"], None, expected_head=None)
    assert not result["model_attempts"] and result["head"] is None


def test_human_resolution_of_ai_proposal_is_separate_and_preserves_ai_event(tmp_path):
    from healthlab.signal.triage import propose_triage

    store, collection = collected(tmp_path)
    triage = prepare_triage(store, collection["run_id"])
    value = decision_for(
        store,
        collection,
        decision="needs_review",
        reported_status="unknown",
        reason_codes=["status_unclear"],
    )
    fake = FakeProvider(
        [citation_completion(value, next(iter(load_documents(store, collection).values())))]
    )
    proposed = propose_triage(store, triage["run_id"], fake, expected_head=None)
    assert triage_packet(store, triage["run_id"])["unjudged"] == 1
    resolved = submit(store, triage["run_id"], [decision_for(store, collection)], proposed["head"])
    packet = triage_packet(store, triage["run_id"])
    assert packet["unjudged"] == 0 and packet["rows"][0]["decision_provenance"] == "human"
    assert [e["reviewer_type"] for e in packet["history"]] == ["model", "human"]
    assert resolved["previous"] == proposed["head"]


def test_model_reference_schema_requires_event_and_passages_and_rejects_unknown_ids(tmp_path):
    from healthlab.signal.triage import CitationTriageDecision, resolve_model_decision

    store, collection = collected(tmp_path)
    document = next(iter(load_documents(store, collection).values()))
    value = decision_for(store, collection)
    payload = json.loads(citation_completion(value, document).content)
    for key in (
        "development_summary",
        "freshness_basis",
        "freshness_explanation",
        "event_date",
        "event_date_passage_id",
        "event_date_quote",
    ):
        payload.pop(key)
    assert {"event_key", "passages"} <= set(CitationTriageDecision.model_json_schema()["required"])
    for bad in [
        {key: val for key, val in payload.items() if key != "passages"},
        payload
        | {"passages": [{"passage_id": "P999", "purposes": ["topic", "development", "status"]}]},
    ]:
        with pytest.raises(ValueError):
            resolve_model_decision(
                json.dumps(bad), document, prompt_version="signal-triage-prompt-2"
            )


def test_citation_resolution_preserves_repeated_unicode_sentence_offsets(tmp_path):
    from healthlab.signal.triage import resolve_model_decision, source_passages

    store, collection = collected(tmp_path)
    document = next(iter(load_documents(store, collection).values()))
    sentence = "Chăm sóc người cao tuổi tại cộng đồng."
    document.text = sentence + " " + sentence
    units = source_passages(document)
    value = decision_for(store, collection)
    payload = value | {
        "original_source_url": None,
        "passages": [
            {"passage_id": units[1]["passage_id"], "purposes": ["topic", "development", "status"]}
        ],
    }
    decision = resolve_model_decision(
        json.dumps(payload), document, prompt_version="signal-triage-prompt-2"
    )
    assert decision.passages[0].quote == sentence
    assert decision.passages[0].start == len(sentence) + 1
    assert document.text[decision.passages[0].start : decision.passages[0].end] == sentence
    assert (
        resolve_model_decision(
            json.dumps(value),
            next(iter(load_documents(store, collection).values())),
            prompt_version="signal-triage-prompt-1",
        ).snapshot_id
        == value["snapshot_id"]
    )


def test_source_passages_bound_long_sentences_without_changing_text(tmp_path):
    from healthlab.signal.triage import source_passages

    store, collection = collected(tmp_path)
    document = next(iter(load_documents(store, collection).values()))
    document.text = "A" * 5000
    units = source_passages(document)
    assert "".join(p["quote"] for p in units) == document.text
    assert all(
        len(p["quote"]) <= 1800 and document.text[p["start"] : p["end"]] == p["quote"]
        for p in units
    )


def test_model_original_claim_cannot_override_explicit_external_footer(tmp_path):
    from healthlab.signal.triage import validate_model_origin

    store, collection = collected(tmp_path)
    document = next(iter(load_documents(store, collection).values()))
    decision = TriageDecision.model_validate(
        decision_for(store, collection, original_source_url=document.url)
    )
    document.text += "\nNguồn: https://original.example.org/"
    with pytest.raises(ValueError, match="External source attribution"):
        validate_model_origin(decision, document)


def test_v3_gates_repost_recommendation_without_losing_raw_proposal_or_history(tmp_path):
    from healthlab.signal.triage import propose_triage

    store, collection = collected(tmp_path)
    triage = prepare_triage(store, collection["run_id"])
    document = next(iter(load_documents(store, collection).values()))
    value = decision_for(store, collection, source_role="repost", original_source_url=None)
    provider = FakeProvider([citation_completion(value, document)])
    result = propose_triage(store, triage["run_id"], provider, expected_head=None)
    assert result["status"] == "completed" and provider.calls == 1
    packet = triage_packet(store, triage["run_id"])
    row = packet["rows"][0]
    assert row["effective_decision"]["decision"] == "needs_review"
    assert row["effective_decision"]["reason_codes"] == ["repost_unverified"]
    assert row["model_assessment"]["proposal"]["decision"] == "include"
    assert row["human_decision"] is None
    selected, _ = draft_selection(store, collection["run_id"], triage["run_id"])
    assert not selected
    assert load_triage(store, triage["run_id"])["head"] == result["head"]


def test_v3_external_homepage_footer_gates_original_claim_with_exact_evidence(tmp_path):
    from healthlab.signal.triage import resolve_model_decision, source_attribution

    store, collection = collected(tmp_path)
    document = next(iter(load_documents(store, collection).values()))
    value = decision_for(store, collection, original_source_url=document.url)
    document.text += "\nNguồn: https://original.example.org/ — Biên tập viên"
    payload = citation_completion(value, document).content
    decision = resolve_model_decision(
        payload,
        document,
        prompt_version="signal-triage-prompt-3",
        monitoring_window=collection["question"],
    )
    assert decision.decision == "needs_review" and decision.source_role == "repost"
    assert decision.original_source_url is None
    evidence = source_attribution(document)[0]
    assert evidence["homepage_only"] and not evidence["original_article_verified"]
    assert document.text[evidence["start"] : evidence["end"]] == evidence["quote"]


def test_v3_old_event_and_background_cannot_pass_on_recent_publication(tmp_path):
    from healthlab.signal.triage import resolve_model_decision, source_passages

    store, collection = collected(tmp_path)
    document = next(iter(load_documents(store, collection).values()))
    value = decision_for(store, collection, original_source_url=document.url)
    document.text += " Event occurred on 2025-01-02."
    payload = json.loads(citation_completion(value, document).content)
    unit = next(p for p in source_passages(document) if "2025-01-02" in p["quote"])
    payload.update(
        freshness_basis="dated_event",
        event_date="2025-01-02",
        event_date_passage_id=unit["passage_id"],
        event_date_quote="2025-01-02",
    )

    def resolve(value):
        return resolve_model_decision(
            json.dumps(value),
            document,
            prompt_version="signal-triage-prompt-3",
            monitoring_window=collection["question"],
        )

    assert resolve(payload).reason_codes == ["event_outside_window"]
    # A stale event is distinct from an article published outside the window.
    # The effective needs_review decision must survive the persisted event gate.
    from healthlab.signal.triage import _validate_decision

    prepared = prepare_triage(store, collection["run_id"])
    view = load_triage(store, prepared["run_id"])
    _validate_decision(resolve(payload), {value["snapshot_id"]: document}, view["proposals"])
    with pytest.raises(ValueError, match="quote differs"):
        resolve(payload | {"event_date_quote": "invented date"})
    with pytest.raises(ValueError, match="Unknown event date"):
        resolve(payload | {"event_date_passage_id": "P999"})
    background = payload | {
        "freshness_basis": "background_only",
        "event_date": None,
        "event_date_passage_id": None,
        "event_date_quote": None,
    }
    assert resolve(background).decision == "needs_review"
    with pytest.raises(ValueError, match="supplied together"):
        resolve(payload | {"event_date": None})


def test_v2_citation_output_remains_readable_without_v3_assessment_fields(tmp_path):
    from healthlab.signal.triage import resolve_model_decision

    store, collection = collected(tmp_path)
    document = next(iter(load_documents(store, collection).values()))
    value = decision_for(store, collection, original_source_url=document.url)
    payload = json.loads(citation_completion(value, document).content)
    for key in (
        "development_summary",
        "freshness_basis",
        "freshness_explanation",
        "event_date",
        "event_date_passage_id",
        "event_date_quote",
    ):
        payload.pop(key)
    decision = resolve_model_decision(
        json.dumps(payload), document, prompt_version="signal-triage-prompt-2"
    )
    assert decision.decision == "include"


def test_saved_invalid_model_output_can_recover_offline_without_overwriting_history(
    tmp_path, monkeypatch
):
    import healthlab.signal.triage as triage_module

    store, collection = collected(tmp_path)
    triage = prepare_triage(store, collection["run_id"])
    document = next(iter(load_documents(store, collection).values()))
    value = decision_for(store, collection, original_source_url=document.url)
    provider = FakeProvider([citation_completion(value, document)])
    original = triage_module._validate_decision

    def invalid(*args, **kwargs):
        raise ValueError("Synthetic validator defect")

    monkeypatch.setattr(triage_module, "_validate_decision", invalid)
    triage_module.propose_triage(store, triage["run_id"], provider, expected_head=None)
    before = store.load(triage["run_id"])["model_attempts"][0]
    monkeypatch.setattr(triage_module, "_validate_decision", original)
    result = triage_module.revalidate_model_attempt(
        store,
        triage["run_id"],
        value["snapshot_id"],
        expected_head=None,
        prompt_version="signal-triage-prompt-4",
    )
    assert result["model_calls"] == 0 and provider.calls == 1
    packet = triage_packet(store, triage["run_id"])
    attempt = packet["model_attempts"][0]
    assert {k: v for k, v in attempt.items() if k != "revalidations"} == before
    assert attempt["status"] == "failed"
    assert attempt["revalidations"][0]["event_sha256"] == packet["head"]
    assert packet["rows"][0]["human_decision"] is None
    assert packet["rows"][0]["effective_decision"]["decision"] == "include"
    with pytest.raises(ValueError, match="Stale"):
        triage_module.revalidate_model_attempt(
            store,
            triage["run_id"],
            value["snapshot_id"],
            expected_head=None,
            prompt_version="signal-triage-prompt-4",
        )
    with pytest.raises(ValueError, match="cannot be overwritten"):
        triage_module.revalidate_model_attempt(
            store,
            triage["run_id"],
            value["snapshot_id"],
            expected_head=result["head"],
            prompt_version="signal-triage-prompt-4",
        )


def test_revalidation_never_repairs_fabricated_citations_or_changes_prompt_version(tmp_path):
    from healthlab.signal.triage import propose_triage, revalidate_model_attempt

    store, collection = collected(tmp_path)
    triage = prepare_triage(store, collection["run_id"])
    document = next(iter(load_documents(store, collection).values()))
    value = decision_for(store, collection, original_source_url=document.url)
    payload = json.loads(citation_completion(value, document).content)
    payload["passages"] = [{"passage_id": "P999", "purposes": ["topic", "development", "status"]}]
    fake = FakeProvider([Completion(json.dumps(payload), "fake-test", "stop", {})])
    propose_triage(store, triage["run_id"], fake, expected_head=None)
    with pytest.raises(ValueError, match="mismatched"):
        revalidate_model_attempt(
            store,
            triage["run_id"],
            value["snapshot_id"],
            expected_head=None,
            prompt_version="signal-triage-prompt-2",
        )
    with pytest.raises(ValueError, match="Unknown or duplicate"):
        revalidate_model_attempt(
            store,
            triage["run_id"],
            value["snapshot_id"],
            expected_head=None,
            prompt_version="signal-triage-prompt-4",
        )
    run = store.load(triage["run_id"])
    assert run["head"] is None and fake.calls == 1
    assert run["model_attempts"][0]["revalidations"][0]["status"] == "failed"


def test_domestic_rubric_blocks_foreign_policy_for_all_submitters_and_model_gate(tmp_path):
    from healthlab.signal.triage import propose_triage, resolve_model_decision

    store, collection = collected(tmp_path)
    triage = prepare_triage(store, collection["run_id"])
    doc = next(iter(load_documents(store, collection).values()))
    # Same source host and even a mistaken direct-Vietnam relevance claim cannot
    # override explicit international jurisdiction in the domestic rubric.
    value = decision_for(
        store, collection, jurisdiction="international", original_source_url=doc.url
    )
    with pytest.raises(ValueError, match="Domestic scope"):
        submit(store, triage["run_id"], [value])
    completion = citation_completion(value, doc)
    resolved = resolve_model_decision(
        completion.content,
        doc,
        prompt_version="signal-triage-prompt-4",
        monitoring_window=collection["question"],
    )
    assert resolved.decision == "exclude" and resolved.reason_codes == ["geography_out_of_scope"]
    fake = FakeProvider([completion])
    propose_triage(store, triage["run_id"], fake, expected_head=None)
    packet = triage_packet(store, triage["run_id"])
    assert packet["counts"] == {"include": 0, "exclude": 1, "needs_review": 0}
    assert packet["rubric"]["scope"] == "vietnam_domestic_only"


@pytest.mark.parametrize(
    "changes",
    [
        {"jurisdiction": None},
        {"jurisdiction": "unclear"},
        {"vietnam_relevance": "transferable_framework"},
        {"passages": None},
    ],
)
def test_domestic_include_requires_explicit_geography_and_support(tmp_path, changes):
    store, collection = collected(tmp_path)
    triage = prepare_triage(store, collection["run_id"])
    value = decision_for(store, collection)
    if changes.get("passages", "not_missing") is None:
        value["passages"][0]["purposes"] = ["topic", "development", "status"]
    else:
        value.update(changes)
    with pytest.raises(ValueError, match="Domestic scope"):
        submit(store, triage["run_id"], [value])


def test_v4_requires_geography_but_v3_keeps_its_historical_shape(tmp_path):
    from healthlab.signal.triage import resolve_model_decision

    store, collection = collected(tmp_path)
    doc = next(iter(load_documents(store, collection).values()))
    payload = json.loads(citation_completion(decision_for(store, collection), doc).content)
    payload.pop("jurisdiction")
    with pytest.raises(ValueError, match="jurisdiction"):
        resolve_model_decision(
            json.dumps(payload),
            doc,
            prompt_version="signal-triage-prompt-4",
            monitoring_window=collection["question"],
        )
    old = resolve_model_decision(
        json.dumps(payload),
        doc,
        prompt_version="signal-triage-prompt-3",
        monitoring_window=collection["question"],
    )
    assert old.decision == "needs_review"  # missing original_source_url remains held by v3
    assert "jurisdiction" not in old.model_dump()


def test_legacy_rubric_preview_survives_but_cannot_create_current_issue(tmp_path):
    from healthlab.signal.triage import RUBRIC_V1
    from healthlab.store import json_bytes

    store, collection = collected(tmp_path)
    triage = prepare_triage(store, collection["run_id"])
    # Synthetic historical input: original source/corpus is unchanged, only the
    # previously supported rubric is used before any decisions are appended.
    inputs = json.loads(store.read(triage["input_sha256"]))
    inputs["rubric_sha256"] = store.put(json_bytes(RUBRIC_V1))
    triage["input_sha256"] = store.put(json_bytes(inputs))
    store.save(triage)
    value = decision_for(
        store, collection, vietnam_relevance="transferable_framework", jurisdiction=None
    )
    submit(store, triage["run_id"], [value])
    assert triage_packet(store, triage["run_id"])["rubric"] == RUBRIC_V1
    fake = FakeProvider([])
    with pytest.raises(ValueError, match="Current rubric"):
        draft_issue(store, collection["run_id"], fake, triage_id=triage["run_id"])
    assert fake.calls == 0
