"""Real captured DOMs plus boundary and repeat-run regression checks; all offline."""

import gzip
import hashlib
import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from healthlab.signal.collection import collect, load_documents
from healthlab.signal.contracts import Registry
from healthlab.signal.html_sources import article_url, parse_article
from healthlab.signal.source_probe import ProbeTarget, page_inventory
from healthlab.store import RunStore
from tests.test_signal_html_sources import TEXT, article, source

FIXTURES = Path(__file__).parent / "fixtures" / "signal_html"


def captured(name):
    filename = name + ".html.gz"
    record = next(
        r for r in json.loads((FIXTURES / "provenance.json").read_bytes()) if r["file"] == filename
    )
    raw = gzip.decompress((FIXTURES / filename).read_bytes())
    assert hashlib.sha256(raw).hexdigest() == record["raw_sha256"]
    return raw, record["source_url"]


def test_actual_hcm_intro_is_preserved_and_update_is_not_publication():
    raw, url = captured("hcm_article")
    parsed = parse_article(raw, "hcm_medinet", url=url)
    assert parsed["body"].startswith("Đến ngày 06/10/2026, phường Phước Thắng")
    assert "Đối với người cao tuổi, người khó đi lại" in parsed["body"]
    assert [p["locator"] for p in parsed["passages"]] == [".contentNew .sapodetail", ".ContentODM"]
    assert parsed["body"] == "\n\n".join(p["text"] for p in parsed["passages"])
    assert parsed["publication_date"] is None
    assert parsed["date_status"] == "update_only"
    assert parsed["modified_date_values"] == ["2026-10-07"]
    assert not parsed["publication_date_evidence"]
    assert "Lượt đọc" not in parsed["body"]


def test_hcm_does_not_borrow_another_articles_intro_or_update():
    raw = (
        '<aside><span class="sapodetail">Wrong introduction</span>'
        '<div class="time">Cập nhật: 12:00, 10/10/2026</div></aside>'
        '<div class="contentNew"><div class="time">Cập nhật: 11:00, 7/10/2026</div>'
        '<h1>Scoped title</h1><span class="sapodetail">Correct introduction</span>'
        f'<div class="ContentODM"><p>{TEXT}</p></div></div>'
    ).encode()
    parsed = parse_article(raw, "hcm_medinet")
    assert "Wrong" not in parsed["body"]
    assert parsed["body"].startswith("Correct introduction")
    assert parsed["modified_date_values"] == ["2026-10-07"]
    assert parsed["publication_date"] is None


@pytest.mark.parametrize(
    "name,kind", [("moh_video", "video"), ("moh_pdf", "pdf"), ("moh_image", "image")]
)
def test_real_media_keeps_date_and_inventory_without_inventing_text(name, kind):
    raw, url = captured(name)
    parsed = parse_article(raw, "moh_population", url=url)
    assert parsed["requires_media_review"]
    assert parsed["content_kind"] == kind
    assert parsed["content_coverage"] == "media_unread"
    assert parsed["publication_date"]
    assert any(m["kind"] == kind and m["url"].startswith("https://") for m in parsed["media"])
    if kind == "pdf":
        assert any(m["url"].endswith(".pdf") for m in parsed["media"])
        assert not any("docs.google.com/viewer" in m["url"] for m in parsed["media"])
    if kind == "image":
        assert parsed["body"] == "" and parsed["passages"] == []


def test_real_population_listing_excludes_navigation_but_keeps_broad_headline():
    raw, url = captured("moh_listing")
    inv = page_inventory(raw, ProbeTarget(source_id="moh", organization="MOH", url=url))
    shaped = [r for r in inv["candidates"] if r["url"].count("/") == 4]
    candidates = [r for r in inv["candidates"] if article_url(r["url"], "moh_population")]
    assert len(shaped) == 55
    assert len(candidates) == 44
    assert not any(r["url"].endswith("/gioi-thieu/") for r in candidates)
    assert any("ninh-binh-dong-bo" in r["url"] for r in candidates)
    article_raw, article_link = captured("moh_broad_headline")
    parsed = parse_article(article_raw, "moh_population", url=article_link)
    assert "tỷ lệ người cao tuổi" in parsed["body"]
    assert parsed["publication_date"] == "2026-09-24"


def test_actual_who_metadata_still_extracts():
    raw, url = captured("who_article")
    parsed = parse_article(raw, "who_news", url=url)
    assert parsed["publication_date"] == "2026-10-01"
    assert "Integrated Care for Older People" in parsed["body"]
    assert not parsed["requires_media_review"]


