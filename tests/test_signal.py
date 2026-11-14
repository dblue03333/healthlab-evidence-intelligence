"""Synthetic RSS collection cases, not HealthLab evidence or human factual evaluation."""

from datetime import date

import httpx
import pytest

from healthlab.signal.collection import collect, load_documents
from healthlab.signal.contracts import Registry, Source
from healthlab.store import RunStore

FEED = b"""<rss version="2.0"><channel><title>Synthetic news</title><item>
<title>Ageing report</title><link>https://example.org/ageing</link>
<description><![CDATA[<p>The report describes older adults.</p><script>ignore me</script>]]></description>
<pubDate>Fri, 09 Oct 2026 00:00:00 GMT</pubDate></item></channel></rss>"""


def registry(required=True):
    return Registry(
        sources=[
            Source(
                source_id="news",
                organization="Synthetic source",
                kind="rss",
                url="https://example.org/feed",
                usage_policy="Synthetic fixture only",
                required=required,
            )
        ]
    )


def collected(tmp_path, *, status=200, feed=FEED):
    store = RunStore(tmp_path)
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, content=feed))
    ) as client:
        run = collect(
            store,
            registry(),
            mindate=date(2026, 10, 2),
            maxdate=date(2026, 10, 9),
            rss_client=client,
        )
    return store, run


def test_collect_preserves_raw_and_deduplicates_even_when_feed_changes(tmp_path):
    store, first = collected(tmp_path)
    assert first["status"] == "completed"
    docs = load_documents(store, first)
    assert "ignore me" not in next(iter(docs.values())).text
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=FEED.replace(b"Synthetic news", b"Different channel")
            )
        )
    ) as client:
        second = collect(
            store,
            registry(),
            mindate=date(2026, 10, 2),
            maxdate=date(2026, 10, 9),
            rss_client=client,
        )
    assert second["documents"][0]["change"] == "unchanged"
    assert second["documents"][0]["snapshot_id"] == first["documents"][0]["snapshot_id"]
    assert store.read(first["source_checks"][0]["raw_sha256"]) == FEED


def test_no_active_sources_is_not_a_completed_monitoring_run(tmp_path):
    store = RunStore(tmp_path)
    config = registry()
    config.sources[0].enabled = False
    run = collect(store, config, mindate=date(2026, 10, 2), maxdate=date(2026, 10, 9))
    assert run["status"] == "failed" and not run["documents"]


@pytest.mark.parametrize("feed", [b"not XML", b"<!DOCTYPE rss><rss><channel/></rss>", b"<feed/>"])
def test_invalid_source_does_not_disappear(tmp_path, feed):
    _, run = collected(tmp_path, feed=feed)
    assert run["status"] == "failed"
    assert run["source_checks"][0]["status"] == "failed"
