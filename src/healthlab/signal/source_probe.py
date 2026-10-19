"""Bounded source access/discovery probes; never ingestion or publication approval."""

import re
import unicodedata
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx
from pydantic import Field, field_validator

from healthlab.models import Contract, IngestionError
from healthlab.pubmed import utc_now
from healthlab.signal.contracts import Text, public_url
from healthlab.signal.source_http import source_client

PROBE_VERSION = "official-discovery-1"
MAX_BYTES = 2_000_000
MAX_LINKS = 300


class ProbeTarget(Contract):
    source_id: Text
    organization: Text
    url: Text
    article_path_prefixes: list[str] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def safe_url(cls, value):
        return public_url(value)


class ProbeTargets(Contract):
    sources: list[ProbeTarget] = Field(min_length=1, max_length=10)


def topic_hint(title, *, basis="title_only"):
    """Discovery hints from titles only, not relevance or factual certification."""
    value = unicodedata.normalize("NFD", title.casefold().replace("đ", "d"))
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = " ".join(re.findall(r"\w+", value))
    groups = {
        "aging_society": (
            "nguoi cao tuoi",
            "nguoi lon tuoi",
            "dan so gia",
            "gia hoa dan so",
            "xa hoi gia hoa",
            "cham soc dai han",
            "older people",
            "older adults",
            "population ageing",
            "population aging",
            "long term care",
        ),
        "longevity": (
            "healthy ageing",
            "healthy aging",
            "healthspan",
            "longevity",
            "lao hoa khoe manh",
            "song khoe",
            "tuoi tho khoe manh",
        ),
    }
    matches = {
        topic: [term for term in terms if f" {term} " in f" {value} "]
        for topic, terms in groups.items()
    }
    topics = [topic for topic, terms in matches.items() if terms]
    return {
        "status": "candidate" if topics else "needs_review",
        "topic": "both" if len(topics) == 2 else topics[0] if topics else None,
        "matched_terms": [term for terms in matches.values() for term in terms],
        "semantic_support": "not_evaluated",
        "basis": basis + "; unmatched text requires review, not automatic exclusion",
    }


class PageInventory(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.feeds = []
        self.anchor = None
        self.hidden = 0
        self.limit_hit = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag == "a":
            self.anchor = {
                "href": attrs.get("href", ""),
                "label": attrs.get("aria-label") or attrs.get("title", ""),
                "parts": [],
            }
        if tag == "link" and attrs.get("type") in {"application/rss+xml", "application/atom+xml"}:
            if len(self.feeds) < 20:
                self.feeds.append(attrs.get("href", ""))

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag == "a" and self.anchor is not None:
            if len(self.links) < MAX_LINKS:
                self.links.append(
                    {
                        "href": self.anchor["href"],
                        "title": " ".join(
                            (self.anchor["label"] or "".join(self.anchor["parts"])).split()
                        ),
                    }
                )
            else:
                self.limit_hit = True
            self.anchor = None

    def handle_data(self, data):
        if self.anchor is not None and not self.hidden:
            self.anchor["parts"].append(data)


def page_inventory(raw, target):
    parser = PageInventory()
    parser.feed(raw.decode("utf-8", errors="replace"))
    candidates, feeds, seen = [], [], set()
    for link in parser.links:
        try:
            url, _ = urldefrag(urljoin(target.url, link["href"]))
            public_url(url)
        except ValueError:
            continue
        parsed = urlsplit(url)
        if parsed.netloc != urlsplit(target.url).netloc or url in seen or not link["title"]:
            continue
        if target.article_path_prefixes and not any(
            parsed.path.startswith(prefix) for prefix in target.article_path_prefixes
        ):
            continue
        # Site templates are not fetched as if they were real article URLs.
        if any(char in url for char in "{} ") or "#:" in link["href"]:
            continue
        seen.add(url)
        candidates.append(
            {"url": url, "title": link["title"], "topic_hint": topic_hint(link["title"])}
        )
    for href in parser.feeds:
        try:
            url = urljoin(target.url, href)
            public_url(url)
        except ValueError:
            continue
        if urlsplit(url).netloc == urlsplit(target.url).netloc and url not in feeds:
            feeds.append(url)
    return {
        "candidates": candidates,
        "advertised_feeds": feeds,
        "links_truncated": parser.limit_hit,
        "coverage": "Configured page links only; no article fetch, pagination or date filter",
        "freshness": "not_evaluated",
    }


def probe_sources(store, targets, *, client=None):
    owned = client is None
    client = client or source_client()
    run = store.new("signal_source_probe", {"targets": targets.model_dump()})
    run.update(parser_version=PROBE_VERSION, stage="source_probe", source_checks=[])
    store.save(run)
    try:
        for target in targets.sources:
            check = {
                "source_id": target.source_id,
                "organization": target.organization,
                "url": target.url,
                "checked_at": utc_now(),
                "status": "running",
                "errors": [],
            }
            run["source_checks"].append(check)
            store.save(run)
            try:
                with client.stream("GET", target.url, follow_redirects=False) as response:
                    check.update(
                        http_status=response.status_code,
                        content_type=response.headers.get("content-type", ""),
                    )
                    if 300 <= response.status_code < 400:
                        check.update(status="redirect_requires_review")
                        location = urljoin(target.url, response.headers.get("location", ""))
                        try:
                            check["redirect_url"] = public_url(location)
                        except ValueError:
                            check["errors"].append({"code": "invalid_redirect"})
                        store.save(run)
                        continue
                    if response.status_code != 200:
                        raise IngestionError("probe_http", "Source returned non-200")
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_BYTES:
                            raise IngestionError("probe_limit", "Source exceeds probe limit")
                check.update(raw_sha256=store.put(bytes(raw)), response_bytes=len(raw))
                if "text/html" not in check["content_type"].lower():
                    raise IngestionError("probe_content_type", "Expected HTML discovery page")
                check.update(page_inventory(bytes(raw), target))
                check["status"] = "reachable"
            except (httpx.HTTPError, IngestionError, ValueError) as exc:
                check["status"] = "failed"
                check["errors"].append(
                    {"code": exc.code if isinstance(exc, IngestionError) else type(exc).__name__}
                )
            store.save(run)
        successes = sum(check["status"] == "reachable" for check in run["source_checks"])
        run.update(
            status="completed"
            if successes == len(targets.sources)
            else "partial"
            if successes
            else "failed",
            stage="finished",
            finished_at=utc_now(),
        )
        run["warnings"].append("Access/discovery probe only; not automated source monitoring")
        store.save(run)
        return run
    finally:
        if owned:
            client.close()
