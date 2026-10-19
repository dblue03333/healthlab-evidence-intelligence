"""Synthetic official HTML extraction and window/lineage cases, not factual evaluation."""

import json
from datetime import date

import httpx
import pytest

from healthlab.models import IngestionError
from healthlab.signal.collection import collect, load_documents
from healthlab.signal.contracts import Registry, Source
from healthlab.signal.html_sources import fetch_html, listing_dates, parse_article
from healthlab.signal.source_probe import MAX_BYTES
from healthlab.store import RunStore

TEXT = (
    "Synthetic healthy ageing report: community care for older adults is described. "
    "This is fixture text only; dates, numbers and relevance require human review."
)


def article(profile="who_news", published="2026-10-09", *, modified="2026-10-10", extra=""):
    body_class = {
        "who_news": "sf-detail-body-wrapper",
        "moh_population": "single-page",
        "hcm_medinet": "ContentODM",
    }[profile]
    meta = (
        f'<meta property="article:published_time" content="{published}T01:00:00Z">'
        if published
        else ""
    )
    return (
        f'<html><head>{meta}<meta property="article:modified_time" content="{modified}"></head>'
        f"<body><nav>Invented menu evidence</nav><h1>Healthy ageing fixture</h1>"
        f'<div class="{body_class}"><p>{TEXT}</p><script>Injected script</script>'
        f"<style>Hidden style</style></div>{extra}<footer>Unrelated footer</footer></body></html>"
    ).encode()


def source(profile="who_news", *, limit=4):
    host = {
        "who_news": "www.who.int",
        "moh_population": "vnpa.moh.gov.vn",
        "hcm_medinet": "medinet.hochiminhcity.gov.vn",
    }[profile]
    return Source(
        source_id="official",
        organization="Synthetic official fixture",
        kind="html",
        html_profile=profile,
        url=f"https://{host}/",
        geography="Fixture geography",
        max_documents=limit,
        usage_policy="Synthetic test data only",
    )


@pytest.mark.parametrize("profile", ["who_news", "moh_population", "hcm_medinet"])
def test_scoped_body_has_no_menu_scripts_or_footer(profile):
    result = parse_article(article(profile), profile)
    assert result["title"] == "Healthy ageing fixture"
    assert result["body"] == TEXT
    assert result["passages"][0]["text"] == TEXT
    assert result["publication_date"] == "2026-10-09"
    assert result["semantic_support"] == "not_evaluated"


def test_jsonld_graph_and_trailing_semicolon_use_publication_not_modified_date():
    metadata = {
        "@graph": [
            {"@type": "NewsArticle", "datePublished": "2025-02-01", "dateModified": "2026-10-10"}
        ]
    }
    extra = '<script type="application/ld+json">' + json.dumps(metadata) + ";</script>"
    result = parse_article(article(published=None, extra=extra), "who_news")
    assert result["publication_date"] == "2025-02-01"
    assert result["modified_date_values"] == ["2026-10-10"]


def test_conflicting_publication_dates_are_not_silently_resolved():
    extra = '<script type="application/ld+json">{"datePublished":"2026-01-01"}</script>'
    with pytest.raises(IngestionError) as error:
        parse_article(article(extra=extra), "who_news")
    assert error.value.code == "html_date_conflict"


def test_dates_in_content_are_not_used_as_missing_publication_date():
    result = parse_article(
        article(published=None).replace(b"community care", b"community care on 2026-10-09"),
        "who_news",
    )
    assert result["publication_date"] is None


@pytest.mark.parametrize(
    "raw,code",
    [
        (b"<h1>Title</h1><main>Unknown structure</main>", "html_article_shape"),
        (b'<h1>Title</h1><div class="single-page">Short</div>', "html_article_sparse"),
    ],
)
def test_dom_changes_and_sparse_media_do_not_become_evidence(raw, code):
    with pytest.raises(IngestionError) as error:
        parse_article(raw, "moh_population")
    assert error.value.code == code


