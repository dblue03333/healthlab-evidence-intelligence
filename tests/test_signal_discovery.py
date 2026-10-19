"""Finite traversal, listing provenance, conservative gaps and checkpoint recovery."""

import json
from datetime import date

import httpx
import pytest

from healthlab.signal.collection import collect, load_documents
from healthlab.signal.contracts import Registry, Source
from healthlab.signal.discovery import listing_link
from healthlab.signal.html_sources import collect_html
from healthlab.signal.source_http import FetchPolicy, HTMLFetcher
from healthlab.store import RunStore

HOST = "https://news.example.org"
BODY = "A synthetic report about community care for older adults in Viet Nam. This text is only an extraction and provenance fixture."
POLICY = FetchPolicy(max_attempts=1, request_interval=0)


def source(**overrides):
    config = {
        "source_id": "fixture",
        "organization": "Synthetic source",
        "kind": "html",
        "url": HOST + "/news",
        "usage_policy": "Synthetic fixture only",
        "max_article_fetches": 20,
        "html_rules": {
            "rule_id": "fixture",
            "host": "news.example.org",
            "article_urls": {"include_path_patterns": [r"/article/[^/]+"]},
            "body": {"tag": "main"},
            "listing_date": {"selector": {"tag": "time"}, "format": "iso"},
        },
        "html_discovery": {"listing_path_patterns": [r"/news(?:/\d+)?"], "max_listing_pages": 3},
    }
    config.update(overrides)
    return Source.model_validate(config)


def card(name, day="2026-10-14"):
    return f'<article><a href="/article/{name}"><time>{day}</time>Report {name}</a></article>'


def article(day=None):
    return (
        f'<meta property="article:published_time" content="{day}">' if day else ""
    ) + f"<h1>Report</h1><main>{BODY}</main>"


