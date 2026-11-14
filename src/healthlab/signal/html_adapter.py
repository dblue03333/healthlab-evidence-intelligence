"""One configured HTML adapter; source-specific rules live in data, not branches."""

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urldefrag, urlencode, urljoin, urlsplit, urlunsplit

from healthlab.common import JsonObject
from healthlab.models import IngestionError
from healthlab.signal.html_rules import HTMLSourceRules, TextDateRule
from healthlab.signal.html_tree import ArticleTree, Element
from healthlab.signal.source_probe import topic_hint

PARSER_VERSION = "official-html-7"


def parse_day(value: str, date_format: str) -> str:
    if date_format == "iso":
        return iso_day(value).isoformat()
    # Explicit English month mapping avoids depending on the machine locale.
    if date_format == "%d %B %Y":
        months = (
            "january february march april may june july august september october november december"
        ).split()
        day, month, year = value.strip().split()
        return date(int(year), months.index(month.lower()) + 1, int(day)).isoformat()
    return datetime.strptime(value, date_format).date().isoformat()


def read_date(node: Element, rule: TextDateRule) -> str | None:
    value = node.attrs.get(rule.attribute, "") if rule.attribute else node.text()
    if rule.pattern:
        match = re.fullmatch(rule.pattern, value)
        if not match:
            return None
        value = match["date"]
    try:
        return parse_day(value, rule.format)
    except (ValueError, TypeError, IngestionError):
        if rule.fallback_only:
            return None
        raise IngestionError("html_date_invalid", "Configured date field is invalid") from None


@dataclass(frozen=True)
class URLDecision:
    url: str
    accepted: bool
    reason: str


class ConfiguredHTMLAdapter:
    def __init__(self, rules: HTMLSourceRules) -> None:
        self.rules = rules
        self.includes = [re.compile(p) for p in rules.article_urls.include_path_patterns]
        self.excludes = [re.compile(p) for p in rules.article_urls.exclude_path_patterns]

    def classify_url(self, href: str, listing_url: str) -> URLDecision:
        url = href
        try:
            url = urldefrag(urljoin(listing_url, href))[0]
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname != self.rules.host
                or parsed.port not in (None, 443)
                or parsed.username
                or parsed.password
                or any(c.isspace() for c in url)
                or any(c in url for c in "{}\\")
            ):
                return URLDecision(url, False, "outside_source_scope")
        except ValueError:
            return URLDecision(url, False, "invalid_url")
        query = parse_qs(parsed.query, keep_blank_values=True)
        urls = self.rules.article_urls
        if parsed.path in urls.exclude_paths:
            return URLDecision(url, False, "excluded_path")
        if any(p.fullmatch(parsed.path) for p in self.excludes):
            return URLDecision(url, False, "excluded_path_pattern")
        if not any(p.fullmatch(parsed.path) for p in self.includes):
            return URLDecision(url, False, "unmatched_path")
        for key, pattern in urls.required_query_parameters.items():
            if len(query.get(key, [])) != 1 or not re.fullmatch(pattern, query[key][0]):
                return URLDecision(url, False, "query_constraint")
        # Only explicitly configured tracking parameters are dropped; keep article IDs.
        query_string = parsed.query
        if urls.drop_query_parameters:
            query_string = urlencode(
                [
                    (k, v)
                    for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                    if k not in urls.drop_query_parameters
                ]
            )
        normalized = urlunsplit(("https", self.rules.host, parsed.path, query_string, ""))
        return URLDecision(normalized, True, "included_path")

    def parse_article(self, raw: bytes, *, url=None) -> JsonObject:
        return _parse_article(raw, self.rules, url=url)

    def listing_dates(self, raw: bytes, listing_url: str) -> dict[str, str]:
        return _listing_dates(raw, listing_url, self)

    def listing_date_values(self, raw: bytes, listing_url: str) -> JsonObject:
        return _listing_dates(raw, listing_url, self, keep_conflicts=True)


def iso_day(value: str) -> date:
    try:
        return (
            date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value).date()
        )
    except (ValueError, TypeError):
        raise IngestionError("html_date_invalid", "Publication date is invalid") from None


def json_records(value: Any) -> Iterator[JsonObject]:
    if isinstance(value, dict):
        yield value
        yield from json_records(value.get("@graph", []))
    elif isinstance(value, list):
        for item in value:
            yield from json_records(item)


