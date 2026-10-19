"""Config-only source onboarding, URL scope and extraction contract regressions."""

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from healthlab.models import IngestionError
from healthlab.signal.collection import collect, load_documents
from healthlab.signal.contracts import Registry, Source
from healthlab.signal.html_adapter import ConfiguredHTMLAdapter
from healthlab.signal.html_rules import HTMLSourceRules
from healthlab.store import RunStore
from tests.test_signal_html_sources import TEXT

ROOT = Path(__file__).resolve().parents[1]


def rules_config():
    example = json.loads((ROOT / "config/signal_sources_html_example.json").read_bytes())
    return example["sources"][0]["html_rules"]


def configured_source(rules=None):
    return Source(
        source_id="new_source",
        organization="Synthetic bulletin",
        kind="html",
        url="https://bulletin.example.org/news",
        usage_policy="Synthetic fixture, no network",
        html_rules=rules or rules_config(),
    )


def adapter():
    return ConfiguredHTMLAdapter(HTMLSourceRules.model_validate(rules_config()))


@pytest.mark.parametrize(
    "href,accepted,reason",
    [
        ("/articles/a#part", True, "included_path"),
        ("/articles/a/b", False, "unmatched_path"),
        ("/articles/about", False, "excluded_path"),
        ("/articles/category-care", False, "excluded_path_pattern"),
        ("/other/articles/a", False, "unmatched_path"),
        ("http://bulletin.example.org/articles/a", False, "outside_source_scope"),
        ("https://bulletin.example.org.evil.org/articles/a", False, "outside_source_scope"),
        ("https://user@bulletin.example.org/articles/a", False, "outside_source_scope"),
        ("https://bulletin.example.org:8443/articles/a", False, "outside_source_scope"),
        ("//other.example.org/articles/a", False, "outside_source_scope"),
        ("https://[bad/articles/a", False, "invalid_url"),
    ],
)
def test_url_rules_apply_before_fetch(href, accepted, reason):
    result = adapter().classify_url(href, "https://bulletin.example.org/news")
    assert (result.accepted, result.reason) == (accepted, reason)
    if accepted:
        assert "#" not in result.url


def test_article_query_identity_is_preserved_and_missing_or_duplicate_id_rejected():
    config = rules_config()
    config["article_urls"]["required_query_parameters"] = {"id": r"\d+"}
    custom = ConfiguredHTMLAdapter(HTMLSourceRules.model_validate(config))
    base = "https://bulletin.example.org/news"
    result = custom.classify_url("/article.aspx?id=42&utm_source=feed#intro", base)
    assert result.accepted and result.url == "https://bulletin.example.org/article.aspx?id=42"
    for url in ("/article.aspx", "/article.aspx?id=bad", "/article.aspx?id=42&id=43"):
        assert custom.classify_url(url, base).reason == "query_constraint"


