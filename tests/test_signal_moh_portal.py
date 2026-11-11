"""Ministry portal identity, bounded discovery, POST replay and evidence regressions."""

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from healthlab.signal.collection import collect, load_documents
from healthlab.signal.contracts import Registry, Source
from healthlab.signal.moh_portal import API, discover_portal
from healthlab.signal.source_http import FetchPolicy, HTMLFetcher
from healthlab.store import RunStore

ROOT = Path(__file__).resolve().parents[1]
WINDOW = {"mindate": date(2026, 10, 7), "maxdate": date(2026, 10, 13)}
POLICY = FetchPolicy(max_attempts=1, request_interval=0)


def portal_source():
    registry = Registry.model_validate_json((ROOT / "config/signal_sources.json").read_bytes())
    source = registry.sources[0].model_dump()
    source["html_discovery"].update(moh_portal_categories=["8356"], portal_inventory_size=3)
    return Source.model_validate(source)


def item(identifier="12", day="09/10/2026 03:00 PM", **changes):
    return {
        "MA": identifier,
        "THOI_GIAN_XUAT_BAN": day,
        "TIEU_DE": "Chăm sóc người cao tuổi",
        "DON_VI_SAN_XUAT": "BO_YTE",
        "NGON_NGU": "TIENG_VIET",
        "MA_DANH_MUC": "8356",
        **changes,
    }


def listing(records=None, total=None):
    records = records if records is not None else [item(), item("11", "06/10/2026 03:00 PM")]
    return [
        {"MA": "8356", "TOTAL_RECORD": len(records) if total is None else total, "ITEMS": records}
    ]


def detail(**changes):
    return {
        "d": {
            "ma": "12",
            "donViSanXuat": "BO_YTE",
            "thoiGianBatDauDang": "09/10/2026 03:00 PM",
            "tieuDe": "Chăm sóc người cao tuổi",
            "noiDung": "<p>"
            + "Chăm sóc người cao tuổi tại Việt Nam. " * 8
            + "</p><p>Nguồn: Example News</p>",
            **changes,
        }
    }


def run(tmp_path, inventory=None, article=None):
    requests = []

    def handle(req):
        assert req.method == "POST"
        body = json.loads(req.content)
        requests.append((str(req.url), body))
        payload = (
            (listing() if inventory is None else inventory)
            if "MA_DANH_MUC" in body
            else (detail() if article is None else article)
        )
        return httpx.Response(200, json=payload)

    store = RunStore(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = collect(
            store,
            Registry(schema_version="signal-registry-3", sources=[portal_source()]),
            rss_client=client,
            fetch_policy=POLICY,
            **WINDOW,
        )
    return store, result, requests


def test_default_registry_tracks_requested_ministry_not_population_department():
    registry = Registry.model_validate_json((ROOT / "config/signal_sources.json").read_bytes())
    assert [s.source_id for s in registry.sources] == ["moh_portal", "hcm_health", "who_ageing"]
    assert registry.sources[0].url == "https://moh.gov.vn/"
    assert all(s.required and s.html_media_policy == "text_only" for s in registry.sources)


def test_portal_collect_retains_raw_json_dates_attribution_and_offline_replay(tmp_path):
    store, result, requests = run(tmp_path)
    assert result["status"] == "completed" and len(requests) == 2
    doc = next(iter(load_documents(store, result).values()))
    assert doc.url == "https://moh.gov.vn/index.jsp?pageId=5803&aid=12&cid=8356"
    assert doc.publication_date == "2026-10-09" and "Nguồn: Example News" in doc.text
    assert json.loads(store.read(doc.raw_sha256)) == detail()
    check = result["source_checks"][0]
    assert check["article_fetch_count"] == 1 and check["outside_window_without_fetch_count"] == 1
    included = next(c for c in check["article_checks"] if c["status"] == "included")
    extraction = json.loads(store.read(included["extraction_sha256"]))
    assert extraction["passages"][0]["locator"] == "json.d.noiDung"
    assert extraction["publication_date_evidence"][0]["locator"] == "json.d.thoiGianBatDauDang"
    cp = json.loads(store.read(check["checkpoint_sha256"]))
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("Replay made network call"))
    ) as client:
        result = discover_portal(
            portal_source(), HTMLFetcher(client, store, POLICY), cp, lambda: None, **WINDOW
        )
    assert result["discovery_complete"]