def article_media(body: Element, url: str | None) -> list[Any]:
    """Inventory media in the scoped body; do not fetch or claim to transcribe it."""
    found, seen = [], set()
    for node in body.nodes():
        tag = node.tag
        value = node.attrs.get("src") or node.attrs.get("data")
        if tag == "a":
            value = node.attrs.get("href")
        if tag not in {"iframe", "video", "audio", "source", "embed", "object", "img", "a"}:
            continue
        if not value and tag not in {"iframe", "embed", "object"}:
            continue
        resolved = urljoin(url or "", value) if value else ""
        if resolved.startswith("//"):
            resolved = "https:" + resolved
        if "docs.google.com/viewer" in resolved:
            resolved = parse_qs(urlsplit(resolved).query).get("url", [resolved])[0]
        path = urlsplit(resolved).path.lower()
        if "/wp-content/plugins/" in path or path.endswith(".svg"):
            continue  # Embedded-document spinners/icons are not source illustrations.
        if path.endswith(".pdf"):
            kind = "pdf"
        elif tag == "img":
            kind = "image"
        elif tag == "audio" or path.endswith((".mp3", ".wav", ".ogg")):
            kind = "audio"
        elif (
            tag == "video"
            or path.endswith((".mp4", ".webm"))
            or any(
                host in (urlsplit(resolved).hostname or "")
                for host in ("youtube.com", "youtu.be", "facebook.com")
            )
        ):
            kind = "video"
        elif tag in {"iframe", "embed", "object"}:
            kind = "embedded"
        else:
            continue
        if resolved and urlsplit(resolved).scheme != "https":
            continue
        key = (kind, resolved)
        if key not in seen:
            found.append({"kind": kind, "url": resolved or None, "locator": tag})
            seen.add(key)
    return found


def relevant_json_record(record: JsonObject, url: str | None) -> bool:
    """Do not borrow publication dates from Organization/Breadcrumb/related articles."""
    types = record.get("@type", [])
    types = [types] if isinstance(types, str) else types
    if types and not set(types) & {"Article", "NewsArticle", "BlogPosting", "Report"}:
        return False
    identity = record.get("url") or record.get("mainEntityOfPage") or record.get("@id")
    if isinstance(identity, dict):
        identity = identity.get("@id")
    if url and isinstance(identity, str):
        entity_url = urldefrag(urljoin(url, identity))[0].rstrip("/")
        return entity_url == urldefrag(url)[0].rstrip("/")
    return True


