"""Bounded HTML collection independent of source-specific URL/DOM rules."""

import hashlib
from datetime import date
from typing import Any

import httpx

from healthlab.common import JsonObject
from healthlab.models import IngestionError
from healthlab.signal.contracts import Source, SourceDocument
from healthlab.signal.html_adapter import PARSER_VERSION, ConfiguredHTMLAdapter, iso_day
from healthlab.signal.html_rules import HTMLSourceRules, builtin_html_rules
from healthlab.signal.source_adapters import source_adapter_for
from healthlab.signal.source_http import FetchPolicy, HTMLFetcher
from healthlab.signal.storage import CollectionStorage
from healthlab.store import RunStore, json_bytes


def _rules(profile: str) -> HTMLSourceRules:
    return profile if isinstance(profile, HTMLSourceRules) else builtin_html_rules(profile)


def parse_article(raw: bytes, profile: str, *, url=None) -> JsonObject:
    return ConfiguredHTMLAdapter(_rules(profile)).parse_article(raw, url=url)


def article_url(url: str, profile: str) -> bool:
    rules = _rules(profile)
    return ConfiguredHTMLAdapter(rules).classify_url(url, "https://" + rules.host + "/").accepted


def listing_dates(raw: bytes, source: Source) -> dict[str, str]:
    # urljoin treats a missing base like an empty one.
    return ConfiguredHTMLAdapter(source.resolved_html_rules).listing_dates(raw, source.url or "")


def fetch_html(client: httpx.Client, url: str, store: RunStore):
    checkpoint: JsonObject = {"responses": {}, "http_attempts": []}
    return HTMLFetcher(client, store).fetch(url, checkpoint, lambda: None)


def checkpoint_identity(source: Source, mindate: date, maxdate: date) -> str:
    config = source.model_dump()
    if config.get("html_discovery") and not config["html_discovery"]["hcm_search_inventory"]:
        config["html_discovery"].pop("hcm_search_inventory")
    if config.get("html_discovery") and not config["html_discovery"]["who_ageing_widget"]:
        config["html_discovery"].pop("who_ageing_widget")
    # Preserve checkpoint identities made before the opt-in text-only policy.
    if config["html_media_policy"] == "review":
        config.pop("html_media_policy")
    if config.get("html_discovery") and not config["html_discovery"]["moh_portal_categories"]:
        config["html_discovery"].pop("moh_portal_categories")
        config["html_discovery"].pop("portal_inventory_size")
    if config.get("html_discovery") and config["html_discovery"]["chronological_scope"] is None:
        config["html_discovery"].pop("chronological_scope")
    if config.get("html_discovery") and not config["html_discovery"]["who_speeches_api"]:
        config["html_discovery"].pop("who_speeches_api")
        config["html_discovery"].pop("who_speeches_max_pages")
    return hashlib.sha256(
        json_bytes(
            {
                "source": config,
                "mindate": str(mindate),
                "maxdate": str(maxdate),
                "parser_version": PARSER_VERSION,
            }
        )
    ).hexdigest()


