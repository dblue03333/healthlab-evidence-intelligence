import json
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from healthlab.__main__ import main
from healthlab.config import Settings
from healthlab.models import IngestionError, Question
from healthlab.pipeline import import_probe, ingest, replay
from healthlab.pubmed import PubMedClient, parse_records, parse_search
from healthlab.store import RunStore

XML = (Path(__file__).parent / "fixtures/pubmed/records.xml").read_bytes()


def search_bytes(ids=None, count=116):
    return json.dumps(
        {
            "esearchresult": {
                "count": str(count),
                "idlist": ids if ids is not None else ["101", "102", "103"],
                "querytranslation": "fixture query",
            }
        }
    ).encode()


class Clock:
    def __init__(self):
        self.now = 0
        self.waits = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def connector(handler, *, key=""):
    clock = Clock()
    settings = Settings(email="developer@healthlab.test", api_key=key)
    client = PubMedClient(
        settings,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=clock.sleep,
        clock=clock.time,
    )
    return client, clock


def normal_handler(request):
    if request.url.path.endswith("esearch.fcgi"):
        return httpx.Response(200, content=search_bytes())
    return httpx.Response(200, content=XML)


def test_parser_preserves_text_metadata_and_missing_values():
    documents, _ = parse_records(XML)
    a, b, c = documents
    assert a.title == "Exercise and frailty: a fixture"
    assert a.abstract_sections[0].text == "Text with nested words and CO2."
    assert a.abstract_sections[0].nlm_category == "BACKGROUND"
    assert a.pub_date == {"Year": "2024", "Month": "Jul"}
    assert a.authors == ["Ada Example", "Fixture Group"]
    assert a.doi == "10.0000/fixture"
    assert b.pub_date == {"MedlineDate": "2023 Winter"}
    assert b.extraction_eligibility == "skipped" and b.skip_reason == "missing_abstract"
    assert not b.has_abstract and not b.abstract_sections
    assert c.pub_date is None and c.abstract_sections[0].label is None


def test_empty_abstract_is_not_eligible():
    raw = b"<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>1</PMID><Article><Abstract><AbstractText> </AbstractText></Abstract></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"
    doc = parse_records(raw)[0][0]
    assert not doc.has_abstract and doc.title is None


def test_online_replay_dedup_and_immutable_parent(tmp_path, monkeypatch):
    store = RunStore(tmp_path)
    client, _ = connector(normal_handler)
    run = ingest(store, Question(raw_question="Câu hỏi", query="fixture"), client)
    assert run["status"] == "completed"
    assert run["counts"] == {
        "matched": 116,
        "selected": 3,
        "stored": 3,
        "eligible": 2,
        "skipped": 1,
    }
    first_objects = set((tmp_path / "objects").iterdir())
    second = ingest(store, Question(raw_question="Different question", query="fixture"), client)
    assert [d["snapshot_id"] for d in run["documents"]] == [
        d["snapshot_id"] for d in second["documents"]
    ]
    assert set((tmp_path / "objects").iterdir()) == first_objects
    original_manifest = (tmp_path / "runs" / run["run_id"] / "manifest.json").read_bytes()

    def no_network(*args, **kwargs):
        raise AssertionError("Replay attempted network")

    monkeypatch.setattr(httpx.Client, "__init__", no_network)
    replayed = replay(store, run["run_id"])
    assert replayed["status"] == "completed" and replayed["documents"] == run["documents"]
    assert replayed["requests"] == [] and replayed["parent_run_id"] == run["run_id"]
    assert (tmp_path / "runs" / run["run_id"] / "manifest.json").read_bytes() == original_manifest
    assert main(["--store", str(tmp_path), "replay", run["run_id"]]) == 0


def test_changed_content_creates_new_snapshot(tmp_path):
    store = RunStore(tmp_path)
    c, _ = connector(normal_handler)
    first = ingest(store, Question(raw_question="q", query="q"), c)

    def changed(req):
        return httpx.Response(
            200,
            content=search_bytes()
            if req.url.path.endswith("esearch.fcgi")
            else XML.replace(b"Sixty", b"Seventy"),
        )

    c, _ = connector(changed)
    second = ingest(store, Question(raw_question="q", query="q"), c)
    assert first["documents"][0]["document_id"] == second["documents"][0]["document_id"]
    assert first["documents"][0]["snapshot_id"] != second["documents"][0]["snapshot_id"]
    assert first["documents"][1:] == second["documents"][1:]


