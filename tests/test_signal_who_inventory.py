"""WHO API fills stale-listing gaps with bounded, evidenced discovery."""

import gzip
import json
from datetime import date
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from healthlab.data.datasets import build_dataset, gold_records
from healthlab.signal.collection import collect, replay_collection
from healthlab.signal.contracts import Registry, Source
from healthlab.signal.source_http import FetchPolicy, HTMLFetcher
from healthlab.signal.who_inventory import PROVIDER, supplement_speeches
from healthlab.store import RunStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/signal_html"
POLICY = FetchPolicy(max_attempts=1, request_interval=0)
WINDOW = {"mindate": date(2026, 10, 14), "maxdate": date(2026, 10, 27)}


def source():
    src = Registry.model_validate_json((ROOT / "config/signal_sources.json").read_bytes()).sources[
        2
    ]
    src.html_discovery.who_ageing_widget = False
    src.url = "https://www.who.int/vietnam/news/speeches/1"
    src.html_discovery.additional_listing_urls = []
    return Source.model_validate(src.model_dump())


def initial():
    return {
        "candidates": [],
        "dates": {},
        "date_evidence": {},
        "listing_pages": [],
        "listing_gaps": [],
        "deferred_listing_urls": [],
        "listing_budget": 12,
        "seed_count": 1,
        "discovered_link_count": 0,
    }


def item(number=1, day="2026-10-27"):
    return {
        "Id": str(UUID(int=number)),
        "Provider": PROVIDER,
        "ItemDefaultUrl": f"/test-{number}",
        "Title": f"Test speech {number}",
        "PublicationDateAndTime": day + "T02:00:00Z",
    }


def supplement(tmp_path, payloads, *, size=100, limit=2):
    src = source()
    src.html_discovery.api_page_size = size
    src.html_discovery.who_speeches_max_pages = limit
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=payloads[len(requests) - 1])

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = supplement_speeches(
            initial(),
            src,
            HTMLFetcher(client, RunStore(tmp_path), POLICY),
            {"responses": {}, "http_attempts": []},
            lambda: None,
            **WINDOW,
        )
    return result, requests


