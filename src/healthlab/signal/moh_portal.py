"""Official Ministry portal's public read-only API; raw JSON remains the source."""

import json
from datetime import datetime
from html import escape
from urllib.parse import parse_qs, urlencode, urlsplit

from healthlab.models import IngestionError
from healthlab.signal.html_adapter import ConfiguredHTMLAdapter
from healthlab.signal.html_rules import ElementSelector

API = "https://moh.gov.vn/bridge?url=/portal/api/"
VERSION = "moh-portal-2"


def publication_day(value):
    # Require the complete displayed timestamp, not an arbitrary ten-char prefix.
    return datetime.strptime(value, "%d/%m/%Y %I:%M %p").date().isoformat()


def discover_portal(source, fetcher, checkpoint, save, *, mindate, maxdate):
    if maxdate is None or maxdate < mindate:
        raise ValueError("Portal discovery requires a complete publication window")
    rules = source.html_discovery
    pages, gaps, candidates, dates, evidence = [], [], {}, {}, {}
    record_dates, article_urls = {}, {}
    for category in rules.moh_portal_categories:
        url = API + "lay_bai_viet_theo_danh_muc"
        body = {
            "CHUYEN_TRANG": "BO_YTE",
            "NGON_NGU": "TIENG_VIET",
            "MA_DANH_MUC": category,
            "TYPE": 1,
            "PAGE_SIZE": rules.portal_inventory_size,
            "ISEXTEND": 1,
        }
        page = {
            "url": url,
            "request_json": body,
            "category_id": category,
            "status": "running",
            "candidate_urls": [],
        }
        pages.append(page)
        try:
            raw, digest = fetcher.fetch(
                url, checkpoint, save, response_format="json", request_json=body
            )
            page["raw_sha256"] = digest
            value = json.loads(raw)
            if not isinstance(value, list) or len(value) != 1:
                raise ValueError("Expected one category inventory")
            value = value[0]
            total, records = value["TOTAL_RECORD"], value["ITEMS"]
            if (
                str(value["MA"]) != category
                or type(total) is not int
                or total < 0
                or not isinstance(records, list)
                or len(records) != min(total, rules.portal_inventory_size)
            ):
                raise ValueError("Category identity or bounded inventory count disagrees")
            seen, ordered = set(), []
            for index, record in enumerate(records):
                identifier = record["MA"]
                if (
                    not isinstance(identifier, str)
                    or not identifier.isdigit()
                    or int(identifier) <= 0
                    or identifier in seen
                    or record["DON_VI_SAN_XUAT"] != "BO_YTE"
                    or record["NGON_NGU"] not in {"TIENG_VIET", "null", None}
                ):
                    raise ValueError("Invalid, repeated or misattributed portal record")
                seen.add(identifier)
                day = publication_day(record["THOI_GIAN_XUAT_BAN"])
                ordered.append(day)
                if identifier in record_dates and record_dates[identifier] != day:
                    raise ValueError("Publication dates disagree across categories")
                record_dates[identifier] = day
                title = record["TIEU_DE"]
                if not isinstance(title, str) or not title.strip():
                    raise ValueError("Missing title")
                # ISEXTEND includes child categories; legacy migrated records may
                # have null language. Endpoint category/site identity is checked
                # above; a foreign explicit language is still rejected.
                # Normalize multi-category posts to the first configured category
                # encountered, retaining all listing evidence for that identity.
                existing = article_urls.get(identifier)
                article_url = existing or "https://moh.gov.vn/index.jsp?" + urlencode(
                    {"pageId": "5803", "aid": identifier, "cid": category}
                )
                candidates.setdefault(
                    article_url, {"url": article_url, "title": title, "portal_id": identifier}
                )
                article_urls[identifier] = article_url
                dates.setdefault(article_url, set()).add(day)
                evidence.setdefault(article_url, []).append(
                    {
                        "locator": f"json[0].ITEMS[{index}].THOI_GIAN_XUAT_BAN",
                        "value": day,
                        "raw_sha256": digest,
                        "listing_url": url,
                        "request_json": body,
                    }
                )
                page["candidate_urls"].append(article_url)
            if ordered != sorted(ordered, reverse=True):
                raise ValueError("Portal inventory is not ordered by publication date")
            exhausted = len(records) == total
            boundary = bool(ordered and ordered[-1] < str(mindate))
            page.update(
                status="completed",
                total_posts=total,
                returned_count=len(records),
                publication_dates_known=len(records),
                candidate_count=len(records),
                publication_range={"min": min(ordered), "max": max(ordered)} if ordered else None,
                stop_reason="api_totals_exhausted"
                if exhausted
                else "publication_boundary"
                if boundary
                else "inventory_limit",
            )
            if not (exhausted or boundary):
                gaps.append(
                    {"url": url, "category_id": category, "code": "portal_inventory_truncated"}
                )
        except IngestionError as exc:
            from healthlab.signal.discovery import FATAL_STORAGE

            if exc.code in FATAL_STORAGE:
                raise
            page.update(status="failed", error_code=exc.code)
            gaps.append({"url": url, "category_id": category, "code": exc.code})
        except (ValueError, TypeError, KeyError, AttributeError):
            page.update(status="failed", error_code="portal_listing_invalid")
            gaps.append({"url": url, "category_id": category, "code": "portal_listing_invalid"})
    # Only the requested publication window consumes the article budget. Every
    # date-only outside-window item retains its raw evidence in the source check.
    outside = [
        {
            "url": u,
            "status": "outside_window",
            "publication_date": next(iter(dates[u])),
            "date_status": "publication_known",
            "date_basis": "saved_listing_only",
            "content_coverage": "not_fetched_outside_window",
            "publication_date_evidence": evidence[u],
        }
        for u in candidates
        if not str(mindate) <= next(iter(dates[u])) <= str(maxdate)
    ]
    selected = [
        c for u, c in candidates.items() if str(mindate) <= next(iter(dates[u])) <= str(maxdate)
    ]
    return {
        "raw_sha256": next((p["raw_sha256"] for p in pages if "raw_sha256" in p), None),
        "candidates": selected,
        "excluded": [],
        "dates": dates,
        "date_evidence": evidence,
        "listing_pages": pages,
        "listing_gaps": gaps,
        "deferred_listing_urls": [],
        "listing_budget": rules.max_listing_pages,
        "seed_count": len(pages),
        "discovered_link_count": len(candidates),
        "listing_links_truncated": False,
        "pagination_followed": False,
        "discovery_complete": not gaps,
        "discovery_method": VERSION,
        "outside_window_checks": outside,
    }


