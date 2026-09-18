import json

import httpx
import pytest

from healthlab.config import FPTSettings
from healthlab.evidence import build_messages, parse_content, verify_passages
from healthlab.extraction import cache_identity, extract_run
from healthlab.models import IngestionError, Question
from healthlab.pipeline import ingest
from healthlab.provider import Completion, FakeProvider, FPTProvider
from healthlab.pubmed import parse_records
from healthlab.store import RunStore
from tests.test_ingestion import XML, connector, normal_handler


def evidence(**updates):
    result = {
        "study_type": "randomized controlled trial",
        "population": None,
        "sample_sizes": [
            {
                "value": 60,
                "role": "randomized",
                "group": None,
                "supporting_passage": "Sixty participants were randomized.",
                "section_index": 1,
            }
        ],
        "main_finding": "Sixty participants were randomized.",
        "supporting_passage": "Sixty participants were randomized.",
        "section_index": 1,
        "author_limitations": [],
    }
    return result | updates


def completion(value=None, **kwargs):
    return Completion(
        json.dumps(value if value is not None else evidence()),
        "fake-test",
        kwargs.get("finish_reason", "stop"),
        {"total_tokens": 10},
    )


def source_run(tmp_path):
    store = RunStore(tmp_path)
    client, _ = connector(normal_handler)
    parent = ingest(store, Question(raw_question="q", query="q"), client)
    return store, parent


def second_completion():
    return completion(
        evidence(
            sample_sizes=[],
            main_finding="One full paragraph.",
            supporting_passage="One full paragraph.",
            section_index=0,
        )
    )


def test_extraction_cache_provenance_and_no_network_on_hit(tmp_path):
    store, parent = source_run(tmp_path)
    provider = FakeProvider([completion(), second_completion()])
    first = extract_run(store, parent["run_id"], provider)
    assert first["status"] == "completed" and provider.calls == 2
    assert first["counts"]["validated_structure"] == 2 and first["counts"]["skipped"] == 1
    item = json.loads(store.read(first["results"][0]["evidence_item_id"]))
    assert item["semantic_support"] == "not_evaluated"
    assert item["source_limitations"] == ["abstract_only"]
    assert item["evidence"]["author_limitations"] == []
    doc = json.loads(store.read(item["snapshot_id"]))["document"]
    for p in item["passages"]:
        assert (
            doc["abstract_sections"][p["section_index"]]["text"][p["start"] : p["end"]] == p["text"]
        )
    no_calls = FakeProvider([])
    second = extract_run(store, parent["run_id"], no_calls)
    assert no_calls.calls == 0 and second["counts"]["cache_hits"] == 2
    assert second["results"][0]["evidence_item_id"] == first["results"][0]["evidence_item_id"]
    assert second["requests"] == []
    assert store.load(parent["run_id"])["mode"] == "online"


@pytest.mark.parametrize(
    "value,code",
    [
        (
            evidence(
                sample_sizes=[
                    {
                        "value": "60",
                        "role": "randomized",
                        "group": None,
                        "supporting_passage": "Sixty participants were randomized.",
                        "section_index": 1,
                    }
                ]
            ),
            "schema_invalid",
        ),
        (evidence(main_finding=123), "schema_invalid"),
        (evidence(section_index=True), "schema_invalid"),
        (evidence(unexpected="field"), "schema_invalid"),
        (evidence(supporting_passage=None), "schema_invalid"),
        ({"study_type": None}, "schema_invalid"),
        ([], "schema_invalid"),
    ],
)
def test_schema_rejects_missing_wrong_types_and_inconsistent_references(value, code):
    with pytest.raises(IngestionError) as exc:
        parse_content(json.dumps(value))
    assert exc.value.code == code


@pytest.mark.parametrize("text", ['{"study_type":', '{"x":1,"x":2}', '{"x":NaN}', "```json\n{}"])
def test_malformed_json_not_repaired(text):
    with pytest.raises(IngestionError) as exc:
        parse_content(text)
    assert exc.value.code == "invalid_json"