def test_captured_stale_html_omits_two_speeches_but_api_and_articles_exist(tmp_path):
    api = json.loads(gzip.decompress((FIXTURES / "who_speeches_api_20261028.json.gz").read_bytes()))
    html = gzip.decompress((FIXTURES / "who_speeches_stale_20261028.html.gz").read_bytes())
    selected = [r for r in api["value"] if r["PublicationDateAndTime"].startswith("2026-10-27")]
    assert len(selected) == 1
    assert all(r["ItemDefaultUrl"].encode() not in html for r in api["value"][:2])
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if request.url.path == "/api/news/speeches":
            # Adapt the real API records to the narrower fixture request window.
            return httpx.Response(200, json={"@odata.count": 1, "value": selected})
        raw = (
            html
            if request.url.path.endswith("/1")
            else gzip.decompress((FIXTURES / "who_speech_20261027.html.gz").read_bytes())
        )
        return httpx.Response(200, content=raw, headers={"content-type": "text/html"})

    store = RunStore(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        run = collect(
            store,
            Registry(schema_version="signal-registry-3", sources=[source()]),
            rss_client=client,
            fetch_policy=POLICY,
            remember_candidates=True,
            **WINDOW,
        )
    result = build_dataset(store, run["run_id"])
    assert result["status"] == "ready" and result["documents"] == 1 and len(requests) == 3
    row = gold_records(store, result["dataset_id"])[0]
    assert row["publication_date"] == "2026-10-27" and len(row["body"]) > 1000
    assert any("json.value[0]" in e["locator"] for e in row["publication_date_evidence"])
    replay = replay_collection(store, run["run_id"])
    assert build_dataset(store, replay["run_id"])["status"] == "ready"


def test_api_totals_page_order_and_zero_result_have_explicit_closure(tmp_path):
    result, requests = supplement(
        tmp_path,
        [
            {"@odata.count": 3, "value": [item(1), item(2, "2026-10-26")]},
            {"@odata.count": 3, "value": [item(3, "2026-10-25")]},
        ],
        size=2,
    )
    assert result["discovery_complete"] and len(result["candidates"]) == 3
    assert [r.url.params["$skip"] for r in requests] == ["0", "2"]
    assert result["listing_pages"][-1]["stop_reason"] == "api_totals_exhausted"
    empty, _ = supplement(tmp_path / "empty", [{"@odata.count": 0, "value": []}])
    assert empty["discovery_complete"] and not empty["candidates"]


@pytest.mark.parametrize("failure", ["provider", "count", "order", "date", "duplicate", "path"])
def test_unverified_api_data_never_claims_complete_inventory(tmp_path, failure):
    rows = [item(1), item(2, "2026-10-26")]
    count = 2
    if failure == "provider":
        rows[0]["Provider"] = "foreign-provider"
    if failure == "count":
        count = 3
    if failure == "order":
        rows.reverse()
    if failure == "date":
        rows[0]["PublicationDateAndTime"] = "2026-10-28T00:00:00Z"
    if failure == "duplicate":
        rows[1]["Id"] = rows[0]["Id"]
    if failure == "path":
        rows[0]["ItemDefaultUrl"] = "//foreign.example/item"
    result, _ = supplement(tmp_path, [{"@odata.count": count, "value": rows}])
    assert not result["discovery_complete"]
    assert result["listing_gaps"][0]["code"] == "who_api_inventory_invalid"


def test_page_budget_and_repeated_api_page_remain_gaps(tmp_path):
    truncated, _ = supplement(tmp_path, [{"@odata.count": 2, "value": [item()]}], size=1, limit=1)
    assert truncated["listing_gaps"][0]["code"] == "who_api_inventory_truncated"
    assert truncated["deferred_listing_urls"]
    repeated, _ = supplement(
        tmp_path / "repeated",
        [
            {"@odata.count": 2, "value": [item()]},
            {"@odata.count": 2, "value": [item()]},
        ],
        size=1,
    )
    assert not repeated["discovery_complete"]


def test_supplement_cannot_be_enabled_for_other_hosts_or_without_speech_branch():
    src = source().model_dump()
    src["url"] = "https://www.who.int/health-topics/ageing"
    with pytest.raises(ValueError, match="verified host and branch"):
        Source.model_validate(src)


def test_captured_ageing_widget_has_three_new_candidates_and_cached_lineage(tmp_path):
    from healthlab.signal.who_inventory import AGEING_URL, supplement_ageing_widget

    src = Registry.model_validate_json((ROOT / "config/signal_sources.json").read_bytes()).sources[
        2
    ]
    store = RunStore(tmp_path)
    landing = gzip.decompress(
        (FIXTURES / "who_ageing_widget_landing_20261028.html.gz").read_bytes()
    )
    payload = gzip.decompress((FIXTURES / "who_ageing_widget_20261028.json.gz").read_bytes())
    digest = store.put(landing)
    requests = []
    checkpoint = {"responses": {}, "http_attempts": []}

    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=payload, headers={"content-type": "application/json"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        fetcher = HTMLFetcher(client, store, POLICY)
        for _ in range(2):
            result = initial()
            result["listing_pages"] = [
                {"url": AGEING_URL, "status": "completed", "raw_sha256": digest}
            ]
            result = supplement_ageing_widget(result, src, fetcher, checkpoint, lambda: None)
            assert result["discovery_complete"] and len(result["candidates"]) == 3
            page = result["listing_pages"][-1]
            assert page["declaration_raw_sha256"] == digest
            assert page["stop_reason"] == "declared_top_three_snapshot"
            assert page["historical_membership_complete"] is False
            assert sorted(next(iter(d)) for d in result["dates"].values()) == [
                "2026-10-16",
                "2026-10-21",
                "2026-10-23",
            ]
            assert all(b["url"].encode() not in landing for b in result["candidates"])
            assert all(
                store.read(e[0]["raw_sha256"]) == payload for e in result["date_evidence"].values()
            )
    assert len(requests) == 1


@pytest.mark.parametrize("failure", ["declaration", "query", "duplicate", "date", "path", "count"])
def test_ageing_widget_changes_fail_visibly_without_partial_candidates(tmp_path, failure):
    from healthlab.signal.who_inventory import AGEING_URL, supplement_ageing_widget

    src = Registry.model_validate_json((ROOT / "config/signal_sources.json").read_bytes()).sources[
        2
    ]
    landing = gzip.decompress(
        (FIXTURES / "who_ageing_widget_landing_20261028.html.gz").read_bytes()
    )
    payload = json.loads(
        gzip.decompress((FIXTURES / "who_ageing_widget_20261028.json.gz").read_bytes())
    )
    if failure == "declaration":
        landing = b"<html>No widget</html>"
    if failure == "query":
        landing = landing.replace(b"$top=3", b"$top=999")
    if failure == "duplicate":
        payload["value"][1] = payload["value"][0]
    if failure == "date":
        payload["value"][0]["FormatedDate"] = "1 October 2026"
    if failure == "path":
        payload["value"][0]["ItemDefaultUrl"] = "//foreign.example/item"
    if failure == "count":
        payload["value"].pop()
    store = RunStore(tmp_path)
    result = initial()
    result["listing_pages"] = [
        {"url": AGEING_URL, "status": "completed", "raw_sha256": store.put(landing)}
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as client:
        result = supplement_ageing_widget(
            result,
            src,
            HTMLFetcher(client, store, POLICY),
            {"responses": {}, "http_attempts": []},
            lambda: None,
        )
    assert not result["discovery_complete"] and not result["candidates"]
    assert result["listing_gaps"][-1]["code"] == "who_ageing_widget_invalid"


def test_ageing_widget_requires_official_landing():
    src = source().model_dump()
    src["html_discovery"]["who_ageing_widget"] = True
    with pytest.raises(ValueError, match="verified host and landing"):
        Source.model_validate(src)


def test_explicit_larger_response_limit_remains_bounded(tmp_path):
    from healthlab.signal.source_http import FetchFailure

    raw = b"<html>" + b"x" * 8_000_001 + b"</html>"
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=raw, headers={"content-type": "text/html"})
        )
    ) as client:
        store = RunStore(tmp_path)
        with pytest.raises(FetchFailure, match="HTML request failed") as exc:
            HTMLFetcher(client, store, POLICY, max_bytes=8_000_000).fetch(
                "https://www.who.int/news/item/large",
                {"responses": {}, "http_attempts": []},
                lambda: None,
            )
        assert exc.value.code == "html_limit"
        fetched, _ = HTMLFetcher(client, store, POLICY, max_bytes=32_000_000).fetch(
            "https://www.who.int/news/item/large",
            {"responses": {}, "http_attempts": []},
            lambda: None,
        )
        assert fetched == raw
        with pytest.raises(ValueError, match="Response limit"):
            HTMLFetcher(client, store, POLICY, max_bytes=32_000_001)