def _parse_article(raw: bytes, rules: HTMLSourceRules, *, url=None) -> JsonObject:
    nodes = list(ArticleTree(raw).root.nodes())
    bodies = [node for node in nodes if rules.body.matches(node)]
    if len(bodies) != 1:
        raise IngestionError("html_article_shape", "Article title/body selector changed")
    article_scope = None
    if rules.article_scope:
        ancestor: Element | None = bodies[0]
        while ancestor is not None:
            if rules.article_scope.matches(ancestor):
                article_scope = ancestor
                break
            ancestor = ancestor.parent
        if article_scope is None and rules.require_article_scope:
            raise IngestionError("html_article_shape", "Article scope selector changed")
    scoped_nodes = list(article_scope.nodes()) if article_scope else nodes
    headings = [node.text() for node in scoped_nodes if rules.title.matches(node) and node.text()]
    if len(headings) != 1:
        raise IngestionError("html_article_shape", "Article title/body selector changed")
    passages = []
    if rules.article_scope and article_scope and rules.lead:
        leads = [node.text() for node in scoped_nodes if rules.lead.matches(node)]
        if len(leads) > 1:
            raise IngestionError("html_article_shape", "Ambiguous article introduction")
        if leads and leads[0] and leads[0] not in bodies[0].text():
            passages.append(
                {
                    "locator": rules.article_scope.locator + " " + rules.lead.locator,
                    "text": leads[0],
                }
            )
    if bodies[0].text():
        passages.append({"locator": rules.body.locator, "text": bodies[0].text()})
    body = "\n\n".join(p["text"] for p in passages)
    media = article_media(bodies[0], url)
    substantial_media = [m for m in media if m["kind"] != "image"]
    media_only = bool(substantial_media and len(body) < 200 or media and len(body) < 80)
    if not media_only and len(body) < 80:
        raise IngestionError("html_article_sparse", "Article is too sparse or media-only")
    kind = (substantial_media or media)[0]["kind"] if media_only else "html_article"
    dates = []
    modified = []
    modified_evidence = []
    for node in nodes if rules.standard_date_metadata else []:
        if node.tag == "meta" and node.attrs.get("property") == "article:published_time":
            dates.append(("meta:article:published_time", node.attrs.get("content", "")))
        if node.tag == "meta" and node.attrs.get("property") == "article:modified_time":
            value = node.attrs.get("content", "")
            modified.append(value)
            modified_evidence.append({"locator": "meta:article:modified_time", "value": value})
        if node.tag == "time" and "published" in node.attrs.get("class", "").split():
            dates.append(("time.published@datetime", node.attrs.get("datetime", "")))
        if node.tag == "script" and node.attrs.get("type") == "application/ld+json":
            try:
                records = json_records(json.loads(node.text(include_hidden=True).rstrip(";")))
                for record in records:
                    if not relevant_json_record(record, url):
                        continue
                    if record.get("datePublished"):
                        dates.append(("jsonld:datePublished", record["datePublished"]))
                    if record.get("dateModified"):
                        modified.append(record["dateModified"])
                        modified_evidence.append(
                            {"locator": "jsonld:dateModified", "value": record["dateModified"]}
                        )
            except (ValueError, TypeError):
                continue
    for rule in rules.text_dates:
        if rule.kind == "published" and rule.fallback_only and dates:
            continue
        selected_nodes = {
            "document": nodes,
            "article": scoped_nodes,
            "article_children": [n for n in scoped_nodes if n.parent is article_scope]
            if article_scope
            else [],
            "body": list(bodies[0].nodes()),
        }[rule.scope]
        for node in selected_nodes:
            if not rule.selector.matches(node):
                continue
            value = read_date(node, rule)
            if value is None:
                continue
            locator = rule.selector.locator
            # The rules contract requires an article scope for article_children dates.
            if rule.scope == "article_children" and rules.article_scope:
                locator = rules.article_scope.locator + " > " + locator
            if rule.attribute:
                locator += "@" + rule.attribute
            if rule.kind == "published":
                dates.append((locator, value))
                if rule.fallback_only:
                    break
            else:
                modified.append(value)
                modified_evidence.append(
                    {"locator": locator, "value": value, "display_text": node.text()}
                )
    days = {iso_day(value) for _, value in dates}
    if len(days) > 1:
        raise IngestionError("html_date_conflict", "Publication date fields disagree")
    return {
        "parser_version": PARSER_VERSION,
        "title": headings[0],
        "body": body,
        "publication_date": next(iter(days)).isoformat() if days else None,
        "publication_date_evidence": [
            {"locator": locator, "value": value} for locator, value in dates
        ],
        "modified_date_values": list(dict.fromkeys(modified)),
        "modified_date_evidence": modified_evidence,
        "date_status": "publication_known" if days else "update_only" if modified else "missing",
        "content_kind": kind,
        "content_coverage": "media_unread" if media_only else "scoped_html_text_only",
        "requires_media_review": media_only,
        "media": media,
        "passages": passages,
        "topic_hint": topic_hint(headings[0] + " " + body, basis="title_and_scoped_body"),
        "semantic_support": "not_evaluated",
    }


def _listing_dates(
    raw: bytes, listing_url: str, adapter: ConfiguredHTMLAdapter, *, keep_conflicts=False
) -> JsonObject:
    """Bind a visible timestamp only to a container identifying one unique article URL."""
    found: JsonObject = {}
    rule = adapter.rules.listing_date
    if rule is None:
        return found
    for anchor in ArticleTree(raw).root.nodes():
        if anchor.tag != "a":
            continue
        decision = adapter.classify_url(anchor.attrs.get("href", ""), listing_url)
        url = decision.url
        if not decision.accepted:
            continue
        parent: Element | None = anchor  # WHO cards contain their timestamp inside the anchor.
        for _ in range(rule.max_ancestor_levels):
            if parent is None:
                break
            decisions = [
                adapter.classify_url(node.attrs.get("href", ""), listing_url)
                for node in parent.nodes()
                if node.tag == "a"
            ]
            urls = {d.url for d in decisions if d.accepted}
            if urls == {url}:
                for node in parent.nodes():
                    if rule.selector.matches(node):
                        try:
                            value = parse_day(node.text(), rule.format)
                            found.setdefault(url, set()).add(value)
                        except (ValueError, IngestionError):
                            pass
                if url in found:
                    break
            parent = parent.parent
    return (
        found
        if keep_conflicts
        else {url: next(iter(values)) for url, values in found.items() if len(values) == 1}
    )