def test_related_jsonld_entity_does_not_supply_a_publication_date():
    records = {
        "@graph": [
            {"@type": "Organization", "datePublished": "2026-10-10"},
            {
                "@type": "NewsArticle",
                "url": "https://www.who.int/news/item/other",
                "datePublished": "2026-10-09",
            },
        ]
    }
    raw = article(
        published=None,
        extra='<script type="application/ld+json">' + json.dumps(records) + "</script>",
    )
    assert (
        parse_article(raw, "who_news", url="https://www.who.int/news/item/current")[
            "publication_date"
        ]
        is None
    )


def collect_routes(tmp_path, routes, configured):
    requests = []

    def handle(request):
        requests.append(request.url.path)
        assert request.url.path in routes, "Unexpected/off-scope request"
        return httpx.Response(
            200, content=routes[request.url.path], headers={"Content-Type": "text/html"}
        )

    store = RunStore(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        run = collect(
            store,
            Registry(schema_version="signal-registry-2", sources=[configured]),
            mindate=date(2026, 10, 3),
            maxdate=date(2026, 10, 10),
            rss_client=client,
        )
    return store, run, requests


def test_fetch_all_configured_candidates_before_topic_or_window_selection(tmp_path):
    listing = b'<a href="/old/">Older adults</a><a href="/broad/">Community programme</a>'
    configured = source("moh_population", limit=1).model_copy(update={"max_article_fetches": 10})
    routes = {
        "/": listing,
        "/old/": article("moh_population", published="2025-01-01"),
        "/broad/": article("moh_population"),
    }
    store, first, _ = collect_routes(tmp_path, routes, configured)
    assert first["status"] == "completed"
    assert len(first["documents"]) == 1
    doc = next(iter(load_documents(store, first).values()))
    assert doc.url.endswith("/broad/")
    check = first["source_checks"][0]
    assert check["coverage_complete"] and not check["deferred_urls"]
    # Same source content, including a changed listing order, gives the same article order/IDs.
    routes["/"] = b'<a href="/broad/">Community programme</a><a href="/old/">Older adults</a>'
    _, second, _ = collect_routes(tmp_path, routes, configured)
    assert second["documents"][0]["change"] == "unchanged"
    assert second["documents"][0]["snapshot_id"] == first["documents"][0]["snapshot_id"]
    assert [c["url"] for c in second["source_checks"][0]["article_checks"]] == [
        c["url"] for c in check["article_checks"]
    ]


def test_budget_backlog_marks_required_source_partial(tmp_path):
    routes = {
        "/": b'<a href="/news/item/a">Report</a><a href="/news/item/b">Report</a>',
        "/news/item/a": article(),
    }
    _, run, _ = collect_routes(tmp_path, routes, source(limit=1))
    check = run["source_checks"][0]
    assert run["status"] == check["status"] == "partial"
    assert not check["coverage_complete"]
    assert check["deferred_urls"] == ["https://www.who.int/news/item/b"]


def test_media_in_window_is_persisted_and_blocks_required_source(tmp_path):
    raw, _ = captured("moh_video")
    routes = {"/": b'<a href="/video/">Healthy ageing</a>', "/video/": raw}
    store, run, _ = collect_routes(tmp_path, routes, source("moh_population"))
    check = run["source_checks"][0]
    assert check["status"] == "partial" and not run["documents"]
    item = check["article_checks"][0]
    assert item["status"] == "needs_review" and item["content_kind"] == "video"
    packet = json.loads(store.read(item["extraction_sha256"]))
    assert packet["raw_sha256"] == item["raw_sha256"]
    assert packet["publication_date"] == "2026-10-10"
    assert packet["media"]


def test_intro_edit_changes_snapshot_but_identical_replay_does_not(tmp_path):
    def page(lead):
        return (
            '<meta property="article:published_time" content="2026-10-09">'
            '<div class="contentNew"><h1>Programme</h1>'
            f'<span class="sapodetail">{lead}</span><div class="ContentODM">'
            f"{TEXT}</div></div>"
        ).encode()

    configured = source("hcm_medinet")
    routes = {
        "/": b'<a href="/news/a-c1780-1.aspx">Programme</a>',
        "/news/a-c1780-1.aspx": page("Original introduction"),
    }
    _, first, _ = collect_routes(tmp_path, routes, configured)
    _, second, _ = collect_routes(tmp_path, routes, configured)
    assert second["documents"][0]["change"] == "unchanged"
    assert second["documents"][0]["snapshot_id"] == first["documents"][0]["snapshot_id"]
    routes["/news/a-c1780-1.aspx"] = page("Updated introduction")
    store, third, _ = collect_routes(tmp_path, routes, configured)
    assert third["documents"][0]["change"] == "updated"
    assert third["documents"][0]["previous_snapshot_id"] == first["documents"][0]["snapshot_id"]
    assert "Updated introduction" in next(iter(load_documents(store, third).values())).text
    # Previously stored content is immutable.
    assert "Original introduction" in next(iter(load_documents(store, first).values())).text
