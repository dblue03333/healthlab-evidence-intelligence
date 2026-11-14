"""Coverage audits must distinguish source gaps, extraction failures and evidence limits."""

import gzip
import hashlib
import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from healthlab.data.coverage import compare, identity
from healthlab.data.datasets import build_dataset, gold_records
from healthlab.signal.collection import collect
from healthlab.signal.contracts import Registry
from healthlab.store import RunStore
from scripts.build_coverage_reference import cards
from tests.test_data_pipeline import POLICY, capture
from tests.test_signal_discovery import HOST, card


def reference(tmp_path, records, *, complete=False):
    raw = b"independently reviewed fixture inventory"
    (tmp_path / "raw").write_bytes(raw)
    path = tmp_path / "reference.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "coverage-reference-1",
                "sources": [
                    {
                        "source_id": "fixture",
                        "date_policy": "publication",
                        "method": "synthetic reference",
                        "independence": "fixture",
                        "scope": "test",
                        "mindate": "2026-10-12",
                        "maxdate": "2026-10-16",
                        "complete": complete,
                        "limitations": [] if complete else ["Incomplete reference"],
                        "records": [
                            dict(
                                r,
                                raw_file="raw",
                                raw_sha256=hashlib.sha256(raw).hexdigest(),
                                locator="fixture",
                            )
                            for r in records
                        ],
                    }
                ],
            }
        )
    )
    return path


def test_coverage_does_not_certify_matching_incomplete_reference(tmp_path):
    store = RunStore(tmp_path / "store")
    run, _ = capture(store)
    dataset = build_dataset(store, run["run_id"])["dataset_id"]
    path = reference(tmp_path, [{"url": HOST + "/article/a", "date": "2026-10-14"}])
    result = compare(store, dataset, path)
    assert result["status"] == "needs_review"
    assert result["sources"][0]["matched"] == 1
    assert result["sources"][0]["reference_recall"] is None
    assert result["website_recall"] is None
    (tmp_path / "raw").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        compare(store, dataset, path)


def test_coverage_separates_missing_links_from_unread_articles(tmp_path):
    store = RunStore(tmp_path / "store")
    run, _ = capture(store, card("a"), pages={HOST + "/article/a": 503})
    dataset = build_dataset(store, run["run_id"])["dataset_id"]
    path = reference(
        tmp_path,
        [{"url": HOST + "/article/" + name, "date": "2026-10-14"} for name in ["a", "b"]],
        complete=True,
    )
    result = compare(store, dataset, path)["sources"][0]
    assert result["missing_from_discovery"][0]["url"].endswith("/b")
    assert result["discovered_but_not_included"][0]["outcomes"] == ["failed"]
    assert result["reference_recall"] == 0


def test_outside_window_conflicting_reference_date_does_not_look_complete(tmp_path):
    store = RunStore(tmp_path / "store")
    run, _ = capture(store)
    dataset = build_dataset(store, run["run_id"])["dataset_id"]
    path = reference(
        tmp_path,
        [{"url": HOST + "/article/a", "date": day} for day in ["2026-10-14", "2026-10-24"]],
        complete=True,
    )
    result = compare(store, dataset, path)["sources"][0]
    assert result["reference_date_conflicts"] and not result["reference_complete"]


def test_reference_extractor_does_not_borrow_a_foreign_category_timestamp():
    raw = b'<div><a href="/x-c1780-1.aspx">one</a><article><a href="/other-c999-2.aspx">two</a><span class="time">2/10/2026</span></article></div>'
    assert cards(raw, "https://medinet.hochiminhcity.gov.vn", True) == []
    assert identity(
        "hcm_health", "https://medinet.hochiminhcity.gov.vn/a-c1780-1.aspx"
    ) == identity("hcm_health", "https://medinet.hochiminhcity.gov.vn/chuyen-muc/a-c1780-1.aspx")


def test_captured_hcm_stale_listing_does_not_lose_previously_observed_october_article(tmp_path):
    root = Path(__file__).resolve().parents[1]
    fixtures = root / "tests/fixtures/signal_html"
    src = Registry.model_validate_json((root / "config/signal_sources.json").read_bytes()).sources[
        1
    ]
    # This historical capture isolates category/history behavior; search has separate fixtures.
    src.html_discovery.hcm_search_inventory = False
    store = RunStore(tmp_path)
    entries = json.loads((fixtures / "provenance.json").read_bytes())
    articles = {
        e["source_url"]: gzip.decompress((fixtures / e["file"]).read_bytes())
        for e in entries
        if e["file"] in {"hcm_article.html.gz", "hcm_updated_only_20261026.html.gz"}
    }
    articles["https://medinet.hochiminhcity.gov.vn/tin-tuc-su-kien-c1780-p2.aspx"] = (
        gzip.decompress((fixtures / "hcm_coverage_page2_20261027.html.gz").read_bytes())
    )
    for name in ["hcm_coverage_listing_20261027.html.gz", "hcm_sidebar_listing_20261026.html.gz"]:
        listing = gzip.decompress((fixtures / name).read_bytes())

        def respond(request, listing=listing):
            return httpx.Response(
                200,
                content=listing if str(request.url) == src.url else articles[str(request.url)],
                headers={"content-type": "text/html"},
            )

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            run = collect(
                store,
                Registry(schema_version="signal-registry-3", sources=[src]),
                mindate=date(2026, 10, 7),
                maxdate=date(2026, 10, 13),
                rss_client=client,
                fetch_policy=POLICY,
                remember_candidates=True,
            )
        result = build_dataset(store, run["run_id"])
        assert result["status"] == "ready"
        rows = gold_records(store, result["dataset_id"])
        assert len(rows) == 1 and rows[0]["url"].endswith("-76294.aspx")
    assert run["source_checks"][0]["historical_candidates_revisited"]
