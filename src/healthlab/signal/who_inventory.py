"""Bounded WHO Vietnam speeches API supplement; HTML discovery remains visible."""

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from healthlab.common import JsonObject
from healthlab.models import IngestionError
from healthlab.signal.contracts import Source
from healthlab.signal.html_adapter import ConfiguredHTMLAdapter
from healthlab.signal.source_http import HTMLFetcher

SITE = "bad9ef31-b8f9-4c48-b98f-345320b3367a"
PROVIDER = "dynamicProvider13"
ENDPOINT = "https://www.who.int/api/news/speeches"
PREFIX = "https://www.who.int/vietnam/news/speeches/speech"
VERSION = "who-vietnam-speeches-1"


def supplement_speeches(
    result: JsonObject,
    source: Source,
    fetcher: HTMLFetcher,
    checkpoint: JsonObject,
    save: Callable,
    *,
    mindate,
    maxdate,
) -> JsonObject:
    from healthlab.signal.discovery import FATAL_STORAGE

    if maxdate is None or maxdate < mindate:
        raise ValueError("WHO inventory requires a complete date window")
    rules = source.html_discovery
    if rules is None:  # Only sources configured for this discovery branch reach here.
        raise ValueError("Source has no HTML discovery rules")
    query = {
        "sf_culture": "en",
        "sf_site": SITE,
        "$filter": f"PublicationDateAndTime ge {mindate}T00:00:00Z and "
        f"PublicationDateAndTime lt {maxdate + timedelta(days=1)}T00:00:00Z",
        "$top": rules.api_page_size,
        "$count": "true",
        "$orderby": "PublicationDateAndTime desc",
        "$select": "Id,Title,ItemDefaultUrl,PublicationDateAndTime,Provider",
    }

    def page_url(number):
        return ENDPOINT + "?" + urlencode(query | {"$skip": (number - 1) * rules.api_page_size})

    candidates = {c["url"]: c for c in result["candidates"]}
    total, seen, seen_urls, previous_day = None, set(), set(), None
    result["listing_budget"] += rules.who_speeches_max_pages
    result["seed_count"] += 1
    result["discovery_method"] = VERSION + ":html_plus_api"
    for number in range(1, rules.who_speeches_max_pages + 1):
        url = page_url(number)
        page: JsonObject = {
            "url": url,
            "status": "running",
            "candidate_urls": [],
            "branch_id": "who_speeches",
        }
        result["listing_pages"].append(page)
        try:
            raw, digest = fetcher.fetch(url, checkpoint, save, response_format="json")
            page["raw_sha256"] = digest
            value = json.loads(raw)
            count, rows = value["@odata.count"], value["value"]
            if (
                type(count) is not int
                or count < 0
                or not isinstance(rows, list)
                or len(rows)
                != min(rules.api_page_size, max(0, count - (number - 1) * rules.api_page_size))
                or (total is not None and total != count)
            ):
                raise ValueError("WHO API count/page length changed or disagrees")
            total = count
            for index, row in enumerate(rows):
                native = str(UUID(row["Id"]))
                path = row["ItemDefaultUrl"]
                if (
                    row["Provider"] != PROVIDER
                    or native in seen
                    or not isinstance(path, str)
                    or not path.startswith("/")
                    or path.startswith("//")
                    or urlsplit(path).query
                    or urlsplit(path).fragment
                    or "/../" in path
                ):
                    raise ValueError("Unverified WHO provider, identity or article path")
                seen.add(native)
                decision = ConfiguredHTMLAdapter(source.resolved_html_rules).classify_url(
                    PREFIX + path, source.url or ""
                )
                if not decision.accepted or decision.url in seen_urls:
                    raise ValueError("WHO API article URL rejected or repeated")
                seen_urls.add(decision.url)
                timestamp = datetime.fromisoformat(
                    row["PublicationDateAndTime"].replace("Z", "+00:00")
                )
                if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
                    raise ValueError("Unverified API calendar-date convention")
                day = timestamp.date()
                if not mindate <= day <= maxdate or (previous_day and day > previous_day):
                    raise ValueError("WHO API date filter/order disagrees")
                previous_day = day
                title = row["Title"]
                if not isinstance(title, str) or not title.strip():
                    raise ValueError("WHO API title missing")
                candidates.setdefault(decision.url, {"url": decision.url, "title": title})
                result["dates"].setdefault(decision.url, set()).add(str(day))
                result["date_evidence"].setdefault(decision.url, []).append(
                    {
                        "locator": f"json.value[{index}].PublicationDateAndTime",
                        "value": str(day),
                        "raw_sha256": digest,
                        "listing_url": url,
                    }
                )
                page["candidate_urls"].append(decision.url)
            page.update(
                status="completed",
                total_posts=total,
                returned_count=len(rows),
                candidate_count=len(rows),
                publication_dates_known=len(rows),
                stop_reason="api_totals_exhausted" if len(seen) == total else "next_api_page",
            )
            result["discovered_link_count"] += len(rows)
            if len(seen) == total:
                break
            if number == rules.who_speeches_max_pages:
                result["listing_gaps"].append(
                    {"url": url, "code": "who_api_inventory_truncated", "branch_id": "who_speeches"}
                )
                result["deferred_listing_urls"].append(page_url(number + 1))
        except IngestionError as exc:
            if exc.code in FATAL_STORAGE:
                raise
            page.update(status="failed", error_code=exc.code)
            result["listing_gaps"].append(
                {"url": url, "code": exc.code, "branch_id": "who_speeches"}
            )
            break
        except (ValueError, KeyError, TypeError, AttributeError):
            page.update(status="failed", error_code="who_api_inventory_invalid")
            result["listing_gaps"].append(
                {"url": url, "code": "who_api_inventory_invalid", "branch_id": "who_speeches"}
            )
            break
    result["candidates"] = list(candidates.values())
    result["discovery_complete"] = not result["listing_gaps"]
    return result


