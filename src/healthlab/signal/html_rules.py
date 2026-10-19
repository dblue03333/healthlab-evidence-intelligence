"""Validated, declarative HTML source rules; no network or source-specific branches."""

import json
import re
from importlib.resources import files
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from healthlab.models import Contract


def checked_pattern(value):
    if not value or len(value) > 512:
        raise ValueError("Patterns must contain 1–512 characters")
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError("Invalid regular expression") from exc
    return value


class ElementSelector(Contract):
    """A single element matcher, not a CSS/query language or executable expression."""

    tag: str | None = None
    classes: list[str] = Field(default_factory=list)
    attributes: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def specific(self):
        if not (self.tag or self.classes or self.attributes):
            raise ValueError("Selector must identify a tag, class or attribute")
        if self.tag and not re.fullmatch(r"[a-z][a-z0-9-]*", self.tag):
            raise ValueError("Selector tag must be a lowercase HTML tag")
        if any(not re.fullmatch(r"[\w-]+", name) for name in self.classes):
            raise ValueError("Selector classes must be individual class names")
        if any(not re.fullmatch(r"[\w:-]+", name) for name in self.attributes):
            raise ValueError("Invalid selector attribute name")
        return self

    def matches(self, node):
        return (
            (self.tag is None or node.tag == self.tag)
            and set(self.classes).issubset(node.attrs.get("class", "").split())
            and all(node.attrs.get(key) == value for key, value in self.attributes.items())
        )

    @property
    def locator(self):
        return (
            (self.tag or "")
            + "".join("." + name for name in self.classes)
            + "".join(f'[{name}="{value}"]' for name, value in self.attributes.items())
        )


class ArticleURLRules(Contract):
    # Full matches of the path, case-sensitive. Query constraints are separate.
    include_path_patterns: list[str] = Field(min_length=1, max_length=30)
    exclude_paths: list[str] = Field(default_factory=list, max_length=100)
    exclude_path_patterns: list[str] = Field(default_factory=list, max_length=30)
    required_query_parameters: dict[str, str] = Field(default_factory=dict)
    drop_query_parameters: list[str] = Field(default_factory=list)

    @field_validator("include_path_patterns", "exclude_path_patterns")
    @classmethod
    def valid_patterns(cls, values):
        return [checked_pattern(value) for value in values]

    @field_validator("required_query_parameters")
    @classmethod
    def valid_query_patterns(cls, values):
        return {key: checked_pattern(value) for key, value in values.items()}

    @field_validator("exclude_paths")
    @classmethod
    def absolute_paths(cls, values):
        if any(not value.startswith("/") or "?" in value or "#" in value for value in values):
            raise ValueError("Excluded paths must be absolute paths without queries/fragments")
        return values

    @model_validator(mode="after")
    def preserve_required_query(self):
        if set(self.required_query_parameters) & set(self.drop_query_parameters):
            raise ValueError("Cannot drop a required query parameter")
        return self


class TextDateRule(Contract):
    selector: ElementSelector
    kind: Literal["published", "modified"]
    scope: Literal["document", "article", "article_children", "body"] = "article"
    attribute: str | None = None
    format: Literal["iso", "%d/%m/%Y", "%d %B %Y"] = "iso"
    # Optional extraction from a header. Must expose a named group (?P<date>...).
    pattern: str | None = None
    fallback_only: bool = False

    @field_validator("pattern")
    @classmethod
    def date_group(cls, value):
        if value is not None:
            checked_pattern(value)
            if "date" not in re.compile(value).groupindex:
                raise ValueError("Date pattern requires a named 'date' group")
        return value


class ListingDateRule(Contract):
    selector: ElementSelector
    format: Literal["%d/%m/%Y", "%d %B %Y", "iso"] = "%d/%m/%Y"
    max_ancestor_levels: int = Field(default=3, ge=1, le=8)


class HTMLDiscoveryRules(Contract):
    """Finite same-host listing traversal; no arbitrary crawls or JS execution."""

    additional_listing_urls: list[str] = Field(default_factory=list, max_length=20)
    listing_path_patterns: list[str] = Field(default_factory=list, max_length=30)
    required_query_parameters: dict[str, str] = Field(default_factory=dict)
    drop_query_parameters: list[str] = Field(default_factory=list)
    pagination_selector: ElementSelector | None = None
    max_listing_pages: int = Field(default=4, ge=1, le=30)
    upgrade_same_host_http: bool = False
    stop_before_window: bool = False
    wordpress_api_url: str | None = None
    api_page_size: int = Field(default=50, ge=1, le=100)
    sequential_page_pattern: str | None = None
    skip_outside_window: bool = False

    @field_validator("listing_path_patterns")
    @classmethod
    def listing_patterns(cls, values):
        return [checked_pattern(value) for value in values]

    @field_validator("required_query_parameters")
    @classmethod
    def query_patterns(cls, values):
        return {key: checked_pattern(value) for key, value in values.items()}

    @field_validator("sequential_page_pattern")
    @classmethod
    def page_pattern(cls, value):
        if value is not None:
            checked_pattern(value)
            if "page" not in re.compile(value).groupindex:
                raise ValueError("Sequential pagination requires a named page group")
        return value

    @model_validator(mode="after")
    def preserve_required_query(self):
        if set(self.required_query_parameters) & set(self.drop_query_parameters):
            raise ValueError("Cannot drop a required listing query parameter")
        return self


class HTMLSourceRules(Contract):
    schema_version: Literal["html-source-rules-1"] = "html-source-rules-1"
    rule_id: str = Field(pattern=r"^[a-z0-9_-]{1,64}$")
    revision: int = Field(default=1, ge=1)
    host: str
    article_urls: ArticleURLRules
    body: ElementSelector
    title: ElementSelector = Field(default_factory=lambda: ElementSelector(tag="h1"))
    article_scope: ElementSelector | None = None
    require_article_scope: bool = True
    lead: ElementSelector | None = None
    standard_date_metadata: bool = True
    text_dates: list[TextDateRule] = Field(default_factory=list, max_length=20)
    listing_date: ListingDateRule | None = None

    @field_validator("host")
    @classmethod
    def exact_host(cls, value):
        if (
            len(value) > 253
            or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", value)
            or "." not in value
        ):
            raise ValueError("Host must be an exact lowercase hostname, without scheme or port")
        return value

    @model_validator(mode="after")
    def scoped_lead(self):
        if self.lead and not self.article_scope:
            raise ValueError("Lead extraction requires an article scope")
        if not self.article_scope and any(r.scope == "article_children" for r in self.text_dates):
            raise ValueError("Direct-child date rules require an article scope")
        return self


def builtin_html_rules(profile, *, listing_url=None):
    """Read legacy presets on demand. New sources can supply inline rules instead."""
    presets = json.loads(files("healthlab.signal").joinpath("html_profiles.json").read_text())
    if profile not in presets:
        raise ValueError("Unknown legacy HTML profile")
    rules = HTMLSourceRules.model_validate(presets[profile])
    # Compatibility migration only: old Medinet listings implied a category constraint.
    # New inline rules state that path constraint explicitly, outside the collector.
    if profile == "hcm_medinet" and listing_url:
        category = re.search(r"-c(\d+)\.aspx$", urlsplit(listing_url).path, re.I)
        if category:
            rules.article_urls.include_path_patterns = [
                rf".*-[cC]{category[1]}-\d+\.[aA][sS][pP][xX]"
            ]
    return rules