def run(tmp_path, pages, src=None):
    requests = []

    def respond(request):
        requests.append(str(request.url))
        value = pages.get(str(request.url), article())
        if isinstance(value, int):
            return httpx.Response(value, headers={"Content-Type": "text/html"})
        return httpx.Response(200, text=value, headers={"Content-Type": "text/html"})

    store = RunStore(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = collect(
            store,
            Registry(schema_version="signal-registry-3", sources=[src or source()]),
            mindate=date(2026, 10, 12),
            maxdate=date(2026, 10, 16),
            rss_client=client,
            fetch_policy=POLICY,
        )
    return store, result, result["source_checks"][0], requests


def test_multiple_pages_deduplicate_articles_and_bind_date_to_correct_raw_listing(tmp_path):
    pages = {
        HOST + "/news": card("a") + '<a href="/news/2">2</a>',
        HOST + "/news/2": card("a") + card("b") + '<a href="/news/3">3</a>',
        HOST + "/news/3": card("old", "2026-10-01"),
    }
    store, result, check, requests = run(tmp_path, pages)
    assert result["status"] == "completed" and check["pagination_followed"]
    assert len(check["listing_pages"]) == 3 and check["candidate_count"] == 3
    assert check["counts"] == {"included": 2, "outside_window": 1, "needs_review": 0, "failed": 0}
    assert requests.count(HOST + "/article/a") == 1
    b = next(a for a in check["article_checks"] if a["url"] == HOST + "/article/b")
    evidence = json.loads(store.read(b["extraction_sha256"]))["publication_date_evidence"]
    assert evidence[0]["listing_url"] == HOST + "/news/2"
    assert evidence[0]["raw_sha256"] == check["listing_pages"][1]["raw_sha256"]
    assert len(load_documents(store, result)) == 2


def test_budget_gap_blocks_complete_and_keeps_unread_urls(tmp_path):
    src = source(
        html_discovery={"listing_path_patterns": [r"/news(?:/\d+)?"], "max_listing_pages": 1}
    )
    _, result, check, requests = run(
        tmp_path, {HOST + "/news": card("a") + '<a href="/news/2">next</a>'}, src
    )
    assert result["status"] == "partial" and not check["coverage_complete"]
    assert check["deferred_listing_urls"] == [HOST + "/news/2"]
    assert any(g["code"] == "listing_budget_exhausted" for g in check["listing_gaps"])
    assert HOST + "/news/2" not in requests


def test_failed_listing_preserves_good_documents_and_visible_gap(tmp_path):
    _, result, check, _ = run(
        tmp_path, {HOST + "/news": card("a") + '<a href="/news/2">next</a>', HOST + "/news/2": 503}
    )
    assert result["status"] == "partial" and len(result["documents"]) == 1
    assert check["listing_pages"][1]["status"] == "failed"
    assert check["listing_gaps"][0]["code"] == "html_http"


def test_conflicting_dates_across_listings_do_not_silently_choose_one(tmp_path):
    _, result, check, _ = run(
        tmp_path,
        {
            HOST + "/news": card("a") + '<a href="/news/2">next</a>',
            HOST + "/news/2": card("a", "2026-10-15"),
        },
    )
    assert not result["documents"]
    assert check["article_checks"][0]["error_code"] == "html_date_conflict"


def test_javascript_fragments_are_gaps_not_repeated_first_page_requests(tmp_path):
    html = (
        card("a")
        + '<a href="#/page/2">2</a><div data-flatsome-relay=\'{"currentPage":"1","totalPages":5}\'></div>'
    )
    _, result, check, requests = run(tmp_path, {HOST + "/news": html})
    assert result["status"] in {"partial", "failed"}
    assert any(g["code"] == "javascript_pagination_unread" for g in check["listing_gaps"])
    assert requests.count(HOST + "/news") == 1


def test_date_boundary_is_conservative_and_does_not_skip_other_seed_branches(tmp_path):
    src = source(
        html_discovery={
            "additional_listing_urls": [HOST + "/other"],
            "listing_path_patterns": [r"/news(?:/\d+)?"],
            "max_listing_pages": 3,
            "stop_before_window": True,
        }
    )
    pages = {
        HOST + "/news": card("old", "2026-10-01") + '<a href="/news/2">next</a>',
        HOST + "/other": card("new"),
    }
    _, result, check, requests = run(tmp_path, pages, src)
    assert result["status"] == "completed" and len(result["documents"]) == 1
    assert check["listing_pages"][0]["stop_reason"] == "publication_boundary"
    assert HOST + "/other" in requests and HOST + "/news/2" not in requests
    # An undated card prevents the early boundary, even if all other dates are old.
    pages[HOST + "/news"] += '<a href="/article/unknown">Unknown date</a>'
    pages[HOST + "/news/2"] = card("other")
    _, _, check, requests = run(tmp_path / "unknown", pages, src)
    assert HOST + "/news/2" in requests


def test_same_article_set_on_different_pages_is_a_visible_stale_pager_gap(tmp_path):
    _, result, check, _ = run(
        tmp_path,
        {
            HOST + "/news": card("a") + '<a href="/news/2">next</a>',
            HOST + "/news/2": card("a") + '<a href="/news/3">next</a>',
        },
    )
    assert result["status"] == "partial"
    assert check["listing_pages"][1]["stop_reason"] == "repeated_listing"
    assert any(g["code"] == "listing_repeated_content" for g in check["listing_gaps"])


def test_empty_challenge_listing_cannot_be_reported_as_no_news(tmp_path):
    _, result, check, _ = run(
        tmp_path,
        {
            HOST
            + "/news": "Your support ID is: 123. Testing whether you are a human visitor. CAPTCHA"
        },
    )
    assert result["status"] == "failed"
    assert check["listing_gaps"][0]["code"] == "listing_access_challenge"


@pytest.mark.parametrize(
    "href,allowed",
    [
        ("/news/2", True),
        ("http://news.example.org/news/2", True),
        ("https://evil.org/news/2", False),
        ("https://news.example.org.evil.org/news/2", False),
        ("https://user@news.example.org/news/2", False),
        ("https://news.example.org:444/news/2", False),
        ("/article/a", False),
        ("#/page/2", False),
    ],
)
def test_listing_scope_filters_before_network(href, allowed):
    rules = source().html_discovery
    rules.upgrade_same_host_http = True
    assert bool(listing_link(href, HOST + "/news", "news.example.org", rules)) == allowed


def test_query_constraints_and_tracking_normalization_are_explicit():
    rules = source().html_discovery
    rules.required_query_parameters = {"topic": "ageing"}
    rules.drop_query_parameters = ["utm_source"]
    assert (
        listing_link("/news/2?utm_source=x&topic=ageing", HOST + "/news", "news.example.org", rules)
        == HOST + "/news/2?topic=ageing"
    )
    assert listing_link("/news/2?topic=other", HOST + "/news", "news.example.org", rules) is None
    assert (
        listing_link("/news/2?topic=ageing&topic=other", HOST + "/news", "news.example.org", rules)
        is None
    )


def test_interrupted_multi_listing_resume_reuses_frozen_listing_and_article_responses(tmp_path):
    store = RunStore(tmp_path)
    src = source()
    checkpoint = None
    requests = []
    interrupted = False
    pages = {HOST + "/news": card("a") + '<a href="/news/2">next</a>', HOST + "/news/2": card("b")}

    def save(value):
        nonlocal checkpoint
        checkpoint = json.loads(json.dumps(value))

    def respond(request):
        nonlocal interrupted
        url = str(request.url)
        requests.append(url)
        if url == HOST + "/article/b" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt()
        return httpx.Response(
            200, text=pages.get(url, article()), headers={"Content-Type": "text/html"}
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(KeyboardInterrupt):
            collect_html(
                store,
                src,
                client,
                mindate=date(2026, 10, 12),
                maxdate=date(2026, 10, 16),
                on_checkpoint=save,
                fetcher=HTMLFetcher(client, store, FetchPolicy(max_attempts=2, request_interval=0)),
            )
        docs, check = collect_html(
            store,
            src,
            client,
            mindate=date(2026, 10, 12),
            maxdate=date(2026, 10, 16),
            checkpoint=checkpoint,
            on_checkpoint=save,
            fetcher=HTMLFetcher(client, store, FetchPolicy(max_attempts=2, request_interval=0)),
        )
    assert len(docs) == 2 and check["coverage_complete"]
    for url in [HOST + "/news", HOST + "/news/2", HOST + "/article/a"]:
        assert requests.count(url) == 1
    assert requests.count(HOST + "/article/b") == 2


def test_discovery_config_rejects_off_host_seeds_or_missing_date_evidence():
    with pytest.raises(ValueError, match="same verified host"):
        source(html_discovery={"additional_listing_urls": ["https://other.example.org/news"]})
    value = source().model_dump()
    value["html_rules"]["listing_date"] = None
    value["html_discovery"]["stop_before_window"] = True
    with pytest.raises(ValueError, match="publication dates"):
        Source.model_validate(value)


def test_discovery_cannot_drop_required_query_filter():
    with pytest.raises(ValueError, match="required listing query"):
        source(
            html_discovery={
                "required_query_parameters": {"topic": "ageing"},
                "drop_query_parameters": ["topic"],
            }
        )


def test_portable_export_retains_every_listing_response(tmp_path):
    from healthlab.signal.backup import export_collection

    store, result, check, _ = run(
        tmp_path / "store",
        {HOST + "/news": card("a") + '<a href="/news/2">next</a>', HOST + "/news/2": card("b")},
    )
    destination = tmp_path / "bundle"
    export_collection(store, result["run_id"], destination)
    copied = RunStore(destination)
    assert (
        copied.load(result["run_id"])["source_checks"][0]["listing_pages"] == check["listing_pages"]
    )
    for page in check["listing_pages"]:
        assert copied.read(page["raw_sha256"]) == store.read(page["raw_sha256"])