def test_new_source_runs_from_json_config_alone_and_keeps_rules_provenance(tmp_path):
    registry = Registry.model_validate_json(
        json.dumps(
            {"schema_version": "signal-registry-3", "sources": [configured_source().model_dump()]}
        )
    )
    raw = (
        '<aside><h1>Sidebar</h1><p class="intro">Wrong lead</p></aside>'
        '<article><h1>New source article</h1><p class="intro">Correct lead</p>'
        '<time data-kind="published" datetime="2026-10-09"></time>'
        '<time data-kind="updated" datetime="2026-10-10"></time>'
        f'<section id="article-body">{TEXT}</section></article>'
    ).encode()
    requests = []
    listing = (
        b'<a href="/articles/a?utm_source=one#intro">Community programme</a>'
        b'<a href="/articles/a?utm_source=two">Same article</a>'
        b'<a href="/articles/about">About</a>'
        b'<a href="/articles/category-care">Category</a><a href="/archive">Archive</a>'
        b'<a href="https://other.example.org/articles/a">External</a>'
        b'<a href="https://[bad/articles/a">Malformed</a>'
    )

    def respond(request):
        requests.append(str(request.url))
        assert str(request.url) in {
            "https://bulletin.example.org/news",
            "https://bulletin.example.org/articles/a",
        }
        return httpx.Response(
            200,
            content=listing if request.url.path == "/news" else raw,
            headers={"Content-Type": "text/html"},
        )

    store = RunStore(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        first = collect(
            store,
            registry,
            mindate=date(2026, 10, 3),
            maxdate=date(2026, 10, 10),
            rss_client=client,
        )
        second = collect(
            store,
            registry,
            mindate=date(2026, 10, 3),
            maxdate=date(2026, 10, 10),
            rss_client=client,
        )
    assert first["status"] == second["status"] == "completed"
    assert len(requests) == 4  # One listing + one deduplicated article each run.
    assert second["documents"][0]["change"] == "unchanged"
    assert first["documents"][0]["snapshot_id"] == second["documents"][0]["snapshot_id"]
    doc = next(iter(load_documents(store, second).values()))
    assert doc.publication_date == "2026-10-09"
    assert "Correct lead" in doc.text and "Wrong lead" not in doc.text
    check = second["source_checks"][0]
    assert {d["reason"] for d in check["excluded_url_rules"]} == {
        "excluded_path",
        "excluded_path_pattern",
        "unmatched_path",
        "normalized_duplicate",
    }
    assert (
        json.loads(store.read(check["rules_sha256"])) == registry.sources[0].html_rules.model_dump()
    )
    packet = json.loads(store.read(check["article_checks"][0]["extraction_sha256"]))
    assert packet["rules_sha256"] == check["rules_sha256"]
    assert packet["modified_date_values"] == ["2026-10-10"]
    assert packet["publication_date_evidence"][0]["locator"].endswith("@datetime")
    assert all(p["text"] in doc.text for p in packet["passages"])
    # A rule revision gets its own immutable lineage without creating a false article update.
    registry.sources[0].html_rules.revision = 2
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        revised = collect(
            store,
            registry,
            mindate=date(2026, 10, 3),
            maxdate=date(2026, 10, 10),
            rss_client=client,
        )
    revised_check = revised["source_checks"][0]
    assert revised_check["rule_revision"] == 2
    assert revised_check["rules_sha256"] != check["rules_sha256"]
    assert json.loads(store.read(check["rules_sha256"]))["revision"] == 1
    assert revised["documents"][0]["snapshot_id"] == second["documents"][0]["snapshot_id"]
    assert revised["documents"][0]["change"] == "unchanged"


@pytest.mark.parametrize("change", ["missing_scope", "ambiguous_body", "ambiguous_title"])
def test_configured_source_dom_changes_fail_visibly(change):
    heading = "<h1>Article</h1>" * (2 if change == "ambiguous_title" else 1)
    body = f'<section id="article-body">{TEXT}</section>' * (2 if change == "ambiguous_body" else 1)
    raw = heading + body
    if change != "missing_scope":
        raw = "<article>" + raw + "</article>"
    with pytest.raises(IngestionError) as error:
        adapter().parse_article(raw.encode())
    assert error.value.code == "html_article_shape"


@pytest.mark.parametrize("change", ["regex", "host", "selector", "date_group", "drop_id"])
def test_invalid_rules_fail_at_config_load(change):
    config = rules_config()
    if change == "regex":
        config["article_urls"]["include_path_patterns"] = ["["]
    elif change == "host":
        config["host"] = "*.example.org"
    elif change == "selector":
        config["body"] = {}
    elif change == "date_group":
        config["text_dates"][0]["pattern"] = r"Date: (.*)"
    else:
        config["article_urls"]["required_query_parameters"] = {"id": r"\d+"}
        config["article_urls"]["drop_query_parameters"] = ["id"]
    with pytest.raises(ValueError):
        configured_source(config)


def test_rules_source_and_registry_contracts_are_unambiguous():
    config = configured_source().model_dump()
    with pytest.raises(ValueError, match="exactly one"):
        Source.model_validate(config | {"html_profile": "who_news"})
    with pytest.raises(ValueError, match="official host"):
        Source.model_validate(config | {"url": "https://other.example.org/news"})
    with pytest.raises(ValueError, match="signal-registry-3"):
        Registry(schema_version="signal-registry-2", sources=[configured_source()])
    assert Registry.model_validate_json((ROOT / "config/signal_sources.json").read_bytes())
    example = Registry.model_validate_json(
        (ROOT / "config/signal_sources_html_example.json").read_bytes()
    )
    assert example.sources[0].enabled is False


def test_legacy_profile_preserves_listing_category_scope():
    from tests.test_signal_html_sources import source

    legacy = Source.model_validate(
        source("hcm_medinet").model_dump()
        | {"url": "https://medinet.hochiminhcity.gov.vn/tin-tuc-c1780.aspx"}
    )
    selected = ConfiguredHTMLAdapter(legacy.resolved_html_rules)
    assert selected.classify_url("/news/a-c1780-1.aspx", legacy.url).accepted
    assert not selected.classify_url("/news/a-c99-1.aspx", legacy.url).accepted
