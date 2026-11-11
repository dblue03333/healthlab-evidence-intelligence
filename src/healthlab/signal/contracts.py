"""Source-neutral contracts, separate from PubMed scientific evidence."""

import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from healthlab.models import Contract
from healthlab.signal.html_rules import HTMLDiscoveryRules, HTMLSourceRules, builtin_html_rules

Text = Annotated[str, Field(min_length=1)]


def public_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or any(c.isspace() for c in value)
    ):
        raise ValueError("Expected an HTTPS source URL without credentials")
    return value


class Source(Contract):
    source_id: Text
    organization: Text
    kind: Literal["rss", "pubmed", "manual", "html"]
    enabled: bool = True
    required: bool = True
    url: str | None = None
    query: str | None = None
    geography: Text = "unknown"
    usage_policy: Text
    max_documents: int = Field(default=5, ge=1, le=30)
    # HTML request budget, separate from newsletter selection. None preserves legacy configs.
    max_article_fetches: int | None = Field(default=None, ge=1, le=300)
    html_profile: Literal["who_news", "moh_population", "hcm_medinet"] | None = None
    html_rules: HTMLSourceRules | None = None
    html_discovery: HTMLDiscoveryRules | None = None
    html_max_response_bytes: int = Field(default=2_000_000, ge=1000, le=32_000_000)
    html_media_policy: Literal["review", "text_only"] = "review"

    @property
    def resolved_html_rules(self):
        return self.html_rules or builtin_html_rules(self.html_profile, listing_url=self.url)

    @field_validator("source_id")
    @classmethod
    def safe_id(cls, value):
        if not re.fullmatch(r"[a-z0-9_-]{1,64}", value):
            raise ValueError("source_id must use lowercase letters, digits, _ or -")
        return value

    @model_validator(mode="after")
    def connector_contract(self):
        if self.kind == "rss":
            public_url(self.url or "")
        if self.kind == "html":
            public_url(self.url or "")
            if (self.html_profile is None) == (self.html_rules is None):
                raise ValueError("HTML source requires exactly one of html_profile or html_rules")
            rules = self.resolved_html_rules
            parsed = urlsplit(self.url)
            if parsed.hostname != rules.host or parsed.port not in (None, 443):
                raise ValueError("HTML profile/rules requires its verified official host")
            if self.html_discovery:
                discovery = self.html_discovery
                if discovery.hcm_search_inventory and (
                    rules.host != "medinet.hochiminhcity.gov.vn"
                    or parsed.path != "/tin-tuc-su-kien-c1780.aspx"
                    or discovery.wordpress_api_url
                    or discovery.moh_portal_categories
                ):
                    raise ValueError("HCMC search requires its verified host and category")
                if discovery.who_ageing_widget and (
                    rules.host != "www.who.int"
                    or discovery.wordpress_api_url
                    or discovery.moh_portal_categories
                    or "https://www.who.int/health-topics/ageing"
                    not in [self.url] + discovery.additional_listing_urls
                ):
                    raise ValueError("WHO Ageing widget requires its verified host and landing")
                if discovery.who_speeches_api and (
                    rules.host != "www.who.int"
                    or discovery.wordpress_api_url
                    or discovery.moh_portal_categories
                    or not any(
                        urlsplit(u).path.startswith("/vietnam/news/speeches")
                        for u in [self.url] + discovery.additional_listing_urls
                    )
                ):
                    raise ValueError(
                        "WHO speeches supplement requires its verified host and branch"
                    )
                if discovery.moh_portal_categories and (
                    rules.host != "moh.gov.vn"
                    or discovery.wordpress_api_url
                    or discovery.additional_listing_urls
                    or len(discovery.moh_portal_categories) > discovery.max_listing_pages
                ):
                    raise ValueError(
                        "Ministry portal requires its official host and category budget"
                    )
                if discovery.wordpress_api_url:
                    public_url(discovery.wordpress_api_url)
                    endpoint = urlsplit(discovery.wordpress_api_url)
                    if (
                        endpoint.hostname != rules.host
                        or endpoint.port not in (None, 443)
                        or endpoint.query
                        or endpoint.fragment
                        or not endpoint.path.endswith("/wp/v2/posts")
                        or discovery.additional_listing_urls
                    ):
                        raise ValueError(
                            "WordPress discovery requires a same-host posts endpoint without extra seeds/query"
                        )
                if len(discovery.additional_listing_urls) + 1 > discovery.max_listing_pages:
                    raise ValueError("Listing budget must cover all configured seed pages")
                for url in discovery.additional_listing_urls:
                    public_url(url)
                    seed = urlsplit(url)
                    if seed.hostname != rules.host or seed.port not in (None, 443) or seed.fragment:
                        raise ValueError(
                            "Discovery seeds must use the same verified host without fragments"
                        )
                if discovery.stop_before_window and rules.listing_date is None:
                    raise ValueError("Date cutoff requires publication dates on listing cards")
        elif self.html_profile is not None or self.html_rules is not None:
            raise ValueError("HTML rules are only valid for HTML sources")
        if self.kind != "html" and self.html_discovery is not None:
            raise ValueError("HTML discovery is only valid for HTML sources")
        if self.kind == "pubmed" and not self.query:
            raise ValueError("PubMed requires an explicit query")
        if self.kind == "pubmed" and self.max_documents != 5:
            raise ValueError("The existing PubMed adapter supports a fixed top-five sample")
        return self


class Registry(Contract):
    schema_version: Literal["signal-registry-1", "signal-registry-2", "signal-registry-3"] = (
        "signal-registry-1"
    )
    sources: list[Source] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_sources(self):
        ids = [s.source_id for s in self.sources]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate source_id")
        if self.schema_version == "signal-registry-1" and any(
            s.kind == "html" for s in self.sources
        ):
            raise ValueError("HTML sources require signal-registry-2")
        if self.schema_version != "signal-registry-3" and any(
            s.html_rules is not None for s in self.sources
        ):
            raise ValueError("Inline HTML rules require signal-registry-3")
        return self


class SourceDocument(Contract):
    schema_version: Literal["signal-document-1", "signal-document-2"] = "signal-document-1"
    document_id: Text
    source_id: Text
    url: Text
    title: Text
    publication_date: str | None
    geography: Text
    text: Text
    coverage: Literal["rss_summary_only", "abstract_only", "manual_excerpt", "html_article"]
    raw_sha256: Text

    @field_validator("url")
    @classmethod
    def safe_link(cls, value):
        return public_url(value)


class Signal(Contract):
    snapshot_id: Text
    topic: Literal["aging_society", "longevity", "both"]
    headline: Text
    what_happened: Text
    supporting_passage: Text
    vietnam_relevance: Text
    uncertainty: Text


class Draft(Contract):
    overview: str
    signals: list[Signal] = Field(max_length=5)

    @model_validator(mode="after")
    def unique_snapshots(self):
        ids = [s.snapshot_id for s in self.signals]
        if len(ids) != len(set(ids)):
            raise ValueError("Use at most one signal per snapshot in this pilot")
        return self
