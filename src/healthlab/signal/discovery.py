"""Audited finite listing traversal, independent of editorial topic selection."""

import json
import re
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from healthlab.models import IngestionError
from healthlab.signal.html_adapter import ConfiguredHTMLAdapter
from healthlab.signal.html_tree import ArticleTree
from healthlab.signal.source_probe import ProbeTarget, page_inventory

FATAL_STORAGE = {
    "cache_corrupt",
    "cache_missing",
    "invalid_reference",
    "cloud_object_io",
    "cloud_state_io",
    "invalid_run",
}


def listing_link(href, base, host, rules):
    """Normalize only allowed listing paths. Same-host HTTP upgrade is opt-in."""
    try:
        parsed = urlsplit(urljoin(base, href))
        if (
            parsed.hostname != host
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or any(c in href for c in "{}\\")
            or any(c.isspace() for c in href)
        ):
            return None
        if parsed.scheme == "http" and rules.upgrade_same_host_http:
            parsed = parsed._replace(scheme="https")
        if parsed.scheme != "https" or parsed.fragment:
            return None
        if not any(re.fullmatch(p, parsed.path) for p in rules.listing_path_patterns):
            return None
        query = parse_qs(parsed.query, keep_blank_values=True)
        if any(
            len(query.get(key, [])) != 1 or not re.fullmatch(pattern, query[key][0])
            for key, pattern in rules.required_query_parameters.items()
        ):
            return None
        kept = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key not in rules.drop_query_parameters
        ]
        return urlunsplit(("https", host, parsed.path, urlencode(sorted(kept)), ""))
    except ValueError:
        return None


def _in_pager(node, selector):
    if selector is None:
        return True
    while node is not None:
        if selector.matches(node):
            return True
        node = node.parent
    return False