AGEING_URL = "https://www.who.int/health-topics/ageing"
AGEING_QUERY = {
    "sf_provider": "OpenAccessDataProvider",
    "sf_culture": "en",
    "$top": "3",
    "$orderby": "PublicationDateAndTime desc",
    "$select": "Title,ItemDefaultUrl,FormatedDate,NewsType",
    "$filter": "publishingoffices/any(s:s eq 48ddfd0b-6cf8-4498-afc0-e044db8d1c36)",
}


def supplement_ageing_widget(
    result: JsonObject, source: Source, fetcher: HTMLFetcher, checkpoint: JsonObject, save: Callable
) -> JsonObject:
    """Read the landing's declared top-three widget, never execute upstream JS.

    This is a current bounded widget snapshot, not an archive of past membership.
    Missing/changed declarations or malformed payloads remain visible gaps.
    """
    import re
    from urllib.parse import parse_qsl

    from healthlab.signal.discovery import FATAL_STORAGE
    from healthlab.signal.html_adapter import parse_day

    page: JsonObject = {
        "url": AGEING_URL,
        "status": "running",
        "candidate_urls": [],
        "branch_id": "who_ageing",
    }
    result["listing_pages"].append(page)
    result["listing_budget"] += 1
    result["seed_count"] += 1
    result["discovery_method"] = result.get("discovery_method", "html") + ":ageing_widget_1"
    try:
        parent = next(
            p
            for p in result["listing_pages"][:-1]
            if p["url"] == AGEING_URL and p.get("status") == "completed"
        )
        raw = fetcher.store.read(parent["raw_sha256"])
        declarations = set(re.findall(rb'read:\s*"(/api/news/newsitems\?[^"<>]+)"', raw))
        if len(declarations) != 1:
            raise ValueError("Ageing news widget declaration missing or ambiguous")
        path = declarations.pop().decode("utf-8")
        parsed = urlsplit(path)
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        if (
            parsed.path != "/api/news/newsitems"
            or parsed.netloc
            or parsed.fragment
            or len(pairs) != len(AGEING_QUERY)
            or dict(pairs) != AGEING_QUERY
        ):
            raise ValueError("Ageing widget query changed")
        url = "https://www.who.int" + path
        page.update(url=url, declared_by=AGEING_URL, declaration_raw_sha256=parent["raw_sha256"])
        raw, digest = fetcher.fetch(url, checkpoint, save, response_format="json")
        page["raw_sha256"] = digest
        rows = json.loads(raw)["value"]
        if not isinstance(rows, list) or len(rows) != 3:
            raise ValueError("Unverified top-three widget payload")
        additions, seen, previous_day = [], set(), None
        adapter = ConfiguredHTMLAdapter(source.resolved_html_rules)
        for index, row in enumerate(rows):
            path, title = row["ItemDefaultUrl"], row["Title"]
            if (
                not isinstance(path, str)
                or not re.fullmatch(r"/\d{2}-\d{2}-\d{4}-[a-z0-9-]+", path)
                or not isinstance(title, str)
                or not title.strip()
            ):
                raise ValueError("Unverified Ageing article identity")
            decision = adapter.classify_url("https://www.who.int/news/item" + path, AGEING_URL)
            day = parse_day(row["FormatedDate"], "%d %B %Y")
            path_day = datetime.strptime(path[1:11], "%d-%m-%Y").date().isoformat()
            if (
                not decision.accepted
                or decision.url in seen
                or day != path_day
                or (previous_day and day > previous_day)
            ):
                raise ValueError("Ageing widget URL/date/order disagrees")
            previous_day = day
            seen.add(decision.url)
            additions.append((decision.url, title, day, index))
        candidates = {c["url"]: c for c in result["candidates"]}
        for url, title, day, index in additions:
            candidates.setdefault(url, {"url": url, "title": title})
            result["dates"].setdefault(url, set()).add(day)
            result["date_evidence"].setdefault(url, []).append(
                {
                    "locator": f"json.value[{index}].FormatedDate",
                    "value": day,
                    "raw_sha256": digest,
                    "listing_url": page["url"],
                }
            )
            page["candidate_urls"].append(url)
        result["candidates"] = list(candidates.values())
        result["discovered_link_count"] += len(rows)
        page.update(
            status="completed",
            returned_count=3,
            candidate_count=3,
            stop_reason="declared_top_three_snapshot",
            historical_membership_complete=False,
        )
    except IngestionError as exc:
        if exc.code in FATAL_STORAGE:
            raise
        page.update(status="failed", error_code=exc.code)
        result["listing_gaps"].append(
            {"url": page["url"], "code": exc.code, "branch_id": "who_ageing"}
        )
    except (ValueError, KeyError, TypeError, AttributeError, StopIteration):
        page.update(status="failed", error_code="who_ageing_widget_invalid")
        result["listing_gaps"].append(
            {"url": page["url"], "code": "who_ageing_widget_invalid", "branch_id": "who_ageing"}
        )
    result["discovery_complete"] = not result["listing_gaps"]
    return result