def collect_html(
    store: CollectionStorage | RunStore,
    source: Source,
    client: httpx.Client | None,
    *,
    mindate,
    maxdate,
    checkpoint=None,
    on_checkpoint=None,
    fetcher=None,
    historical_candidates=(),
) -> Any:
    identity = checkpoint_identity(source, mindate, maxdate)
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
    source_adapter = source_adapter_for(source)
    discovery = source_adapter.discover(
        store, fetcher, checkpoint, save, mindate=mindate, maxdate=maxdate
    )
    listing_hash = discovery["raw_sha256"]
    adapter = ConfiguredHTMLAdapter(source.resolved_html_rules)
    candidates, excluded = discovery["candidates"], discovery["excluded"]
    current_urls = {c["url"] for c in candidates} | {
        c["url"] for c in discovery.get("outside_window_checks", [])
    }
    recovered = {}
    history_day = (
        "selection_date"
        if adapter.rules.window_date_policy == "source_displayed"
        else "publication_date"
    )
    history_evidence = history_day + "_evidence"
    for previous in historical_candidates:
        url = previous["url"]
        if url in current_urls:
            continue
        if not adapter.classify_url(url, source.url or "").accepted:
            raise ValueError("Historical candidate is outside the current source scope")
        recovered[url] = previous
        candidates.append({"url": url, "title": previous["title"]})
        current_urls.add(url)
        if previous.get(history_day) and previous.get(history_evidence):
            discovery["dates"][url] = {previous[history_day]}
            discovery["date_evidence"][url] = [
                dict(
                    e,
                    historical=True,
                    collection_manifest_sha256=previous["collection_manifest_sha256"],
                )
                for e in previous[history_evidence]
            ]
    navigation = [item for item in excluded if item["reason"] == "excluded_path"]
    dates = {
        url: next(iter(values)) for url, values in discovery["dates"].items() if len(values) == 1
    }
    rules_hash = store.put(json_bytes(adapter.rules.model_dump()))
    displayed = adapter.rules.window_date_policy == "source_displayed"
    # Discovery is not editorial selection. A broad headline can contain relevant evidence.
    # Stable ordering also makes repeated runs on identical responses reproducible.
    candidates.sort(
        key=lambda item: (
            -date.fromisoformat(dates[item["url"]]).toordinal() if item["url"] in dates else 0,
            item["url"],
        )
    )
    budget = source.max_article_fetches or source.max_documents
    checks, documents = list(discovery.get("outside_window_checks", [])), []
    # Known outside-window cards consume no article request budget. Otherwise a
    # recent listing can starve an older requested window before its first fetch.
    to_fetch = []
    for item in candidates:
        day = dates.get(item["url"])
        if (
            source.html_discovery
            and source.html_discovery.skip_outside_window
            and day
            and not mindate <= date.fromisoformat(day) <= maxdate
        ):
            checks.append(
                {
                    "url": item["url"],
                    "status": "outside_window",
                    "publication_date": day,
                    "date_status": "publication_known",
                    "date_basis": "saved_listing_only",
                    "content_coverage": "not_fetched_outside_window",
                    "publication_date_evidence": discovery["date_evidence"].get(item["url"], []),
                }
            )
            if displayed:
                c = checks[-1]
                c["selection_date"] = c.pop("publication_date")
                c["selection_date_evidence"] = c.pop("publication_date_evidence")
                c["selection_date_basis"] = "listing_displayed"
                c["date_status"] = "source_displayed"
        else:
            to_fetch.append(item)
    selected = to_fetch[:budget]
    deferred = [item["url"] for item in to_fetch[budget:]]
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
        "candidate_observations": [
            {
                "url": item["url"],
                "title": item["title"],
                "publication_dates": sorted(discovery["dates"].get(item["url"], [])),
                "publication_date_evidence": discovery["date_evidence"].get(item["url"], []),
            }
            for item in candidates
        ],
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
        "historical_candidates_revisited": sorted(recovered),
        "discovery_complete": discovery["discovery_complete"],
    }
    if displayed:
        metadata["date_filter"] = "Source-displayed calendar date; not original publication"
        for observation in metadata["candidate_observations"]:
            observation["selection_dates"] = observation.pop("publication_dates")
            observation["selection_date_evidence"] = observation.pop("publication_date_evidence")
    if source.html_media_policy == "text_only":
        metadata["media_policy"] = "text_only"
        metadata["coverage"] += "; text only; images, audio and video content excluded"
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
        if item["url"] in recovered:
            check["historical_candidate"] = recovered[item["url"]]
        checks.append(check)
        document = None
        try:
            raw, raw_hash, parsed = source_adapter.fetch_article(item, fetcher, checkpoint, save)
            if "portal_request" in parsed:
                check["portal_request"] = parsed["portal_request"]
            check["raw_sha256"] = raw_hash
            # Responses keep their original acquisition time when cached during
            # replay/repair. This prevents old successful pages becoming "new".
            captured = [
                r.get("captured_at")
                for r in checkpoint["responses"].values()
                if r["raw_sha256"] == raw_hash and r.get("captured_at")
            ]
            if captured:
                check["observed_at"] = max(captured)
            parsed.update(url=item["url"], raw_sha256=raw_hash, rules_sha256=rules_hash)
            listing_date = dates.get(item["url"])
            date_conflict = len(discovery["dates"].get(item["url"], set())) > 1
            if listing_date and not displayed:
                if parsed["publication_date"] and parsed["publication_date"] != listing_date:
                    date_conflict = True
                else:
                    parsed["publication_date"] = listing_date
                    parsed["date_status"] = "publication_known"
            if not displayed:
                parsed["publication_date_evidence"].extend(
                    discovery["date_evidence"].get(item["url"], [])
                )
            else:
                # Newsletter policy: prefer the dated listing card, then the
                # article's labelled update date, then explicit publication.
                # Publication metadata keeps its original meaning throughout.
                modified_days = {iso_day(v).isoformat() for v in parsed["modified_date_values"]}
                modified_day = next(iter(modified_days)) if len(modified_days) == 1 else None
                date_conflict |= len(modified_days) > 1 or bool(
                    listing_date and modified_day and listing_date != modified_day
                )
                parsed.update(
                    selection_date=listing_date or modified_day or parsed["publication_date"],
                    selection_date_basis="listing_displayed"
                    if listing_date
                    else "article_modified"
                    if modified_day
                    else "publication",
                    selection_date_evidence=discovery["date_evidence"].get(item["url"], [])
                    if listing_date
                    else parsed["modified_date_evidence"]
                    if modified_day
                    else parsed["publication_date_evidence"],
                )
                check.update(
                    {
                        k: parsed[k]
                        for k in (
                            "selection_date",
                            "selection_date_basis",
                            "selection_date_evidence",
                        )
                    }
                )
            check["extraction_sha256"] = store.put(json_bytes(parsed))
            published = parsed.get("selection_date") if displayed else parsed["publication_date"]
            check["publication_date"] = parsed["publication_date"]
            check["date_status"] = parsed["date_status"]
            check["modified_date_values"] = parsed["modified_date_values"]
            check["content_kind"] = parsed["content_kind"]
            check["content_coverage"] = parsed["content_coverage"]
            check["media"] = parsed["media"]
            check["topic_hint"] = parsed["topic_hint"]
            if (
                parsed["requires_media_review"]
                and source.html_media_policy == "text_only"
                and parsed["media"]
                and all(m["kind"] in {"image", "video", "audio"} for m in parsed["media"])
            ):
                check.update(
                    status="excluded_media",
                    exclusion_reason="media_out_of_scope",
                    content_coverage="excluded_by_text_only_policy",
                )
                continue
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
                publication_date=parsed["publication_date"],
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
            if check.get("status") in {
                "included",
                "outside_window",
                "needs_review",
                "failed",
                "excluded_media",
            }:
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
    if source.html_discovery and source.html_discovery.moh_portal_categories:
        metadata["article_fetch_count"] = len(
            {
                a["request_key"]
                for a in checkpoint["http_attempts"]
                if a.get("method") == "POST" and "maBaiViet" in a.get("request_json", {})
            }
        )
    metadata["outside_window_without_fetch_count"] = sum(
        c.get("content_coverage") == "not_fetched_outside_window" for c in checks
    )
    if (
        not candidates
        and not checks
        and not (
            discovery.get("discovery_method") in {"wordpress_rest", "moh-portal-1", "moh-portal-2"}
            and discovery["discovery_complete"]
        )
    ):
        metadata["item_errors"].append({"code": "html_no_article_candidates"})
    metadata["all_candidates_checked"] = not deferred
    metadata["coverage_complete"] = metadata["coverage_complete"] and not metadata["item_errors"]
    metadata["website_coverage_complete"] = False
    metadata["checked_count"] = len(checks)
    statuses: tuple[str, ...] = ("included", "outside_window", "needs_review", "failed")
    if source.html_media_policy == "text_only":
        statuses += ("excluded_media",)
    metadata["counts"] = {
        status: sum(check.get("status") == status for check in checks) for status in statuses
    }
    return documents, metadata
