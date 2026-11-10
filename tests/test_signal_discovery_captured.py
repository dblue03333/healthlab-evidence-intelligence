"""Offline discovery regressions on immutable responses from the actual sources."""

import json
from datetime import date
from pathlib import Path

import pytest

from healthlab.signal.contracts import Registry
from healthlab.signal.discovery import discover_listings
from healthlab.store import RunStore
from tests.test_signal_collector_regressions import captured


@pytest.mark.parametrize(
    "name,source_id,count,expected_gap",
    [
        ("moh_discovery_home", "moh_population", 48, "javascript_pagination_unread"),
        ("moh_discovery_archive", "moh_population", 15, "listing_budget_exhausted"),
        ("hcm_discovery_page2", "hcm_health", 30, "listing_budget_exhausted"),
        ("who_discovery_vietnam", "who_ageing", 8, None),
    ],
)
def test_actual_listing_contracts(tmp_path, name, source_id, count, expected_gap):
    raw, url = captured(name)
    registry = Registry.model_validate_json(
        Path("tests/fixtures/signal_html/population_registry_legacy.json").read_bytes()
    )
    original = next(s for s in registry.sources if s.source_id == source_id)
    config = original.model_dump()
    config["url"] = url
    config["html_discovery"].update(additional_listing_urls=[], max_listing_pages=1)
    # These fixtures also preserve the older all-links HTML traversal contract.
    if source_id == "moh_population":
        config["html_discovery"].update(
            wordpress_api_url=None,
            listing_path_patterns=[r"/danh-muc-tin-tuc/[^/]+/(?:page/\d+/)?"],
        )
    if source_id == "hcm_health":
        config["html_discovery"].update(stop_before_window=False, sequential_page_pattern=None)
    source = type(original).model_validate(config)
    store = RunStore(tmp_path)
    digest = store.put(raw)

    class SavedResponse:
        def fetch(self, requested_url, checkpoint, save):
            assert requested_url == url
            return raw, digest

    result = discover_listings(
        store, source, SavedResponse(), {}, lambda: None, mindate=date(2026, 10, 12)
    )
    page = result["listing_pages"][0]
    assert page["candidate_count"] == count
    assert result["discovery_complete"] == (expected_gap is None)
    if expected_gap:
        assert expected_gap in {g["code"] for g in result["listing_gaps"]}
    if source_id == "who_ageing":
        assert page["publication_dates_known"] == count
        assert page["publication_range"] == {"min": "2026-05-28", "max": "2026-10-02"}
        assert page["stop_reason"] == "publication_boundary"
        for evidence in result["date_evidence"].values():
            assert all(e["listing_url"] == url and e["raw_sha256"] == digest for e in evidence)
    if source_id == "hcm_health":
        assert page["publication_dates_known"] == count
        assert all(
            u.startswith("https://medinet.hochiminhcity.gov.vn/") for u in page["pagination_urls"]
        )
        assert any("-p3.aspx" in u for u in page["pagination_urls"])
    assert json.loads(json.dumps(result["listing_pages"])) == result["listing_pages"]
