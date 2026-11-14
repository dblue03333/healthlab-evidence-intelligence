"""Source boundary: discovery and article decoding; shared engine owns IO/audit.

Adapters receive the bounded fetcher, never construct clients or own retries.
The current contract emits text articles; time-series need a separate schema.
"""

from collections.abc import Callable
from typing import Any, Protocol

from healthlab.common import JsonObject
from healthlab.signal.contracts import Source
from healthlab.signal.html_adapter import ConfiguredHTMLAdapter
from healthlab.signal.source_http import HTMLFetcher
from healthlab.signal.storage import CollectionStorage
from healthlab.store import RunStore


class SourceAdapter(Protocol):
    def discover(self, store, fetcher, checkpoint, save, *, mindate, maxdate): ...

    def fetch_article(self, item, fetcher, checkpoint, save): ...


class HTMLSourceAdapter:
    def __init__(self, source: Source) -> None:
        self.source = source
        self.parser = ConfiguredHTMLAdapter(source.resolved_html_rules)

    def discover(
        self,
        store: RunStore | CollectionStorage,
        fetcher: HTMLFetcher,
        checkpoint: JsonObject,
        save: Callable,
        *,
        mindate,
        maxdate,
    ) -> JsonObject:
        from healthlab.signal.discovery import discover_html_listings

        return discover_html_listings(
            store, self.source, fetcher, checkpoint, save, mindate=mindate, maxdate=maxdate
        )

    def fetch_article(
        self, item: JsonObject, fetcher: HTMLFetcher, checkpoint: JsonObject, save: Callable
    ) -> Any:
        raw, digest = fetcher.fetch(item["url"], checkpoint, save)
        return raw, digest, self.parser.parse_article(raw, url=item["url"])


class MinistryPortalAdapter(HTMLSourceAdapter):
    def discover(
        self,
        store: RunStore | CollectionStorage,
        fetcher: HTMLFetcher,
        checkpoint: JsonObject,
        save: Callable,
        *,
        mindate,
        maxdate,
    ) -> JsonObject:
        from healthlab.signal.moh_portal import discover_portal

        return discover_portal(
            self.source, fetcher, checkpoint, save, mindate=mindate, maxdate=maxdate
        )

    def fetch_article(
        self, item: dict[str, str], fetcher: HTMLFetcher, checkpoint: JsonObject, save: Callable
    ) -> tuple[bytes, str, JsonObject]:
        from healthlab.signal.moh_portal import fetch_article

        return fetch_article(self.source, item, fetcher, checkpoint, save)


class WordPressAdapter(HTMLSourceAdapter):
    def discover(
        self,
        store: CollectionStorage | RunStore,
        fetcher: HTMLFetcher,
        checkpoint: JsonObject,
        save: Callable,
        *,
        mindate,
        maxdate,
    ) -> JsonObject:
        from healthlab.signal.wordpress_discovery import discover_wordpress

        return discover_wordpress(
            self.source, fetcher, checkpoint, save, mindate=mindate, maxdate=maxdate
        )


def source_adapter_for(source: Source) -> SourceAdapter:
    discovery = source.html_discovery
    if discovery and discovery.moh_portal_categories:
        return MinistryPortalAdapter(source)
    if discovery and discovery.wordpress_api_url:
        return WordPressAdapter(source)
    return HTMLSourceAdapter(source)