def test_no_results_skips_fetch_and_replays(tmp_path):
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, content=search_bytes([], 0))

    c, _ = connector(handler)
    store = RunStore(tmp_path)
    run = ingest(store, Question(raw_question="q", query="q"), c)
    assert len(calls) == 1 and run["outcome"] == "no_results"
    assert replay(store, run["run_id"])["outcome"] == "no_results"


@pytest.mark.parametrize(
    "body,code",
    [
        (b"not JSON", "invalid_search_response"),
        (b'{"error":"rate limit"}', "source_error"),
        (b'{"esearchresult":{"ERROR":"bad"}}', "source_error"),
        (b'{"esearchresult":{"errorlist":{"phrasesnotfound":["q"]}}}', "query_error"),
        (b'{"esearchresult":{"count":"2","idlist":[]}}', "invalid_search_response"),
        (b"[]", "invalid_search_response"),
    ],
)
def test_search_errors_are_not_empty_evidence(tmp_path, body, code):
    c, _ = connector(lambda req: httpx.Response(200, content=body))
    store = RunStore(tmp_path)
    run = ingest(store, Question(raw_question="q", query="q"), c)
    assert run["status"] == "failed" and run["errors"][0]["code"] == code
    assert store.read(run["search_raw_sha256"]) == body


@pytest.mark.parametrize(
    "raw,code",
    [
        (b"<bad", "invalid_xml"),
        (b"<ERROR>error</ERROR>", "source_error"),
        (b"<html/>", "invalid_xml"),
    ],
)
def test_bad_fetch_retains_search_and_raw(tmp_path, raw, code):
    c, _ = connector(
        lambda req: httpx.Response(
            200,
            content=search_bytes() if req.url.path.endswith("esearch.fcgi") else raw,
        )
    )
    store = RunStore(tmp_path)
    run = ingest(store, Question(raw_question="q", query="q"), c)
    assert run["status"] == "failed" and run["errors"][0]["code"] == code
    assert store.read(run["fetch_raw_sha256"]) == raw
    assert run["search"]["count"] == 116
    assert replay(store, run["run_id"])["status"] == "failed"


def test_duplicate_ids_and_records():
    result = parse_search(search_bytes(["101", "101", "102"]))
    assert result["selected_pmids"] == ["101", "102"] and result["duplicate_ids"] == 1
    root = ET.fromstring(XML)
    root.append(ET.fromstring(ET.tostring(root[0])))
    docs, report = parse_records(ET.tostring(root))
    assert len(docs) == 3 and report["duplicate_records"] == 1
    root[-1].find("./MedlineCitation/Article/ArticleTitle").text = "different"
    with pytest.raises(IngestionError, match="Conflicting"):
        parse_records(ET.tostring(root))


def test_missing_unexpected_and_unsupported_records_report_partial(tmp_path):
    raw = XML.replace(b"<PMID>103</PMID>", b"<PMID>999</PMID>").replace(
        b"</PubmedArticleSet>", b"<PubmedBookArticle/></PubmedArticleSet>"
    )
    c, _ = connector(
        lambda req: httpx.Response(
            200,
            content=search_bytes() if req.url.path.endswith("esearch.fcgi") else raw,
        )
    )
    run = ingest(RunStore(tmp_path), Question(raw_question="q", query="q"), c)
    assert run["status"] == "partial"
    assert run["parse_report"]["missing_pmids"] == ["103"]
    assert run["parse_report"]["unexpected_pmids"] == ["999"]
    assert run["parse_report"]["unsupported_tags"] == ["PubmedBookArticle"]
    assert [d["pmid"] for d in run["documents"]] == ["101", "102"]


def test_retry_after_rate_limit_and_spacing(tmp_path):
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, content=b"busy")
        return normal_handler(req)

    c, clock = connector(handler)
    run = ingest(RunStore(tmp_path), Question(raw_question="q", query="q"), c)
    assert run["status"] == "completed" and len(calls) == 3
    assert 2 in clock.waits and 0.35 in clock.waits
    assert [r["attempt"] for r in run["requests"]] == [1, 2, 1]


