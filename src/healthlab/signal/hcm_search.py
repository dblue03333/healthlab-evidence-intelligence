"""Bounded public HCMC search supplement, with resumable session audit."""

import re
from datetime import date
from uuid import uuid4

from healthlab.models import IngestionError
from healthlab.signal.html_adapter import ConfiguredHTMLAdapter, parse_day
from healthlab.signal.html_tree import ArticleTree

URL = "https://medinet.hochiminhcity.gov.vn/Search.aspx?key="
TARGET = "ctl00$plcall$Search1$grvmn"
VERSION = "hcm-public-search-1"


def search_page(raw):
    tree = ArticleTree(raw)
    nodes = list(tree.root.nodes())
    totals = [n for n in nodes if n.attrs.get("id") == "plcall_Search1_lblMessage"]
    grids = [n for n in nodes if n.attrs.get("id") == "plcall_Search1_grvmn"]
    if len(totals) != 1 or len(grids) != 1:
        raise ValueError("Search result containers changed")
    match = re.fullmatch(r"Hệ thống tìm thấy (\d+) kết quả", totals[0].text())
    if not match or not 0 <= int(match[1]) <= 100:
        raise ValueError("Search result cap changed")
    rows = []
    for node in grids[0].nodes():
        if "story" not in node.attrs.get("class", "").split():
            continue
        times = [n.text() for n in node.nodes() if "time" in n.attrs.get("class", "").split()]
        links = {
            n.attrs["href"]: n.text()
            for n in node.nodes()
            if n.tag == "a" and re.search(r"-c\d+-\d+\.aspx$", n.attrs.get("href", ""))
        }
        if len(times) != 1 or len(links) != 1:
            raise ValueError("Search row date or identity ambiguous")
        href, title = next(iter(links.items()))
        rows.append((href, title, parse_day(times[0], "%d/%m/%Y")))
    fields = {
        n.attrs["name"]: n.attrs.get("value", "")
        for n in nodes
        if n.tag == "input" and n.attrs.get("type") == "hidden" and n.attrs.get("name")
    }
    if not fields.get("__VIEWSTATE") or not fields.get("__EVENTVALIDATION"):
        raise ValueError("Public paging state missing")
    return int(match[1]), rows, fields


def supplement_search(result, source, fetcher, checkpoint, save, *, mindate):
    from healthlab.signal.discovery import FATAL_STORAGE

    state = checkpoint.setdefault("hcm_search_state", {"attempts": 0})
    # An incomplete live resume needs a fresh GET and same-session POSTs; stale
    # ViewState from another HTTP session can return 500. Never discard old raw.
    if not state.get("completed") and not fetcher.cache_only:
        if state["attempts"] >= 3:
            result["listing_gaps"].append(
                {"url": URL, "code": "hcm_search_session_budget", "branch_id": "hcm_c1780"}
            )
            result["discovery_complete"] = False
            return result
        state.update(attempts=state["attempts"] + 1, namespace="hcm-search-" + uuid4().hex)
        save()
    namespace = state.get("namespace", "hcm-search-unavailable")
    result["listing_budget"] += 7
    result["seed_count"] += 1
    result["discovery_method"] = result.get("discovery_method", "html") + ":" + VERSION
    adapter = ConfiguredHTMLAdapter(source.resolved_html_rules)
    candidates = {c["url"]: c for c in result["candidates"]}
    total, previous_day, seen, fields = None, None, set(), None
    for number in range(1, 8):
        page = {
            "url": URL,
            "page": number,
            "status": "running",
            "branch_id": "hcm_c1780",
            "candidate_urls": [],
        }
        result["listing_pages"].append(page)
        try:
            form = (
                None
                if number == 1
                else fields
                | {
                    "__EVENTTARGET": TARGET,
                    "__EVENTARGUMENT": f"Page${number}",
                    "ctl00$plcall$Search1$txtKeyword": "",
                    "ctl00$plcall$Search1$drpTypenew": "0",
                    "ctl00$plcall$Search1$drpAuthor": "0",
                    "ctl00$plcall$Search1$chk": "2",
                }
            )
            raw, digest = fetcher.fetch(
                URL, checkpoint, save, request_form=form, cache_namespace=namespace
            )
            page["raw_sha256"] = digest
            count, rows, fields = search_page(raw)
            if (
                total is not None
                and total != count
                or len(rows) != min(15, max(0, count - 15 * (number - 1)))
            ):
                raise ValueError("Search total/page count disagrees")
            total = count
            additions = []
            for index, (href, title, day) in enumerate(rows):
                native = re.search(r"-c(\d+)-(\d+)\.aspx$", href).groups()
                if native in seen or previous_day and day > previous_day:
                    raise ValueError("Search identity repeated or order changed")
                seen.add(native)
                previous_day = day
                decision = adapter.classify_url(href, URL)
                if not decision.accepted:
                    continue
                additions.append((decision.url, title, day, index))
            for url, title, day, index in additions:
                candidates.setdefault(url, {"url": url, "title": title or url})
                result["dates"].setdefault(url, set()).add(day)
                result["date_evidence"].setdefault(url, []).append(
                    {
                        "locator": f"#plcall_Search1_grvmn .story[{index}] .time",
                        "value": day,
                        "raw_sha256": digest,
                        "listing_url": URL,
                    }
                )
                page["candidate_urls"].append(url)
            result["discovered_link_count"] += len(additions)
            boundary = bool(previous_day and date.fromisoformat(previous_day) < mindate)
            exhausted = len(seen) == total
            page.update(
                status="completed",
                total_posts=total,
                returned_count=len(rows),
                candidate_count=len(additions),
                stop_reason="before_window"
                if boundary
                else "reported_search_end"
                if exhausted
                else "next_search_page",
                search_cap=100,
                website_inventory_complete=False,
            )
            if boundary or exhausted:
                if exhausted and total == 100 and not boundary:
                    raise ValueError("Capped search did not reach requested window")
                state["completed"] = True
                save()
                break
        except IngestionError as exc:
            if exc.code in FATAL_STORAGE:
                raise
            page.update(status="failed", error_code=exc.code)
            result["listing_gaps"].append({"url": URL, "code": exc.code, "branch_id": "hcm_c1780"})
            break
        except (ValueError, KeyError, TypeError, AttributeError):
            page.update(status="failed", error_code="hcm_search_inventory_invalid")
            result["listing_gaps"].append(
                {"url": URL, "code": "hcm_search_inventory_invalid", "branch_id": "hcm_c1780"}
            )
            break
    result["candidates"] = list(candidates.values())
    result["discovery_complete"] = not result["listing_gaps"]
    return result
