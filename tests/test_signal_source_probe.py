"""Source discovery boundaries; synthetic cases do not certify editorial relevance."""

import httpx
import pytest

from healthlab.signal.collection import load_documents
from healthlab.signal.source_probe import (
    MAX_BYTES,
    ProbeTarget,
    ProbeTargets,
    page_inventory,
    probe_sources,
    topic_hint,
)
from healthlab.store import RunStore


def target():
    return ProbeTarget(
        source_id="who",
        organization="Synthetic WHO fixture",
        url="https://example.org/health-topics/ageing",
        article_path_prefixes=["/news/item/"],
    )


def test_inventory_keeps_original_urls_and_rejects_offsite_or_template_links():
    page = b"""<html><head>
    <link type="application/rss+xml" href="/feed" />
    <link type="application/rss+xml" href="https://outside.org/feed" />
    </head><body><script><a href="/news/item/invented">Longevity</a></script>
    <a href="/news/item/older-people" aria-label="Older people and long-term care">Read</a>
    <a href="/news/item/older-people#details">Duplicate</a>
    <a href="https://outside.org/news/item/ageing">Longevity</a>
    <a href="/about">About</a><a href="/news/item#:ItemDefaultUrl #">Template</a>
    <a href="https://user:secret@example.org/news/item/ageing">Credentials</a>
    </body></html>"""
    inventory = page_inventory(page, target())
    assert [item["url"] for item in inventory["candidates"]] == [
        "https://example.org/news/item/older-people"
    ]
    assert inventory["candidates"][0]["topic_hint"]["topic"] == "aging_society"
    assert inventory["advertised_feeds"] == ["https://example.org/feed"]
    assert inventory["freshness"] == "not_evaluated"


@pytest.mark.parametrize(
    "title,topic",
    [
        ("Chăm sóc NGƯỜI CAO TUỔI tại cộng đồng", "aging_society"),
        ("New Healthy-Ageing report", "longevity"),
        ("Older adults and longevity", "both"),
        ("Cấp phép cơ sở khám chữa bệnh", None),
        ("Sức khỏe người trẻ và bí quyết tăng tuổi thọ", None),
    ],
)
def test_topic_hints_preserve_uncertainty_and_handle_vietnamese(title, topic):
    hint = topic_hint(title)
    assert hint["topic"] == topic
    assert hint["status"] == ("candidate" if topic else "needs_review")
    assert hint["semantic_support"] == "not_evaluated"


def test_probe_preserves_raw_and_failure_in_the_same_run(tmp_path):
    targets = ProbeTargets(
        sources=[
            target(),
            ProbeTarget(
                source_id="moh",
                organization="Synthetic ministry fixture",
                url="https://moh.example.org/",
            ),
        ]
    )
    raw = b'<a href="/news/item/ageing">Healthy ageing</a>'

    def response(request):
        if request.url.host == "moh.example.org":
            raise httpx.ConnectError("synthetic failure", request=request)
        return httpx.Response(200, content=raw, headers={"Content-Type": "text/html"})

    store = RunStore(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        run = probe_sources(store, targets, client=client)
    assert run["status"] == "partial"
    assert store.read(run["source_checks"][0]["raw_sha256"]) == raw
    assert run["source_checks"][1]["errors"] == [{"code": "ConnectError"}]
    assert store.load(run["run_id"])["source_checks"] == run["source_checks"]
    # A probe cannot silently become an ingested collection used to create a newsletter.
    with pytest.raises(ValueError, match="collection"):
        load_documents(store, run)
    assert not run["documents"]


def test_redirect_is_reported_and_not_followed(tmp_path):
    requested = []

    def response(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://outside.org/"})

    with httpx.Client(transport=httpx.MockTransport(response), follow_redirects=True) as client:
        run = probe_sources(RunStore(tmp_path), ProbeTargets(sources=[target()]), client=client)
    assert requested == [target().url]
    assert run["status"] == "failed"
    assert run["source_checks"][0]["status"] == "redirect_requires_review"


@pytest.mark.parametrize(
    "status,content,content_type,code",
    [
        (503, b"maintenance", "text/html", "probe_http"),
        (200, b"x" * (MAX_BYTES + 1), "text/html", "probe_limit"),
        (200, b"%PDF-synthetic", "application/pdf", "probe_content_type"),
    ],
)
def test_probe_does_not_report_unusable_sources_as_reachable(
    tmp_path, status, content, content_type, code
):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            status, content=content, headers={"Content-Type": content_type}
        )
    )
    with httpx.Client(transport=transport) as client:
        run = probe_sources(RunStore(tmp_path), ProbeTargets(sources=[target()]), client=client)
    assert run["status"] == "failed"
    assert run["source_checks"][0]["errors"] == [{"code": code}]


def test_reachable_page_with_no_articles_is_not_called_complete_monitoring(tmp_path):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, content=b"<p>No articles on this page</p>", headers={"Content-Type": "text/html"}
        )
    )
    with httpx.Client(transport=transport) as client:
        run = probe_sources(RunStore(tmp_path), ProbeTargets(sources=[target()]), client=client)
    assert run["status"] == "completed"
    check = run["source_checks"][0]
    assert check["status"] == "reachable" and not check["candidates"]
    assert "no article fetch" in check["coverage"]
    assert check["freshness"] == "not_evaluated"