def test_missing_source_fields_are_null_or_empty_and_fences_parse_once():
    value = evidence(
        study_type=None,
        population=None,
        sample_sizes=[],
        main_finding=None,
        supporting_passage=None,
        section_index=None,
    )
    parsed = parse_content("```json\n" + json.dumps(value) + "\n```")
    assert parsed.population is None and parsed.sample_sizes == []
    assert verify_passages(parsed, parse_records(XML)[0][0], "snapshot") == []


@pytest.mark.parametrize(
    "updates",
    [
        {"supporting_passage": "Sixty ..."},
        {"section_index": 0},
        {"section_index": 999},
        {"supporting_passage": " "},
    ],
)
def test_wrong_or_wrong_section_passage_rejected(updates):
    parsed = parse_content(json.dumps(evidence(**updates)))
    with pytest.raises(IngestionError) as exc:
        verify_passages(parsed, parse_records(XML)[0][0], "snapshot")
    assert exc.value.code == "passage_not_found"


def test_citation_present_but_finding_unsupported_never_claims_semantic_pass(tmp_path):
    store, parent = source_run(tmp_path)
    fake = FakeProvider([completion(evidence(main_finding="This treatment cures every disease."))])
    run = extract_run(store, parent["run_id"], fake, max_documents=1)
    result = run["results"][0]
    assert result["schema_valid"] and result["passage_match"]
    assert result["semantic_support"] == "not_evaluated"
    assert run["status"] == "partial"  # Remaining eligible record intentionally not processed.


def test_failed_extraction_not_cached_and_other_documents_continue(tmp_path):
    store, parent = source_run(tmp_path)
    fake = FakeProvider([completion(evidence(supporting_passage="invented")), second_completion()])
    first = extract_run(store, parent["run_id"], fake)
    assert first["status"] == "partial" and first["counts"]["failed"] == 1
    assert first["results"][0]["passage_match"] is False
    assert "evidence_item_id" not in first["results"][0]
    fake = FakeProvider([completion()])
    retry = extract_run(store, parent["run_id"], fake)
    assert fake.calls == 1 and retry["status"] == "completed"
    assert retry["counts"]["cache_hits"] == 1


def test_length_finish_rejected_even_with_valid_json(tmp_path):
    store, parent = source_run(tmp_path)
    run = extract_run(
        store, parent["run_id"], FakeProvider([completion(finish_reason="length")]), max_documents=1
    )
    entry = run["results"][0]
    assert entry["status"] == "failed" and entry["error"]["code"] == "output_truncated"
    assert store.read(entry["content_sha256"])
    assert entry["schema_valid"] is None


def test_cache_changes_with_every_material_input(monkeypatch):
    import healthlab.extraction as module

    doc = parse_records(XML)[0][0]
    messages = build_messages(doc)
    config = FakeProvider([]).identity()
    key = cache_identity("snapshot", messages, config)[0]
    assert cache_identity("changed", messages, config)[0] != key
    assert cache_identity("snapshot", messages, config | {"model": "new"})[0] != key
    assert cache_identity("snapshot", messages, config | {"max_tokens": 123})[0] != key
    assert (
        cache_identity("snapshot", messages + [{"role": "user", "content": "changed"}], config)[0]
        != key
    )
    monkeypatch.setattr(module, "PROMPT_VERSION", "new-version")
    assert cache_identity("snapshot", messages, config)[0] != key


def test_refresh_bypasses_valid_cache(tmp_path):
    store, parent = source_run(tmp_path)
    extract_run(store, parent["run_id"], FakeProvider([completion(), second_completion()]))
    fake = FakeProvider([completion(), second_completion()])
    run = extract_run(store, parent["run_id"], fake, refresh=True)
    assert fake.calls == 2 and run["counts"]["cache_hits"] == 0


