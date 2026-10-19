"""Bounded HTML collection independent of source-specific URL/DOM rules."""

import hashlib
from datetime import date

import httpx

from healthlab.models import IngestionError
from healthlab.signal.contracts import SourceDocument
from healthlab.signal.discovery import discover_listings
from healthlab.signal.html_adapter import PARSER_VERSION, ConfiguredHTMLAdapter
from healthlab.signal.html_rules import HTMLSourceRules, builtin_html_rules
from healthlab.signal.source_http import FetchPolicy, HTMLFetcher
from healthlab.store import json_bytes


def _rules(profile):
    return profile if isinstance(profile, HTMLSourceRules) else builtin_html_rules(profile)


def parse_article(raw, profile, *, url=None):
    return ConfiguredHTMLAdapter(_rules(profile)).parse_article(raw, url=url)


def article_url(url, profile):
    rules = _rules(profile)
    return ConfiguredHTMLAdapter(rules).classify_url(url, "https://" + rules.host + "/").accepted


def listing_dates(raw, source):
    return ConfiguredHTMLAdapter(source.resolved_html_rules).listing_dates(raw, source.url)


def fetch_html(client, url, store):
    checkpoint = {"responses": {}, "http_attempts": []}
    return HTMLFetcher(client, store).fetch(url, checkpoint, lambda: None)