def fetch_article(source, item, fetcher, checkpoint, save):
    query = parse_qs(urlsplit(item["url"]).query)
    body = {"maBaiViet": query["aid"][0], "maDanhMuc": query["cid"][0]}
    raw, digest = fetcher.fetch(
        API + "layChiTietBaiViet", checkpoint, save, response_format="json", request_json=body
    )
    try:
        value = json.loads(raw)["d"]
        if str(value["ma"]) != body["maBaiViet"] or value["donViSanXuat"] != "BO_YTE":
            raise ValueError("Article identity mismatch")
        day = publication_day(value["thoiGianBatDauDang"])
        title, content = value["tieuDe"], value["noiDung"]
        if not isinstance(title, str) or not isinstance(content, str):
            raise ValueError("Missing title/body")
        # Reuse bounded HTML text/media extraction on the API's literal HTML.
        # The wrapper is internal only; locators below point back into raw JSON.
        wrapped = f'<h1 id="portal-title">{escape(title)}</h1><div id="portal-body">{content}</div>'.encode()
        rules = source.resolved_html_rules.model_copy(
            update={"title": ElementSelector(tag="h1", attributes={"id": "portal-title"})}
        )
        parsed = ConfiguredHTMLAdapter(rules).parse_article(wrapped, url=item["url"])
        parsed.update(publication_date=day, date_status="publication_known")
        parsed["publication_date_evidence"] = [
            {"locator": "json.d.thoiGianBatDauDang", "value": day, "raw_sha256": digest}
        ]
        parsed["passages"] = [{"locator": "json.d.noiDung", "text": parsed["body"]}]
        parsed["portal_request"] = {"url": API + "layChiTietBaiViet", "request_json": body}
        parsed["parser_version"] = VERSION
        return raw, digest, parsed
    except (ValueError, KeyError, TypeError):
        raise IngestionError(
            "portal_article_invalid", "Portal article identity/date/body invalid"
        ) from None