@pytest.mark.parametrize(
    "kind,expected_attempts",
    [("timeout", 3), ("transport_error", 3), ("http_error", 1), ("rate_limited", 3)],
)
def test_failures_bounded_and_secrets_not_saved(tmp_path, kind, expected_attempts):
    key = "private-secret-test"

    def handler(req):
        if kind == "timeout":
            raise httpx.ReadTimeout(f"URL contains {key}", request=req)
        if kind == "transport_error":
            raise httpx.ConnectError(f"URL contains {key}", request=req)
        return httpx.Response(401 if kind == "http_error" else 429, content=key.encode())

    c, _ = connector(handler, key=key)
    run = ingest(RunStore(tmp_path), Question(raw_question="q", query="q"), c)
    assert run["status"] == "failed" and run["errors"][0]["code"] == kind
    assert len(run["requests"]) == expected_attempts
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert key.encode() not in path.read_bytes()


def test_long_retry_after_is_deferred(tmp_path):
    c, clock = connector(lambda req: httpx.Response(429, headers={"Retry-After": "120"}))
    run = ingest(RunStore(tmp_path), Question(raw_question="q", query="q"), c)
    assert run["errors"][0]["code"] == "retry_deferred"
    assert not clock.waits


def test_fetch_timeout_keeps_search_and_replay_does_not_refetch(tmp_path):
    def handler(req):
        if req.url.path.endswith("esearch.fcgi"):
            return normal_handler(req)
        raise httpx.ReadTimeout("timed out", request=req)

    c, _ = connector(handler)
    store = RunStore(tmp_path)
    run = ingest(store, Question(raw_question="q", query="q"), c)
    assert run["stage"] == "fetch" and run["search_raw_sha256"]
    assert replay(store, run["run_id"])["errors"][0]["code"] == "cache_incomplete"


def test_cache_corruption_detected(tmp_path):
    c, _ = connector(normal_handler)
    store = RunStore(tmp_path)
    run = ingest(store, Question(raw_question="q", query="q"), c)
    (tmp_path / "objects" / run["fetch_raw_sha256"]).write_bytes(b"tampered")
    result = replay(store, run["run_id"])
    assert result["status"] == "failed" and result["errors"][0]["code"] == "cache_corrupt"


def test_import_probe_preserves_manual_provenance(tmp_path):
    root = tmp_path / "input"
    root.mkdir()
    artifact = {
        "timestamp": "2026-09-16T15:46:31",
        "query": "q",
        "experiment_a_sort_relevance": {
            "params": {"term": "q", "sort": "relevance"},
            "pmids": ["101", "103"],
            "raw_response": json.loads(search_bytes(["101", "103"])),
        },
        "experiment_c_fetch": {
            "search_candidates_pmids": ["101", "103"],
            "manual_test_pmids": ["102"],
            "total_requested": ["101", "103", "102"],
            "fetch_params": {"id": "101,103,102"},
        },
    }
    (root / "probe.json").write_text(json.dumps(artifact))
    (root / "raw.xml").write_bytes(XML)
    store = RunStore(tmp_path / "store")
    run = import_probe(store, root / "probe.json", root / "raw.xml")
    assert run["mode"] == "import_probe" and run["status"] == "completed"
    assert run["selection"]["manual_test_pmids"] == ["102"]
    assert run["requests"] == []
    assert replay(store, run["run_id"])["documents"] == run["documents"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mindate": "2026-01-01"},
        {"mindate": "2026-01-01", "maxdate": "2025-01-01"},
        {"query": " "},
    ],
)
def test_question_rejects_invalid_filters(kwargs):
    with pytest.raises(ValidationError):
        Question(**({"raw_question": "q", "query": "q"} | kwargs))


def test_online_rejects_placeholder_email():
    with pytest.raises(ValueError, match="real contact"):
        PubMedClient(Settings(email="your_email@example.com"))


def test_duplicate_conflict_in_unnormalized_metadata_is_not_lost():
    root = ET.fromstring(XML)
    duplicate = ET.fromstring(ET.tostring(root[0]))
    ET.SubElement(duplicate, "ExtraSourceMetadata").text = "changed revision"
    root.append(duplicate)
    with pytest.raises(IngestionError, match="Conflicting"):
        parse_records(ET.tostring(root))