def fpt(handler):
    settings = FPTSettings(
        _env_file=None,
        api_key="fpt-test-secret",
        base_url="https://fpt.test/v1",
        model="fixture-model",
    )
    return FPTProvider(settings, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_fpt_wire_contract_and_recorded_actual_model():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "model": "actual-model-version",
                "usage": {"total_tokens": 20},
                "choices": [
                    {"finish_reason": "stop", "message": {"content": json.dumps(evidence())}}
                ],
            },
        )

    provider = fpt(handler)
    events = []
    result = provider.complete(
        [{"role": "user", "content": "abstract"}], lambda e, b: events.append((e, b))
    )
    assert seen[0].headers["Authorization"] == "Bearer fpt-test-secret"
    payload = json.loads(seen[0].content)
    assert payload["model"] == "fixture-model" and payload["max_tokens"] == 4096
    assert "response_format" not in payload
    assert result.actual_model == "actual-model-version" and result.usage["total_tokens"] == 20
    assert "secret" not in json.dumps(provider.identity())
    assert events[0][0]["http_status"] == 200


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "provider_auth"),
        (403, "provider_auth"),
        (429, "provider_quota_or_rate"),
        (500, "provider_http"),
    ],
)
def test_fpt_http_errors_are_explicit_and_redacted(status, code):
    provider = fpt(lambda req: httpx.Response(status, content=b"fpt-test-secret"))
    events = []
    with pytest.raises(IngestionError) as exc:
        provider.complete([], lambda e, b: events.append((e, b)))
    assert exc.value.code == code and len(events) == 1
    assert b"fpt-test-secret" not in events[0][1] and events[0][0]["body_redacted"]


def test_fpt_timeout_is_not_parse_error():
    def handler(request):
        raise httpx.ReadTimeout("do not expose fpt-test-secret", request=request)

    provider = fpt(handler)
    events = []
    with pytest.raises(IngestionError) as exc:
        provider.complete([], lambda e, b: events.append((e, b)))
    assert exc.value.code == "provider_timeout" and "secret" not in str(exc.value)
    assert events[0][1] is None


@pytest.mark.parametrize("body", [b"not json", b'{"choices":[]}', b'{"error":"quota"}'])
def test_fpt_invalid_envelope_retained(body):
    provider = fpt(lambda req: httpx.Response(200, content=body))
    events = []
    with pytest.raises(IngestionError) as exc:
        provider.complete([], lambda e, b: events.append((e, b)))
    assert exc.value.code == "provider_envelope" and events[0][1] == body


def test_author_limitations_need_their_own_passage():
    parsed = parse_content(
        json.dumps(
            evidence(
                author_limitations=[
                    {"text": "Too small", "supporting_passage": "Not in source", "section_index": 0}
                ]
            )
        )
    )
    with pytest.raises(IngestionError, match="author_limitations"):
        verify_passages(parsed, parse_records(XML)[0][0], "snapshot")


def test_cache_corruption_does_not_trigger_hidden_paid_call(tmp_path):
    from healthlab.extraction import cache_path

    store, parent = source_run(tmp_path)
    run = extract_run(store, parent["run_id"], FakeProvider([completion()]), max_documents=1)
    cache_path(store, run["results"][0]["cache_key"]).write_text('{"result_sha256":"invalid"}')
    fake = FakeProvider([])
    result = extract_run(store, parent["run_id"], fake, max_documents=1)
    assert result["status"] == "failed" and fake.calls == 0


def test_cache_only_provider_never_constructs_http_client(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network client must not be created")

    monkeypatch.setattr(httpx.Client, "__init__", forbidden)
    settings = FPTSettings(_env_file=None, api_key="")
    provider = FPTProvider(settings, cache_only=True)
    with pytest.raises(IngestionError) as exc:
        provider.complete([], lambda *args: None)
    assert exc.value.code == "cache_miss"
    provider.close()


def test_provider_failure_is_visible_in_run_summary(tmp_path):
    store, parent = source_run(tmp_path)
    run = extract_run(
        store,
        parent["run_id"],
        FakeProvider([IngestionError("provider_timeout", "Timed out")]),
        max_documents=1,
    )
    assert run["errors"][0]["code"] == "provider_timeout"
    assert run["warnings"] and run["status"] == "failed"