@pytest.mark.parametrize("change", ["duplicate", "date", "foreign", "identity", "count", "order"])
def test_bad_inventory_never_claims_coverage(tmp_path, change):
    value = listing()
    if change == "duplicate":
        value[0]["ITEMS"][1] = item()
    if change == "date":
        value[0]["ITEMS"][0]["THOI_GIAN_XUAT_BAN"] = "bad"
    if change == "foreign":
        value[0]["ITEMS"][0]["DON_VI_SAN_XUAT"] = "OTHER"
    if change == "identity":
        value[0]["MA"] = "999"
    if change == "count":
        value[0]["TOTAL_RECORD"] = 3
    if change == "order":
        value[0]["ITEMS"].reverse()
    _, result, _ = run(tmp_path, value)
    assert result["status"] in {"partial", "failed"}
    assert result["source_checks"][0]["listing_gaps"][0]["code"] == "portal_listing_invalid"


def test_full_inventory_without_window_boundary_stays_partial(tmp_path):
    records = [item(str(i)) for i in (12, 11, 10)]
    _, result, _ = run(tmp_path, listing(records, total=9))
    assert result["status"] in {"partial", "failed"}
    assert result["source_checks"][0]["listing_gaps"][0]["code"] == "portal_inventory_truncated"


def test_migrated_null_language_and_child_category_are_supported(tmp_path):
    value = listing([item(NGON_NGU="null", MA_DANH_MUC="8363")])
    _, result, _ = run(tmp_path, value)
    assert result["status"] == "completed"


@pytest.mark.parametrize(
    "changes",
    [{"ma": "99"}, {"thoiGianBatDauDang": "10/10/2026 03:00 PM"}, {"noiDung": "<p>short</p>"}],
)
def test_article_identity_date_conflict_or_sparse_body_cannot_publish_snapshot(tmp_path, changes):
    _, result, _ = run(tmp_path, article=detail(**changes))
    assert result["status"] in {"partial", "failed"} and not result["documents"]


def test_empty_portal_window_is_complete_without_model_or_article_request(tmp_path):
    _, result, requests = run(tmp_path, listing([]))
    assert result["status"] == "completed" and not result["documents"] and len(requests) == 1


def test_post_cache_and_attempt_budget_are_bound_to_payload(tmp_path):
    calls = []
    store = RunStore(tmp_path)
    cp = {"responses": {}, "http_attempts": []}

    def respond(req):
        calls.append(json.loads(req.content))
        return httpx.Response(200, json=calls[-1])

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        fetcher = HTMLFetcher(client, store, POLICY)
        for body in ({"id": 1}, {"id": 2}, {"id": 1}):
            raw, _ = fetcher.fetch(API, cp, lambda: None, response_format="json", request_json=body)
            assert json.loads(raw) == body
    assert calls == [{"id": 1}, {"id": 2}] and len(cp["http_attempts"]) == 2