def test_date_filter_and_transport_params_are_preserved(tmp_path):
    c, _ = connector(normal_handler)
    run = ingest(
        RunStore(tmp_path),
        Question(
            raw_question="raw",
            query="q",
            sort="pub_date",
            mindate="2021-09-16",
            maxdate="2026-09-16",
        ),
        c,
    )
    params = run["requests"][0]["params"]
    assert params["mindate"] == "2021/09/16" and params["maxdate"] == "2026/09/16"
    assert params["datetype"] == "pdat" and params["sort"] == "pub_date"
    assert params["retmax"] == 5 and params["retstart"] == 0
    assert run["requests"][0]["started_at"].endswith("+00:00")


def test_server_error_recovers_with_bounded_backoff(tmp_path):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(503, content=b"busy") if len(calls) <= 2 else normal_handler(req)

    c, clock = connector(handler)
    run = ingest(RunStore(tmp_path), Question(raw_question="q", query="q"), c)
    assert run["status"] == "completed"
    assert [r["http_status"] for r in run["requests"]] == [503, 503, 200, 200]
    assert 1 in clock.waits and 2 in clock.waits


def test_retry_after_http_date():
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    responses = [
        httpx.Response(
            429, headers={"Retry-After": format_datetime(datetime.now(UTC) + timedelta(seconds=5))}
        ),
        httpx.Response(200, content=search_bytes()),
    ]
    c, clock = connector(lambda req: responses.pop(0))
    c.request("esearch.fcgi", {}, lambda *args: None)
    assert 3 < max(clock.waits) <= 5


def test_missing_cache_and_missing_pmid_are_explicit(tmp_path):
    c, _ = connector(normal_handler)
    store = RunStore(tmp_path)
    run = ingest(store, Question(raw_question="q", query="q"), c)
    (tmp_path / "objects" / run["search_raw_sha256"]).unlink()
    assert replay(store, run["run_id"])["errors"][0]["code"] == "cache_missing"
    with pytest.raises(IngestionError) as error:
        parse_records(XML.replace(b"<PMID>101</PMID>", b"<PMID/>"))
    assert error.value.code == "invalid_record"


def test_settings_field_names_secrets_and_env_aliases(tmp_path, monkeypatch):
    monkeypatch.setenv("NCBI_API_KEY", "from-env")
    s = Settings(_env_file=None, email="dev@healthlab.test", api_key="explicit")
    assert s.email == "dev@healthlab.test" and s.api_key.get_secret_value() == "explicit"
    assert "explicit" not in repr(s)
    assert Settings(_env_file=None).api_key.get_secret_value() == "from-env"


def test_placeholder_email_does_not_block_offline_cli(tmp_path, monkeypatch):
    c, _ = connector(normal_handler)
    run = ingest(RunStore(tmp_path), Question(raw_question="q", query="q"), c)
    monkeypatch.setenv("NCBI_EMAIL", "your_email@example.com")
    assert main(["--store", str(tmp_path), "replay", run["run_id"]]) == 0


def test_api_key_is_unwrapped_only_on_wire(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return normal_handler(request)

    c, _ = connector(handler, key="wire-secret")
    run = ingest(RunStore(tmp_path), Question(raw_question="q", query="q"), c)
    assert requests[0].url.params["api_key"] == "wire-secret"
    assert all("api_key" not in r["params"] for r in run["requests"])
    assert run["status"] == "completed"


def test_malformed_manifest_fails_explicitly(tmp_path):
    store = RunStore(tmp_path)
    run = store.new("online", {"query": "q"})
    path = tmp_path / "runs" / run["run_id"] / "manifest.json"
    path.write_text("[]")
    with pytest.raises(IngestionError, match="must be an object"):
        store.load(run["run_id"])


def test_storage_error_is_recorded_when_manifest_remains_writable(tmp_path, monkeypatch):
    store = RunStore(tmp_path)

    def fail_write(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(store, "put", fail_write)
    c, _ = connector(normal_handler)
    run = ingest(store, Question(raw_question="q", query="q"), c)
    assert run["status"] == "failed" and run["errors"][0]["code"] == "storage_error"