def discover_listings(store, source, fetcher, checkpoint, save, *, mindate, maxdate=None):
    if source.html_discovery and source.html_discovery.wordpress_api_url:
        from healthlab.signal.wordpress_discovery import discover_wordpress

        return discover_wordpress(
            source, fetcher, checkpoint, save, mindate=mindate, maxdate=maxdate
        )
    adapter = ConfiguredHTMLAdapter(source.resolved_html_rules)
    rules = source.html_discovery
    queue = list(dict.fromkeys([source.url] + (rules.additional_listing_urls if rules else [])))
    seed_count = len(queue)
    seeds = set(queue)
    budget = rules.max_listing_pages if rules else 1
    queued, visited = set(queue), set()
    pages, gaps, excluded, candidates, dates = [], [], [], {}, {}
    date_evidence = {}
    primary_hash = None
    total_links = 0
    fingerprints = {}
    while queue and len(pages) < budget:
        url = queue.pop(0)
        visited.add(url)
        page = {"url": url, "status": "running", "candidate_urls": []}
        pages.append(page)
        try:
            raw, digest = fetcher.fetch(url, checkpoint, save)
            page["raw_sha256"] = digest
            if url == source.url:
                primary_hash = digest
        except IngestionError as exc:
            if exc.code in FATAL_STORAGE or not rules:
                raise
            page.update(status="failed", error_code=exc.code)
            gaps.append({"url": url, "code": exc.code})
            continue
        tree = ArticleTree(raw)
        inventory = page_inventory(
            raw, ProbeTarget(source_id=source.source_id, organization=source.organization, url=url)
        )
        page["links_truncated"] = inventory["links_truncated"]
        total_links += len(inventory["candidates"])
        if inventory["links_truncated"]:
            gaps.append({"url": url, "code": "listing_links_truncated"})
        local_dates = adapter.listing_dates(raw, url)
        for item in inventory["candidates"]:
            decision = adapter.classify_url(item["url"], url)
            if not decision.accepted:
                excluded.append(
                    {
                        "url": item["url"],
                        "title": item["title"],
                        "reason": decision.reason,
                        "listing_url": url,
                    }
                )
                continue
            page["candidate_urls"].append(decision.url)
            if decision.url in candidates:
                excluded.append(
                    {
                        "url": item["url"],
                        "title": item["title"],
                        "reason": "normalized_duplicate",
                        "normalized_url": decision.url,
                        "listing_url": url,
                    }
                )
            else:
                candidates[decision.url] = item | {"url": decision.url}
            if decision.url in local_dates:
                value = local_dates[decision.url]
                dates.setdefault(decision.url, set()).add(value)
                date_evidence.setdefault(decision.url, []).append(
                    {
                        "locator": "listing:single-article-container "
                        + adapter.rules.listing_date.selector.locator,
                        "value": value,
                        "raw_sha256": digest,
                        "listing_url": url,
                    }
                )
        page.update(status="completed", candidate_count=len(page["candidate_urls"]))
        known = [local_dates[u] for u in page["candidate_urls"] if u in local_dates]
        page["publication_range"] = {"min": min(known), "max": max(known)} if known else None
        page["publication_dates_known"] = len(known)
        if rules and not page["candidate_urls"]:
            text = tree.root.text().casefold()
            code = (
                "listing_access_challenge"
                if any(
                    t in text for t in ["support id", "testing whether you are a human", "captcha"]
                )
                else "listing_no_article_candidates"
            )
            gaps.append({"url": url, "code": code})
        # A configured chronological boundary applies only to pagination from
        # this page; the other seed branches are always checked independently.
        cutoff = bool(
            rules
            and rules.stop_before_window
            and known
            and len(known) == len(page["candidate_urls"])
            and max(known) < str(mindate)
        )
        page["stop_reason"] = "publication_boundary" if cutoff else "links_exhausted"
        if not rules:
            continue
        fingerprint = tuple(sorted(page["candidate_urls"]))
        repeated = bool(fingerprint and fingerprint in fingerprints)
        if repeated:
            page.update(
                stop_reason="repeated_listing", duplicate_listing_url=fingerprints[fingerprint]
            )
            # A repeated article set can be a stale/ignored page parameter.
            gaps.append({"url": url, "code": "listing_repeated_content"})
        elif fingerprint:
            fingerprints[fingerprint] = url
        follow = []
        js_seen = False
        for node in tree.root.nodes():
            relay = node.attrs.get("data-flatsome-relay")
            if relay:
                try:
                    value = json.loads(relay)
                    js_seen |= int(value["totalPages"]) > int(value["currentPage"])
                except (ValueError, TypeError, KeyError):
                    js_seen = True
            if node.tag != "a" or not _in_pager(node, rules.pagination_selector):
                continue
            href = node.attrs.get("href", "")
            if re.fullmatch(r"#/page/\d+/?", href):
                js_seen = True
                continue
            candidate = listing_link(href, url, adapter.rules.host, rules)
            if candidate and candidate != url:
                follow.append(candidate)
        page["pagination_urls"] = list(dict.fromkeys(follow))
        if rules.sequential_page_pattern:
            pattern = re.compile(rules.sequential_page_pattern)
            current = pattern.fullmatch(urlsplit(url).path)
            if current is None:
                gaps.append({"url": url, "code": "listing_page_number_unknown"})
                page["pagination_urls"] = []
            else:
                number = int(current["page"] or 1)
                numbered = [
                    (u, pattern.fullmatch(urlsplit(u).path)) for u in page["pagination_urls"]
                ]
                numbered = [(u, int(m["page"] or 1)) for u, m in numbered if m]
                page["observed_pagination_urls"] = page["pagination_urls"]
                page["pagination_urls"] = [u for u, n in numbered if n == number + 1][:1]
                if (
                    not page["pagination_urls"]
                    and any(n > number for _, n in numbered)
                    and not cutoff
                ):
                    gaps.append({"url": url, "code": "listing_next_page_missing"})
        if js_seen and not cutoff:
            gaps.append({"url": url, "code": "javascript_pagination_unread"})
        if cutoff or repeated:
            continue
        for candidate in page["pagination_urls"]:
            if candidate not in queued:
                queue.append(candidate)
                queued.add(candidate)

        # Seed branches first, then nearby page numbers before distant "last"
        # links. Never synthesize a URL from a page number or fragment.
        def priority(candidate):
            numbers = re.findall(r"\d+", urlsplit(candidate).path)
            return (candidate not in seeds, int(numbers[-1]) if numbers else 0)

        queue.sort(key=priority)
    if queue:
        gaps.append({"code": "listing_budget_exhausted", "remaining_count": len(queue)})
    return {
        "raw_sha256": primary_hash,
        "candidates": list(candidates.values()),
        "excluded": excluded,
        "dates": dates,
        "date_evidence": date_evidence,
        "listing_pages": pages,
        "listing_gaps": gaps,
        "deferred_listing_urls": queue,
        "listing_budget": budget,
        "seed_count": seed_count,
        "discovered_link_count": total_links,
        "listing_links_truncated": any(p.get("links_truncated") for p in pages),
        "pagination_followed": any(
            p["url"] not in [source.url] + (rules.additional_listing_urls if rules else [])
            for p in pages
        ),
        "discovery_complete": not gaps,
    }