def run_collection(tmp_path, routes, *, selected_source=None):
    store = RunStore(tmp_path)
    config = Registry(schema_version="signal-registry-2", sources=[selected_source or source()])

    def handle(request):
        content = routes.get(request.url.path)
        return httpx.Response(
            200 if content is not None else 404,
            content=content or b"",
            headers={"Content-Type": "text/html"},
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        run = collect(
            store, config, mindate=date(2026, 10, 3), maxdate=date(2026, 10, 10), rss_client=client
        )
    return store, run


def test_article_raw_snapshot_and_extraction_locators_survive_replay(tmp_path):
    raw = article()
    routes = {"/": b'<a href="/news/item/one">Healthy ageing</a>', "/news/item/one": raw}
    store, run = run_collection(tmp_path, routes)
    assert run["status"] == "completed"
    doc = next(iter(load_documents(store, run).values()))
    assert doc.schema_version == "signal-document-2" and doc.coverage == "html_article"
    assert store.read(doc.raw_sha256) == raw
    check = run["source_checks"][0]["article_checks"][0]
    packet = json.loads(store.read(check["extraction_sha256"]))
    assert packet["raw_sha256"] == doc.raw_sha256
    assert packet["passages"][0]["text"] in doc.text
    assert packet["publication_date_evidence"]
    _, second = run_collection(tmp_path, routes)
    assert second["documents"][0]["change"] == "unchanged"
    assert second["documents"][0]["snapshot_id"] == run["documents"][0]["snapshot_id"]


def test_old_articles_with_new_modified_date_stay_outside_current_window(tmp_path):
    routes = {
        "/": b'<a href="/news/item/old">Healthy ageing</a>',
        "/news/item/old": article(published="2025-01-01", modified="2026-10-09"),
    }
    store, run = run_collection(tmp_path, routes)
    assert run["status"] == "completed" and not run["documents"]
    check = run["source_checks"][0]["article_checks"][0]
    assert check["status"] == "outside_window"
    assert store.read(check["raw_sha256"])


def test_missing_publication_date_is_a_visible_gap_not_a_current_signal(tmp_path):
    routes = {
        "/": b'<a href="/news/item/one">Healthy ageing</a>',
        "/news/item/one": article(published=None),
    }
    store, run = run_collection(tmp_path, routes)
    assert run["status"] == "failed" and not run["documents"]
    check = run["source_checks"][0]
    assert check["status"] == "partial"
    assert check["item_errors"][0]["code"] == "html_date_missing"
    assert store.read(check["article_checks"][0]["extraction_sha256"])


def test_fetch_budget_keeps_discovery_backlog_visible(tmp_path):
    listing = (
        b'<a href="/news/item/one">Healthy ageing</a><a href="/news/item/two">Older adults</a>'
    )
    _, run = run_collection(
        tmp_path, {"/": listing, "/news/item/one": article()}, selected_source=source(limit=1)
    )
    check = run["source_checks"][0]
    assert check["limited"] and check["deferred_urls"] == ["https://www.who.int/news/item/two"]


@pytest.mark.parametrize(
    "status,body,content_type,code",
    [
        (302, b"redirect", "text/html", "html_http"),
        (200, b"pdf", "application/pdf", "html_content_type"),
        (200, b"x" * (MAX_BYTES + 1), "text/html", "html_limit"),
    ],
)
def test_fetch_enforces_response_boundaries(tmp_path, status, body, content_type, code):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, content=body, headers={"Content-Type": content_type})
    )
    with httpx.Client(transport=transport) as client:
        with pytest.raises(IngestionError) as error:
            fetch_html(client, "https://www.who.int/", RunStore(tmp_path))
    assert error.value.code == code


def test_profiles_cannot_be_pointed_at_unverified_hosts():
    with pytest.raises(ValueError, match="official host"):
        Source.model_validate(source().model_dump() | {"url": "https://outside.org/"})


def test_html_registry_uses_a_new_version_and_old_rss_registry_still_loads():
    with pytest.raises(ValueError, match="signal-registry-2"):
        Registry(sources=[source()])
    assert Registry(schema_version="signal-registry-2", sources=[source()])
    old = Registry.model_validate(
        {
            "schema_version": "signal-registry-1",
            "sources": [
                {
                    "source_id": "rss",
                    "organization": "Fixture",
                    "kind": "rss",
                    "url": "https://example.org/feed",
                    "usage_policy": "Synthetic fixture only",
                }
            ],
        }
    )
    assert old.sources[0].kind == "rss"


def test_deeply_nested_html_fails_with_a_recordable_source_error():
    with pytest.raises(IngestionError) as error:
        parse_article(b"<div>" * 200, "who_news")
    assert error.value.code == "html_depth_limit"


def test_listing_date_is_bound_to_one_article_not_its_neighbor():
    page = b"""<div class="story"><h1><a href="/news/a-c1780-1.aspx">One</a>
    <span class="time">9/10/2026</span></h1></div>
    <div class="story"><a href="/news/b-c1780-2.aspx">Two</a>
    <span class="time">20/9/2026</span></div>"""
    result = listing_dates(page, source("hcm_medinet"))
    assert result == {
        "https://medinet.hochiminhcity.gov.vn/news/a-c1780-1.aspx": "2026-10-09",
        "https://medinet.hochiminhcity.gov.vn/news/b-c1780-2.aspx": "2026-09-20",
    }


def test_shared_or_conflicting_listing_dates_do_not_get_assigned_to_an_article():
    page = b"""<div><a href="/news/a-c1780-1.aspx">One</a>
    <a href="/news/b-c1780-2.aspx">Two</a><span class="time">9/10/2026</span></div>
    <div><a href="/news/c-c1780-3.aspx">Three</a><span class="time">9/10/2026</span>
    <span class="time">20/9/2026</span></div>"""
    assert listing_dates(page, source("hcm_medinet")) == {}


def test_missing_article_date_can_use_verified_listing_date_with_raw_provenance(tmp_path):
    page = b"""<div class="story"><a href="/news/a-c1780-1.aspx">Healthy ageing</a>
    <span class="time">9/10/2026</span></div>"""
    routes = {"/": page, "/news/a-c1780-1.aspx": article("hcm_medinet", published=None)}
    store, run = run_collection(tmp_path, routes, selected_source=source("hcm_medinet"))
    assert run["status"] == "completed"
    check = run["source_checks"][0]
    packet = json.loads(store.read(check["article_checks"][0]["extraction_sha256"]))
    evidence = packet["publication_date_evidence"][0]
    assert evidence["raw_sha256"] == check["raw_sha256"]
    assert store.read(evidence["raw_sha256"]) == page
    assert next(iter(load_documents(store, run).values())).publication_date == "2026-10-09"


def test_listing_article_date_conflict_keeps_issue_blocked(tmp_path):
    page = b"""<div class="story"><a href="/news/a-c1780-1.aspx">Healthy ageing</a>
    <span class="time">9/10/2026</span></div>"""
    routes = {"/": page, "/news/a-c1780-1.aspx": article("hcm_medinet", published="2026-01-01")}
    _, run = run_collection(tmp_path, routes, selected_source=source("hcm_medinet"))
    assert run["status"] == "failed" and not run["documents"]
    assert run["source_checks"][0]["item_errors"][0]["code"] == "html_date_conflict"
