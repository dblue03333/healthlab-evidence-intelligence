"""Public WordPress posts discovery by publication window; article bodies stay HTML."""

import json
from collections.abc import Callable
from datetime import datetime, time, timedelta
from urllib.parse import urlencode

from healthlab.common import JsonObject
from healthlab.models import IngestionError
from healthlab.signal.contracts import Source
from healthlab.signal.discovery import FATAL_STORAGE
from healthlab.signal.html_adapter import ConfiguredHTMLAdapter
from healthlab.signal.html_tree import ArticleTree
from healthlab.signal.source_http import HTMLFetcher


def discover_wordpress(
    source: Source,
    fetcher: HTMLFetcher,
    checkpoint: JsonObject,
    save: Callable,
    *,
    mindate,
    maxdate,
) -> JsonObject:
    rules = source.html_discovery
    if rules is None:  # Only sources configured for this discovery branch reach here.
        raise ValueError("Source has no HTML discovery rules")
    adapter = ConfiguredHTMLAdapter(source.resolved_html_rules)
    if maxdate is None or maxdate < mindate:
        raise ValueError("WordPress discovery requires a complete publication window")
    # WordPress filters its site-local publication time, not modified time.
    # Expand the lower bound by one second so midnight publications are retained.
    query = {
        "after": (datetime.combine(mindate, time()) - timedelta(seconds=1)).isoformat(),
        "before": datetime.combine(maxdate + timedelta(days=1), time()).isoformat(),
        "per_page": rules.api_page_size,
        "orderby": "date",
        "order": "asc",
        "status": "publish",
        "_fields": "id,date,date_gmt,link,title",
    }

    def page_url(number):
        return rules.wordpress_api_url + "?" + urlencode(query | {"page": number})

    pages, gaps, candidates, dates, evidence, excluded = [], [], {}, {}, {}, []
    totals = None
    record_ids = set()
    records_seen = 0
    deferred = []
    for number in range(1, rules.max_listing_pages + 1):
        url = page_url(number)
        page: JsonObject = {"url": url, "status": "running", "candidate_urls": []}
        pages.append(page)
        try:
            raw, digest = fetcher.fetch(url, checkpoint, save, response_format="json")
            page["raw_sha256"] = digest
            records = json.loads(raw)
            headers = checkpoint["responses"][url].get("headers", {})
            total, total_pages = int(headers["x-wp-total"]), int(headers["x-wp-totalpages"])
            if (
                not isinstance(records, list)
                or total < 0
                or total_pages != (total + rules.api_page_size - 1) // rules.api_page_size
                or len(records)
                != min(rules.api_page_size, max(0, total - (number - 1) * rules.api_page_size))
            ):
                raise ValueError("API totals/page length disagree")
            if totals is not None and totals != (total, total_pages):
                raise ValueError("API totals changed during traversal")
            totals = (total, total_pages)
            page.update(total_posts=total, total_pages=total_pages)
            for index, record in enumerate(records):
                records_seen += 1
                if (
                    not isinstance(record["id"], int)
                    or record["id"] <= 0
                    or record["id"] in record_ids
                ):
                    raise ValueError("Repeated or invalid API record")
                record_ids.add(record["id"])
                published = datetime.fromisoformat(record["date"]).date()
                decision = adapter.classify_url(record["link"], url)
                if not decision.accepted:
                    gaps.append(
                        {"url": url, "code": "api_article_url_rejected", "record_id": record["id"]}
                    )
                    excluded.append(
                        {"url": record["link"], "reason": decision.reason, "listing_url": url}
                    )
                    continue
                article_url = decision.url
                if article_url in candidates:
                    raise ValueError("Duplicate article URL across API records")
                title = ArticleTree(record["title"]["rendered"].encode()).root.text()
                if not title:
                    raise ValueError("Missing API title")
                candidates[article_url] = {"url": article_url, "title": title}
                dates[article_url] = {published.isoformat()}
                evidence[article_url] = [
                    {
                        "locator": f"json[{index}].date",
                        "value": published.isoformat(),
                        "raw_sha256": digest,
                        "listing_url": url,
                    }
                ]
                page["candidate_urls"].append(article_url)
            page.update(
                status="completed",
                candidate_count=len(page["candidate_urls"]),
                publication_dates_known=len(page["candidate_urls"]),
                stop_reason="api_totals_exhausted" if number >= total_pages else "next_api_page",
            )
            known = [next(iter(dates[u])) for u in page["candidate_urls"]]
            page["publication_range"] = {"min": min(known), "max": max(known)} if known else None
            if number >= total_pages:
                if records_seen != total:
                    raise ValueError("Incomplete API inventory")
                break
            if number == rules.max_listing_pages:
                deferred = [page_url(number + 1)]
                gaps.append(
                    {"code": "listing_budget_exhausted", "remaining_count": total_pages - number}
                )
        except IngestionError as exc:
            if exc.code in FATAL_STORAGE:
                raise
            page.update(status="failed", error_code=exc.code)
            gaps.append({"url": url, "code": exc.code})
            break
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            page.update(status="failed", error_code="api_listing_invalid")
            gaps.append({"url": url, "code": "api_listing_invalid", "detail": str(exc)[:200]})
            break
    return {
        "raw_sha256": pages[0].get("raw_sha256"),
        "candidates": list(candidates.values()),
        "excluded": excluded,
        "dates": dates,
        "date_evidence": evidence,
        "listing_pages": pages,
        "listing_gaps": gaps,
        "deferred_listing_urls": deferred,
        "listing_budget": rules.max_listing_pages,
        "seed_count": 1,
        "discovered_link_count": records_seen,
        "listing_links_truncated": False,
        "pagination_followed": len(pages) > 1,
        "discovery_complete": not gaps,
        "discovery_method": "wordpress_rest",
    }
