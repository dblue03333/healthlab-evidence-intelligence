"""Publication-window API inventory, explicit content skips and recoverable pagination."""

import json
from datetime import date
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from healthlab.signal.collection import collect, load_documents, resume_collection
from healthlab.signal.contracts import Registry
from healthlab.signal.source_http import FetchPolicy, HTMLFetcher
from healthlab.store import RunStore
from tests.test_signal_discovery import HOST, article, card, run, source

WINDOW = {"mindate": date(2026, 10, 12), "maxdate": date(2026, 10, 16)}
POLICY = FetchPolicy(max_attempts=2, request_interval=0, backoff_seconds=0)


def wp_source(**options):
    return source(
        html_discovery={
            "wordpress_api_url": HOST + "/wp-json/wp/v2/posts",
            "api_page_size": 1,
            "max_listing_pages": 3,
            **options,
        }
    )


def record(identifier, day="2026-10-14T00:00:00"):
    return {
        "id": identifier,
        "date": day,
        "link": HOST + f"/article/{identifier}",
        "title": {"rendered": f"Report {identifier}"},
    }


def api_response(records, total="2", pages="2"):
    return httpx.Response(
        200, json=records, headers={"x-wp-total": total, "x-wp-totalpages": pages}
    )


def collect_api(tmp_path, handler, src=None):
    store = RunStore(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = collect(
            store,
            Registry(schema_version="signal-registry-3", sources=[src or wp_source()]),
            **WINDOW,
            rss_client=client,
            fetch_policy=POLICY,
        )
    return store, result


def test_api_inventory_is_window_bound_paginated_and_auditable(tmp_path):
    requests = []

    def handle(req):
        requests.append(str(req.url))
        if "/wp-json/" not in req.url.path:
            return httpx.Response(
                200, text=article("2026-10-14"), headers={"content-type": "text/html"}
            )
        query = parse_qs(req.url.query.decode())
        assert query["after"] == ["2026-10-11T23:59:59"]
        assert query["before"] == ["2026-10-17T00:00:00"]
        assert query["status"] == ["publish"] and query["orderby"] == ["date"]
        return api_response([record(int(query["page"][0]))])

    store, result = collect_api(tmp_path, handle)
    check = result["source_checks"][0]
    assert result["status"] == "completed" and len(load_documents(store, result)) == 2
    assert len(requests) == 4 and len(check["listing_pages"]) == 2
    assert check["discovery_method"] == "wordpress_rest"
    extraction = json.loads(store.read(check["article_checks"][1]["extraction_sha256"]))
    citation = extraction["publication_date_evidence"][-1]
    assert citation["locator"] == "json[0].date" and "page=2" in citation["listing_url"]
    assert json.loads(store.read(citation["raw_sha256"]))[0]["id"] == 2


@pytest.mark.parametrize("headers", [{}, {"x-wp-total": "3", "x-wp-totalpages": "1"}])
def test_invalid_api_totals_cannot_claim_complete(tmp_path, headers):
    _, result = collect_api(
        tmp_path, lambda _: httpx.Response(200, json=[record(1)], headers=headers)
    )
    check = result["source_checks"][0]
    assert not check["coverage_complete"]
    assert check["listing_gaps"][0]["code"] == "api_listing_invalid"


@pytest.mark.parametrize("failure", ["totals_changed", "duplicate_id"])
def test_api_total_change_and_duplicate_records_remain_gaps(tmp_path, failure):
    def handle(req):
        if "/wp-json/" not in req.url.path:
            return httpx.Response(
                200, text=article("2026-10-14"), headers={"content-type": "text/html"}
            )
        page = parse_qs(req.url.query.decode())["page"][0]
        changed = page != "1" and failure == "totals_changed"
        return api_response(
            [record(1)], total="3" if changed else "2", pages="3" if changed else "2"
        )

    _, result = collect_api(tmp_path, handle)
    assert result["status"] == "partial"
    assert result["source_checks"][0]["listing_gaps"][0]["code"] == "api_listing_invalid"


def test_empty_api_with_zero_totals_is_valid_empty_window(tmp_path):
    _, result = collect_api(tmp_path, lambda _: api_response([], "0", "0"))
    assert result["status"] == "completed" and not result["documents"]
    assert result["source_checks"][0]["coverage_complete"]


def test_api_budget_retains_next_page_gap(tmp_path):
    def handle(req):
        if "/wp-json/" in req.url.path:
            return api_response([record(1)])
        return httpx.Response(
            200, text=article("2026-10-14"), headers={"content-type": "text/html"}
        )

    _, result = collect_api(tmp_path, handle, wp_source(max_listing_pages=1))
    check = result["source_checks"][0]
    assert result["status"] == "partial"
    assert "page=2" in check["deferred_listing_urls"][0]


def test_api_resume_reuses_raw_json_and_saved_totals(tmp_path):
    store = RunStore(tmp_path)
    registry = Registry(schema_version="signal-registry-3", sources=[wp_source()])
    requested = []

    def first(req):
        requested.append(str(req.url))
        if "page=2" in str(req.url):
            raise KeyboardInterrupt()
        return api_response([record(1)])

    with (
        httpx.Client(transport=httpx.MockTransport(first)) as client,
        pytest.raises(KeyboardInterrupt),
    ):
        collect(store, registry, **WINDOW, rss_client=client, fetch_policy=POLICY)
    run_id = next((tmp_path / "runs").iterdir()).name

    def second(req):
        assert parse_qs(req.url.query.decode()).get("page") != ["1"]
        if "/wp-json/" in req.url.path:
            return api_response([record(2)])
        return httpx.Response(
            200, text=article("2026-10-14"), headers={"content-type": "text/html"}
        )

    with httpx.Client(transport=httpx.MockTransport(second)) as client:
        result = resume_collection(store, run_id, rss_client=client)
    assert result["status"] == "completed" and len(result["documents"]) == 2
    assert len([u for u in requested if parse_qs(urlsplit(u).query).get("page") == ["1"]]) == 1


def test_dated_outside_window_card_does_not_claim_article_content_read(tmp_path):
    src = source(html_discovery={"skip_outside_window": True})
    _, result, check, requested = run(tmp_path, {HOST + "/news": card("old", "2026-10-01")}, src)
    assert requested == [HOST + "/news"] and result["status"] == "completed"
    item = check["article_checks"][0]
    assert (
        item["status"] == "outside_window"
        and item["content_coverage"] == "not_fetched_outside_window"
    )
    assert item["publication_date_evidence"][0]["raw_sha256"] == check["raw_sha256"]
    assert "raw_sha256" not in item


def test_date_skip_does_not_hide_sparse_in_window_content(tmp_path):
    src = source(html_discovery={"skip_outside_window": True})
    _, result, check, requested = run(
        tmp_path,
        {HOST + "/news": card("new"), HOST + "/article/new": "<h1>Report</h1><main></main>"},
        src,
    )
    assert HOST + "/article/new" in requested
    assert result["status"] != "completed" and not check["coverage_complete"]
    assert check["article_checks"][0]["error_code"] == "html_article_sparse"


def test_sequential_pagination_stops_on_publication_boundary_not_budget(tmp_path):
    src = source(
        html_discovery={
            "listing_path_patterns": [r"/news(?:/\d+)?"],
            "sequential_page_pattern": r"/news(?:/(?P<page>\d+))?",
            "max_listing_pages": 3,
            "stop_before_window": True,
            "skip_outside_window": True,
        }
    )
    pages = {
        HOST + "/news": card("new") + '<a href="/news/2">2</a><a href="/news/30">last</a>',
        HOST + "/news/2": card("old", "2026-10-01") + '<a href="/news/3">3</a>',
    }
    _, result, check, requested = run(tmp_path, pages, src)
    assert result["status"] == "completed"
    assert len(check["listing_pages"]) == 2 and not check["deferred_listing_urls"]
    assert check["listing_pages"][-1]["stop_reason"] == "publication_boundary"
    assert not any(u.endswith(("/30", "/3")) for u in requested)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://elsewhere.example.org/wp/v2/posts",
        HOST + "/wp/v2/posts?status=private",
        "http://news.example.org/wp/v2/posts",
    ],
)
def test_api_configuration_cannot_expand_host_or_query_scope(endpoint):
    with pytest.raises(ValueError):
        wp_source(wordpress_api_url=endpoint)