def collect_html(
    store, source, client, *, mindate, maxdate, checkpoint=None, on_checkpoint=None, fetcher=None
):
    identity = hashlib.sha256(
        json_bytes(
            {
                "source": source.model_dump(),
                "mindate": str(mindate),
                "maxdate": str(maxdate),
                "parser_version": PARSER_VERSION,
            }
        )
    ).hexdigest()
    checkpoint = (
        checkpoint
        if checkpoint is not None
        else {
            "version": "html-checkpoint-1",
            "identity": identity,
            "responses": {},
            "http_attempts": [],
            "results": {},
        }
    )
    if checkpoint.get("version") != "html-checkpoint-1" or checkpoint.get("identity") != identity:
        raise IngestionError("html_checkpoint_incompatible", "Checkpoint configuration differs")

    def save():
        if on_checkpoint:
            on_checkpoint(checkpoint)

    fetcher = fetcher or HTMLFetcher(
        client, store, FetchPolicy(), max_bytes=source.html_max_response_bytes
    )
    discovery = discover_listings(
        store, source, fetcher, checkpoint, save, mindate=mindate, maxdate=maxdate
    )
    listing_hash = discovery["raw_sha256"]
    adapter = ConfiguredHTMLAdapter(source.resolved_html_rules)
    candidates, excluded = discovery["candidates"], discovery["excluded"]
    navigation = [item for item in excluded if item["reason"] == "excluded_path"]
    dates = {
        url: next(iter(values)) for url, values in discovery["dates"].items() if len(values) == 1
    }
    rules_hash = store.put(json_bytes(adapter.rules.model_dump()))
    # Discovery is not editorial selection. A broad headline can contain relevant evidence.
    # Stable ordering also makes repeated runs on identical responses reproducible.
    candidates.sort(
        key=lambda item: (
            -date.fromisoformat(dates[item["url"]]).toordinal() if item["url"] in dates else 0,
            item["url"],
        )
    )
    budget = source.max_article_fetches or source.max_documents
    selected = candidates[:budget]
    deferred = [item["url"] for item in candidates[budget:]]
    checks, documents = [], []
    metadata = {
        "raw_sha256": listing_hash,
        "parser_version": PARSER_VERSION,
        "rules_sha256": rules_hash,
        "rule_id": adapter.rules.rule_id,
        "rule_revision": adapter.rules.revision,
        "excluded_url_rules": sorted(excluded, key=lambda item: item["url"]),
        "candidate_count": len(candidates),
        "discovered_link_count": discovery["discovered_link_count"],
        "excluded_navigation": [
            {"url": item["url"], "title": item["title"], "reason": "source_navigation"}
            for item in sorted(navigation, key=lambda item: item["url"])
        ],
        "candidate_urls": [item["url"] for item in candidates],
        "selected_urls": [item["url"] for item in selected],
        "deferred_urls": deferred,
        "fetch_budget": budget,
        "coverage_complete": not deferred and discovery["discovery_complete"],
        "limited": bool(deferred) or not discovery["discovery_complete"],
        "listing_links_truncated": discovery["listing_links_truncated"],
        "coverage": "Configured seed listings and bounded same-host pagination; not exhaustive website coverage",
        "pagination_followed": discovery["pagination_followed"],
        "listing_pages": discovery["listing_pages"],
        "listing_gaps": discovery["listing_gaps"],
        "deferred_listing_urls": discovery["deferred_listing_urls"],
        "listing_budget": discovery["listing_budget"],
        "discovery_method": discovery.get("discovery_method", "html_listings"),
        "html_max_response_bytes": source.html_max_response_bytes,
        "date_filter": "Article publication date only; event/effective dates need review",
        "article_checks": checks,
        "item_errors": [],
        "http_attempts": checkpoint["http_attempts"],
        "request_attempt_count": len(checkpoint["http_attempts"]),
    }
    for item in selected:
        cached = checkpoint["results"].get(item["url"])
        if cached:
            check = dict(cached["check"])
            checks.append(check)
            if cached["document_sha256"]:
                documents.append(
                    SourceDocument.model_validate_json(store.read(cached["document_sha256"]))
                )
            if check["status"] in {"failed", "needs_review"}:
                metadata["item_errors"].append({"url": item["url"], "code": check["error_code"]})
            continue
        check = {"url": item["url"]}
        checks.append(check)
        document = None
        try:
            listing_date = dates.get(item["url"])
            if (
                source.html_discovery
                and source.html_discovery.skip_outside_window
                and listing_date
                and not mindate <= date.fromisoformat(listing_date) <= maxdate
            ):
                check.update(
                    status="outside_window",
                    publication_date=listing_date,
                    date_status="publication_known",
                    date_basis="saved_listing_only",
                    content_coverage="not_fetched_outside_window",
                    publication_date_evidence=discovery["date_evidence"].get(item["url"], []),
                )
                continue
            raw, raw_hash = fetcher.fetch(item["url"], checkpoint, save)
            check["raw_sha256"] = raw_hash
            parsed = adapter.parse_article(raw, url=item["url"])
            parsed.update(url=item["url"], raw_sha256=raw_hash, rules_sha256=rules_hash)
            listing_date = dates.get(item["url"])
            date_conflict = len(discovery["dates"].get(item["url"], set())) > 1
            if listing_date:
                if parsed["publication_date"] and parsed["publication_date"] != listing_date:
                    date_conflict = True
                else:
                    parsed["publication_date"] = listing_date
                    parsed["date_status"] = "publication_known"
            parsed["publication_date_evidence"].extend(
                discovery["date_evidence"].get(item["url"], [])
            )
            check["extraction_sha256"] = store.put(json_bytes(parsed))
            published = parsed["publication_date"]
            check["publication_date"] = published
            check["date_status"] = parsed["date_status"]
            check["modified_date_values"] = parsed["modified_date_values"]
            check["content_kind"] = parsed["content_kind"]
            check["content_coverage"] = parsed["content_coverage"]
            check["media"] = parsed["media"]
            check["topic_hint"] = parsed["topic_hint"]
            if date_conflict:
                raise IngestionError("html_date_conflict", "Listing and article dates disagree")
            if published is None:
                check.update(status="needs_review", error_code="html_date_missing")
                metadata["item_errors"].append({"url": item["url"], "code": "html_date_missing"})
                continue
            if not mindate <= date.fromisoformat(published) <= maxdate:
                check["status"] = "outside_window"
                continue
            if parsed["requires_media_review"]:
                check.update(status="needs_review", error_code="html_article_media")
                metadata["item_errors"].append(
                    {
                        "url": item["url"],
                        "code": "html_article_media",
                        "content_kind": parsed["content_kind"],
                    }
                )
                continue
            # Title-only or ambiguous relevance is not silently accepted as a relevant signal.
            # Keep the article for the next triage/review stage, with its hint in article_checks.
            document = SourceDocument(
                schema_version="signal-document-2",
                document_id="html:" + hashlib.sha256(item["url"].encode()).hexdigest(),
                source_id=source.source_id,
                url=item["url"],
                title=parsed["title"],
                publication_date=published,
                geography=source.geography,
                text=parsed["title"] + "\n\n" + parsed["body"],
                coverage="html_article",
                raw_sha256=raw_hash,
            )
            documents.append(document)
            check["status"] = "included"
        except (IngestionError, ValueError) as exc:
            if isinstance(exc, IngestionError) and exc.code in {
                "cache_corrupt",
                "cache_missing",
                "invalid_reference",
                "cloud_object_io",
                "cloud_state_io",
                "invalid_run",
            }:
                raise
            code = exc.code if isinstance(exc, IngestionError) else "html_article_invalid"
            check.update(status="failed", error_code=code)
            metadata["item_errors"].append({"url": item["url"], "code": code})
        except httpx.HTTPError as exc:
            check.update(status="failed", error_code=type(exc).__name__)
            metadata["item_errors"].append({"url": item["url"], "code": type(exc).__name__})
        finally:
            if check.get("status") in {"included", "outside_window", "needs_review", "failed"}:
                checkpoint["results"][item["url"]] = {
                    "check": check,
                    "document_sha256": store.put(json_bytes(document.model_dump()))
                    if document
                    else None,
                }
                save()
    metadata["request_attempt_count"] = len(checkpoint["http_attempts"])
    selected_urls = {item["url"] for item in selected}
    metadata["article_fetch_count"] = len(
        {a["url"] for a in checkpoint["http_attempts"] if a["url"] in selected_urls}
    )
    metadata["outside_window_without_fetch_count"] = sum(
        c.get("content_coverage") == "not_fetched_outside_window" for c in checks
    )
    if not selected and not (
        discovery.get("discovery_method") == "wordpress_rest" and discovery["discovery_complete"]
    ):
        metadata["item_errors"].append({"code": "html_no_article_candidates"})
    metadata["all_candidates_checked"] = not deferred
    metadata["coverage_complete"] = metadata["coverage_complete"] and not metadata["item_errors"]
    metadata["website_coverage_complete"] = False
    metadata["checked_count"] = len(checks)
    metadata["counts"] = {
        status: sum(check.get("status") == status for check in checks)
        for status in ("included", "outside_window", "needs_review", "failed")
    }
    return documents, metadata