def test_captured_ministry_inventory_and_article_match_real_portal_contract(tmp_path):
    import gzip
    import hashlib

    from healthlab.signal.moh_portal import fetch_article

    fixtures = ROOT / "tests/fixtures/signal_html"
    provenance = json.loads((fixtures / "provenance.json").read_bytes())
    entries = [
        next(p for p in provenance if p["file"] == name)
        for name in ("moh_portal_inventory.json.gz", "moh_portal_article.json.gz")
    ]

    def handle(req):
        entry = next(e for e in entries if e["source_url"] == str(req.url))
        assert json.loads(req.content) == entry["request_json"]
        raw = gzip.decompress((fixtures / entry["file"]).read_bytes())
        assert hashlib.sha256(raw).hexdigest() == entry["raw_sha256"]
        return httpx.Response(200, content=raw, headers={"content-type": "application/json"})

    source = portal_source()
    source.html_discovery.portal_inventory_size = 1000
    store = RunStore(tmp_path)
    cp = {"responses": {}, "http_attempts": []}
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        fetcher = HTMLFetcher(client, store, POLICY)
        discovery = discover_portal(source, fetcher, cp, lambda: None, **WINDOW)
        assert discovery["discovery_complete"] and len(discovery["candidates"]) == 11
        item = next(c for c in discovery["candidates"] if c["portal_id"] == "170358")
        _, _, parsed = fetch_article(source, item, fetcher, cp, lambda: None)
    assert parsed["publication_date"] == "2026-10-07"
    assert "(Nguồn: suckhoedoisong.vn)" in parsed["body"]
    assert "Tin liên quan" not in parsed["body"]


def test_captured_who_pagination_avoids_alias_and_distant_page_queue(tmp_path):
    import gzip
    import hashlib

    from healthlab.signal.discovery import discover_listings

    fixtures = ROOT / "tests/fixtures/signal_html"
    entries = [
        p
        for p in json.loads((fixtures / "provenance.json").read_bytes())
        if p["file"].startswith("who_rerun_listing_")
    ]
    responses = {p["source_url"]: p for p in entries}
    requests = []

    def handle(req):
        entry = responses[str(req.url)]
        requests.append(str(req.url))
        raw = gzip.decompress((fixtures / entry["file"]).read_bytes())
        assert hashlib.sha256(raw).hexdigest() == entry["raw_sha256"]
        return httpx.Response(200, content=raw, headers={"content-type": "text/html"})

    source = Registry.model_validate_json(
        (ROOT / "config/signal_sources.json").read_bytes()
    ).sources[2]
    # This captured test exercises the historical HTML traversal in isolation.
    # Supplemental API behavior has separate captured and synthetic acceptance.
    source.html_discovery.who_speeches_api = False
    source.html_discovery.who_ageing_widget = False
    store = RunStore(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = discover_listings(
            store,
            source,
            HTMLFetcher(client, store, POLICY),
            {"responses": {}, "http_attempts": []},
            lambda: None,
            **WINDOW,
        )
    assert result["discovery_complete"] and not result["deferred_listing_urls"]
    assert len(requests) == 6
    assert "https://www.who.int/vietnam/news/releases/2" in requests
    assert "https://www.who.int/vietnam/news/releases" not in requests
    assert not any("/releases/3" in u for u in requests)


def test_real_ministry_embedded_h1_keeps_api_title_and_full_body(tmp_path):
    import gzip

    from healthlab.signal.moh_portal import fetch_article

    raw = gzip.decompress(
        (ROOT / "tests/fixtures/signal_html/moh_embedded_heading_20261028.json.gz").read_bytes()
    )
    store = RunStore(tmp_path)
    checkpoint = {"responses": {}, "http_attempts": []}
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=raw, headers={"content-type": "application/json"})
        )
    ) as client:
        _, digest, parsed = fetch_article(
            portal_source(),
            {"url": "https://moh.gov.vn/index.jsp?pageId=5803&aid=170667&cid=8358"},
            HTMLFetcher(client, store, POLICY),
            checkpoint,
            lambda: None,
        )
    assert parsed["title"] == json.loads(raw)["d"]["tieuDe"]
    assert parsed["body"].startswith("Ngày 20/10/2026")
    assert len(parsed["body"]) > 3000 and parsed["publication_date"] == "2026-10-22"
    assert parsed["publication_date_evidence"][0]["raw_sha256"] == digest