def test_explicit_source_response_limit_remains_bounded(tmp_path):
    raw = b" " * 2_100_000

    def handle(_):
        return httpx.Response(200, content=raw, headers={"content-type": "text/html"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        store = RunStore(tmp_path)
        cp = {"responses": {}, "http_attempts": []}
        result, _ = HTMLFetcher(client, store, POLICY, max_bytes=4_000_000).fetch(
            HOST, cp, lambda: None
        )
        assert result == raw
        with pytest.raises(ValueError):
            HTMLFetcher(client, store, POLICY, max_bytes=32_000_001)


def test_actual_population_api_inventory_and_headers_cover_the_period(tmp_path):
    import gzip
    import hashlib
    from pathlib import Path

    from healthlab.signal.discovery import discover_listings

    fixtures = Path(__file__).parent / "fixtures" / "signal_html"
    record = next(
        r
        for r in json.loads((fixtures / "provenance.json").read_bytes())
        if r["file"] == "moh_window_api.json.gz"
    )
    raw = gzip.decompress((fixtures / record["file"]).read_bytes())
    assert hashlib.sha256(raw).hexdigest() == record["raw_sha256"]
    registry = Registry.model_validate_json(
        Path("tests/fixtures/signal_html/population_registry_legacy.json").read_bytes()
    )
    source = next(s for s in registry.sources if s.source_id == "moh_population")

    def response(request):
        assert str(request.url) == record["source_url"]
        return httpx.Response(
            200,
            content=raw,
            headers={"content-type": "application/json", **record["response_headers"]},
        )

    store = RunStore(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        checkpoint = {"responses": {}, "http_attempts": []}
        result = discover_listings(
            store, source, HTMLFetcher(client, store, POLICY), checkpoint, lambda: None, **WINDOW
        )
    assert result["discovery_complete"] and len(result["candidates"]) == 22
    assert result["listing_pages"][0]["total_posts"] == 22
    assert result["listing_pages"][0]["publication_range"] == {
        "min": "2026-10-12",
        "max": "2026-10-16",
    }
    assert any("canh-bao-gia-tang" in c["url"] for c in result["candidates"])
