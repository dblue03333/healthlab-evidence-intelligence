"""Public HCMC search recovers articles absent from stale category listings."""

import gzip
from datetime import date
from urllib.parse import parse_qs

import httpx
import pytest

from healthlab.signal.contracts import Registry, Source
from healthlab.signal.hcm_search import supplement_search
from healthlab.signal.source_http import FetchPolicy, HTMLFetcher
from healthlab.store import RunStore
from tests.test_signal_who_inventory import FIXTURES, ROOT, initial


def source():
    return Registry.model_validate_json((ROOT / "config/signal_sources.json").read_bytes()).sources[
        1
    ]


def fixture(number):
    return gzip.decompress((FIXTURES / f"hcm_search_page{number}_20261028.html.gz").read_bytes())


def test_capture_recovers_missing_ids_with_category_filter_boundary_and_cached_replay(tmp_path):
    calls = []

    def respond(request):
        number = 1
        if request.method == "POST":
            assert request.headers["content-type"] == "application/x-www-form-urlencoded"
            form = parse_qs(request.content.decode())
            assert form["__EVENTTARGET"] == ["ctl00$plcall$Search1$grvmn"]
            assert form["__VIEWSTATE"]
            number = int(form["__EVENTARGUMENT"][0].split("$")[1])
        calls.append(number)
        return httpx.Response(200, content=fixture(number), headers={"content-type": "text/html"})

    store = RunStore(tmp_path)
    checkpoint = {"responses": {}, "http_attempts": []}
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        fetcher = HTMLFetcher(client, store, FetchPolicy(max_attempts=1, request_interval=0))
        for _ in range(2):
            result = supplement_search(
                initial(), source(), fetcher, checkpoint, lambda: None, mindate=date(2026, 9, 21)
            )
            assert result["discovery_complete"] and checkpoint["hcm_search_state"]["completed"]
            assert result["listing_pages"][-1]["stop_reason"] == "before_window"
            urls = {r["url"] for r in result["candidates"]}
            assert all("-c1780-" in u for u in urls)
            assert any("-76220.aspx" in u for u in urls)
            assert any("-76279.aspx" in u for u in urls)
            assert all(store.read(e[0]["raw_sha256"]) for e in result["date_evidence"].values())
    assert calls == [1, 2, 3, 4]
    assert checkpoint["hcm_search_state"]["attempts"] == 1
    assert all("__VIEWSTATE" not in str(e) for e in checkpoint["http_attempts"])


def test_old_window_capped_inventory_stays_incomplete(tmp_path):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200, content=fixture(len(calls)), headers={"content-type": "text/html"}
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = supplement_search(
            initial(),
            source(),
            HTMLFetcher(
                client, RunStore(tmp_path), FetchPolicy(max_attempts=1, request_interval=0)
            ),
            {"responses": {}, "http_attempts": []},
            lambda: None,
            mindate=date(2025, 1, 1),
        )
    assert len(calls) == 7 and not result["discovery_complete"]
    assert result["listing_gaps"][-1]["code"] == "hcm_search_inventory_invalid"


def test_interrupted_search_gets_fresh_session_with_lifetime_bound(tmp_path):
    checkpoint = {"responses": {}, "http_attempts": []}
    store = RunStore(tmp_path)
    calls = []

    def first(request):
        calls.append(request.method)
        if request.method == "POST":
            raise httpx.ReadTimeout("interrupted", request=request)
        return httpx.Response(200, content=fixture(1), headers={"content-type": "text/html"})

    with httpx.Client(transport=httpx.MockTransport(first)) as client:
        result = supplement_search(
            initial(),
            source(),
            HTMLFetcher(client, store, FetchPolicy(max_attempts=1, request_interval=0)),
            checkpoint,
            lambda: None,
            mindate=date(2026, 10, 14),
        )
    assert not result["discovery_complete"]
    old = checkpoint["hcm_search_state"]["namespace"]

    def second(request):
        calls.append(request.method)
        return httpx.Response(
            200,
            content=fixture(1 if request.method == "GET" else 2),
            headers={"content-type": "text/html"},
        )

    with httpx.Client(transport=httpx.MockTransport(second)) as client:
        fetcher = HTMLFetcher(client, store, FetchPolicy(max_attempts=1, request_interval=0))
        result = supplement_search(
            initial(), source(), fetcher, checkpoint, lambda: None, mindate=date(2026, 10, 14)
        )
        assert result["discovery_complete"] and checkpoint["hcm_search_state"]["namespace"] != old
        checkpoint["hcm_search_state"].update(completed=False, attempts=3)
        stopped = supplement_search(
            initial(), source(), fetcher, checkpoint, lambda: None, mindate=date(2026, 10, 14)
        )
    assert calls == ["GET", "POST", "GET", "POST"]
    assert stopped["listing_gaps"][-1]["code"] == "hcm_search_session_budget"


def test_changed_search_dom_cannot_be_silent_empty(tmp_path):
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, text="<html>Changed</html>", headers={"content-type": "text/html"}
            )
        )
    ) as client:
        result = supplement_search(
            initial(),
            source(),
            HTMLFetcher(
                client, RunStore(tmp_path), FetchPolicy(max_attempts=1, request_interval=0)
            ),
            {"responses": {}, "http_attempts": []},
            lambda: None,
            mindate=date(2026, 10, 14),
        )
    assert not result["discovery_complete"] and not result["candidates"]


def test_search_cannot_be_enabled_on_other_publishers():
    src = (
        Registry.model_validate_json((ROOT / "config/signal_sources.json").read_bytes())
        .sources[2]
        .model_dump()
    )
    src["html_discovery"]["hcm_search_inventory"] = True
    with pytest.raises(ValueError, match="verified host and category"):
        Source.model_validate(src)
