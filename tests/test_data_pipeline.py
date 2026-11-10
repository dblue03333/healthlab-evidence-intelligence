"""Dataset acceptance: complete ledgers, immutable lineage, recovery and honest gaps."""

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from healthlab.data.__main__ import main
from healthlab.data.bundle import export_dataset, verify_export
from healthlab.data.datasets import build_dataset, gold_records, read_dataset
from healthlab.models import IngestionError
from healthlab.signal.collection import (
    collect,
    repair_collection,
    replay_collection,
    resume_collection,
)
from healthlab.signal.contracts import Registry
from healthlab.signal.source_http import FetchPolicy
from healthlab.store import RunStore, json_bytes
from tests.test_signal_discovery import HOST, article, card, source

WINDOW = {"mindate": date(2026, 10, 12), "maxdate": date(2026, 10, 16)}
POLICY = FetchPolicy(max_attempts=1, request_interval=0)


def test_previously_published_v1_dataset_keeps_its_identity_and_membership(tmp_path):
    import base64
    import gzip

    fixture = Path(__file__).parent / "fixtures/data/dataset_v1.json.gz"
    saved = json.loads(gzip.decompress(fixture.read_bytes()))
    store = RunStore(tmp_path)
    for ref, content in saved["objects"].items():
        assert store.put(base64.b64decode(content)) == ref
    records = gold_records(store, saved["dataset_id"])
    assert len(records) == 1 and records[0]["url"] == HOST + "/article/a"
    assert records[0]["publication_date"] == "2026-10-14"


def capture(store, listing=None, *, pages=None, src=None, remember=True):
    requests = []
    pages = pages or {}

    def respond(request):
        requests.append(str(request.url))
        value = pages.get(
            str(request.url), listing if str(request.url) == HOST + "/news" else article()
        )
        if isinstance(value, int):
            return httpx.Response(value)
        return httpx.Response(200, text=value or card("a"), headers={"content-type": "text/html"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        run = collect(
            store,
            Registry(schema_version="signal-registry-3", sources=[src or source()]),
            **WINDOW,
            rss_client=client,
            fetch_policy=POLICY,
            remember_candidates=remember,
        )
    return run, requests


def test_date_skips_do_not_spend_budget_before_in_window_articles(tmp_path):
    src = source(max_article_fetches=1)
    src.html_discovery.skip_outside_window = True
    run, requests = capture(RunStore(tmp_path), card("new", "2026-10-20") + card("wanted"), src=src)
    assert run["status"] == "completed" and len(run["documents"]) == 1
    assert HOST + "/article/new" not in requests and HOST + "/article/wanted" in requests
    result = build_dataset(RunStore(tmp_path), run["run_id"])
    assert result["status"] == "ready" and result["sources"][0]["outcomes"] == {
        "included": 1,
        "outside_window": 1,
    }


def test_same_listing_conflicting_dates_are_not_borrowed_from_article_metadata(tmp_path):
    store = RunStore(tmp_path)
    run, _ = capture(
        store,
        card("a") + card("a", "2026-10-15"),
        pages={HOST + "/article/a": article("2026-10-14")},
    )
    result = build_dataset(store, run["run_id"])
    assert result["status"] == "partial" and result["documents"] == 0
    assert any(i["code"] == "html_date_conflict" for i in result["sources"][0]["issues"])


def test_lost_listing_link_is_refetched_with_historical_date_provenance(tmp_path):
    store = RunStore(tmp_path)
    first, _ = capture(store, card("a") + card("b"))
    first_bytes = (store.root / "runs" / first["run_id"] / "manifest.json").read_bytes()
    second, requests = capture(store, card("a"))
    assert HOST + "/article/b" in requests
    result = build_dataset(store, second["run_id"])
    records = gold_records(store, result["dataset_id"])
    assert len(records) == 2 and result["sources"][0]["recovered_from_history"] == 1
    recovered = next(r for r in records if r["url"].endswith("/b"))
    assert recovered["recovered_from_history"]
    assert recovered["publication_date_evidence"][0]["historical"]
    assert recovered["publication_date_evidence"][0]["collection_manifest_sha256"]
    assert {d["snapshot_id"] for d in first["documents"]} == {
        d["snapshot_id"] for d in second["documents"]
    }
    assert first_bytes == (store.root / "runs" / first["run_id"] / "manifest.json").read_bytes()


def test_missing_historical_article_is_retained_as_gap_not_old_gold_success(tmp_path):
    store = RunStore(tmp_path)
    capture(store, card("a") + card("b"))
    second, _ = capture(store, card("a"), pages={HOST + "/article/b": 404})
    result = build_dataset(store, second["run_id"])
    assert result["status"] == "partial" and result["sources"][0]["outcomes"]["failed"] == 1
    with pytest.raises(IngestionError, match="unresolved"):
        gold_records(store, result["dataset_id"])
    assert len(gold_records(store, result["dataset_id"], allow_partial=True)) == 1


def test_history_retries_failed_candidate_and_budget_changes_do_not_forget_it(tmp_path):
    store = RunStore(tmp_path)
    first, _ = capture(store, card("a") + card("b"), src=source(max_article_fetches=1))
    assert first["source_checks"][0]["deferred_urls"]
    second, requests = capture(
        store,
        card("a"),
        pages={HOST + "/article/b": article("2026-10-14")},
        src=source(max_article_fetches=3),
    )
    assert HOST + "/article/b" in requests and len(second["documents"]) == 2


def test_scope_changes_do_not_import_old_candidates(tmp_path):
    store = RunStore(tmp_path)
    capture(store, card("a") + card("b"))
    src = source()
    src.html_rules.article_urls.include_path_patterns = [r"/article/a"]
    second, requests = capture(store, card("a"), src=src)
    assert HOST + "/article/b" not in requests and len(second["documents"]) == 1


def test_new_publication_date_outside_window_supersedes_historical_candidate(tmp_path):
    store = RunStore(tmp_path)
    capture(store, card("a") + card("b"))
    src = source()
    src.html_discovery.skip_outside_window = True
    # Keep the same config across runs to exercise actual reconciliation.
    capture(store, card("a") + card("b"), src=src)
    second, requests = capture(store, card("a") + card("b", "2026-10-20"), src=src)
    result = build_dataset(store, second["run_id"])
    assert result["status"] == "ready" and result["documents"] == 1
    assert HOST + "/article/b" not in requests


def test_dataset_is_deterministic_and_new_body_keeps_older_version(tmp_path):
    store = RunStore(tmp_path)
    first, _ = capture(store, card("a"))
    result = build_dataset(store, first["run_id"])
    assert result == build_dataset(store, first["run_id"])
    original = gold_records(store, result["dataset_id"])[0]
    second, _ = capture(
        store,
        card("a"),
        pages={HOST + "/article/a": article().replace("community", "revised community")},
    )
    updated = gold_records(store, build_dataset(store, second["run_id"])["dataset_id"])[0]
    assert original["document_id"] == updated["document_id"]
    assert original["content_version"] != updated["content_version"]
    assert updated["previous_snapshot_id"] == original["snapshot_id"]
    assert gold_records(store, result["dataset_id"])[0] == original


def test_replay_reparses_offline_and_preserves_failed_cache_misses(tmp_path, monkeypatch):
    store = RunStore(tmp_path)
    first, _ = capture(store, card("a") + card("b"), pages={HOST + "/article/b": 503})
    before = (store.root / "runs" / first["run_id"] / "manifest.json").read_bytes()
    monkeypatch.setattr(
        "healthlab.signal.collection.source_client",
        lambda: pytest.fail("Replay must not create an HTTP client"),
    )
    replayed = replay_collection(store, first["run_id"])
    assert replayed["run_id"] != first["run_id"]
    assert len(replayed["documents"]) == 1
    result = build_dataset(store, replayed["run_id"])
    assert result["status"] == "partial" and result["capture_kind"] == "replay"
    assert any(i["code"] == "replay_cache_miss" for i in result["sources"][0]["issues"])
    assert before == (store.root / "runs" / first["run_id"] / "manifest.json").read_bytes()


def test_replay_rebuilds_extraction_results_not_just_copying_old_decisions(tmp_path, monkeypatch):
    from healthlab.signal.html_adapter import ConfiguredHTMLAdapter

    store = RunStore(tmp_path)
    original_parse = ConfiguredHTMLAdapter.parse_article
    monkeypatch.setattr(
        ConfiguredHTMLAdapter,
        "parse_article",
        lambda *a, **kw: (_ for _ in ()).throw(
            IngestionError("html_article_shape", "Old parser failed")
        ),
    )
    first, _ = capture(store, card("a"))
    assert not first["documents"]
    monkeypatch.setattr(ConfiguredHTMLAdapter, "parse_article", original_parse)
    monkeypatch.setattr(
        "healthlab.signal.collection.source_client",
        lambda: pytest.fail("Replay must remain offline"),
    )
    replayed = replay_collection(store, first["run_id"])
    assert len(replayed["documents"]) == 1
    assert build_dataset(store, replayed["run_id"])["status"] == "ready"


def test_repair_fetches_only_missing_responses_with_new_audited_budget(tmp_path):
    store = RunStore(tmp_path)
    first, _ = capture(store, card("a") + card("b"), pages={HOST + "/article/b": 503})
    old_checkpoint = json.loads(store.read(first["source_checks"][0]["checkpoint_sha256"]))
    requests = []

    def respond(request):
        requests.append(str(request.url))
        assert str(request.url) == HOST + "/article/b"
        return httpx.Response(200, text=article(), headers={"content-type": "text/html"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        repaired = repair_collection(store, first["run_id"], rss_client=client)
    assert requests == [HOST + "/article/b"]
    result = build_dataset(store, repaired["run_id"])
    assert result["status"] == "ready" and result["documents"] == 2
    assert result["capture_kind"] == "repair"
    checkpoint = json.loads(store.read(repaired["source_checks"][0]["checkpoint_sha256"]))
    assert len(checkpoint["http_attempts"]) == 1
    assert (
        checkpoint["responses"][HOST + "/article/a"]
        == old_checkpoint["responses"][HOST + "/article/a"]
    )
    assert store.load(first["run_id"])["status"] == "partial"


def test_repair_preserves_server_cooldown_across_explicit_new_runs(tmp_path):
    store = RunStore(tmp_path)

    def limited(request):
        return (
            httpx.Response(200, text=card("a"), headers={"content-type": "text/html"})
            if request.url.path == "/news"
            else httpx.Response(429, headers={"retry-after": "3600"})
        )

    with httpx.Client(transport=httpx.MockTransport(limited)) as client:
        run = collect(
            store,
            Registry(schema_version="signal-registry-3", sources=[source()]),
            **WINDOW,
            rss_client=client,
            fetch_policy=POLICY,
        )
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: pytest.fail("Must respect saved Retry-After"))
    ) as client:
        repaired = repair_collection(store, run["run_id"], rss_client=client)
    result = build_dataset(store, repaired["run_id"])
    assert result["status"] == "partial" and result["sources"][0]["http_attempts"] == 0
    assert any(i["code"] == "html_retry_cooldown" for i in result["sources"][0]["issues"])


def test_repair_cached_old_body_cannot_roll_back_new_live_index(tmp_path):
    from healthlab.signal.storage import collection_storage

    store = RunStore(tmp_path)
    old, _ = capture(store, card("a") + card("b"), pages={HOST + "/article/b": 503})
    capture(
        store,
        card("a") + card("b"),
        pages={HOST + "/article/a": article().replace("community", "changed community")},
    )
    index = collection_storage(store).state.load_index()
    current_a = next(
        v for v in index.values() if v["document_id"] == old["documents"][0]["document_id"]
    )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text=article(), headers={"content-type": "text/html"})
        )
    ) as client:
        repair_collection(store, old["run_id"], rss_client=client)
    after = collection_storage(store).state.load_index()
    assert (
        next(v for v in after.values() if v["document_id"] == current_a["document_id"]) == current_a
    )


def test_every_listing_candidate_has_an_outcome_even_if_summary_omits_it(tmp_path):
    store = RunStore(tmp_path)
    run, _ = capture(store, card("a"))
    run["source_checks"][0]["listing_pages"][0]["candidate_urls"].append(HOST + "/article/lost")
    store.save(run)  # Simulate a damaged writer, not a valid release.
    result = build_dataset(store, run["run_id"])
    assert result["status"] == "partial"
    assert result["sources"][0]["outcomes"]["deferred"] == 1


def test_export_is_self_contained_and_detects_raw_or_derived_tampering(tmp_path):
    store = RunStore(tmp_path / "original")
    run, _ = capture(store, card("a") + card("b"))
    latest, _ = capture(store, card("a"))
    result = build_dataset(store, latest["run_id"])
    destination = tmp_path / "portable"
    export_dataset(store, result["dataset_id"], destination)
    assert verify_export(destination)["verified"]
    restored = RunStore(destination)
    assert len(gold_records(restored, result["dataset_id"])) == 2
    assert not list((destination / "runs").iterdir())  # No original run manifests required.
    expected = (destination / "gold.json").read_bytes()
    (destination / "gold.json").write_text("{}")
    with pytest.raises(ValueError, match="Derived export"):
        verify_export(destination)
    (destination / "gold.json").write_bytes(expected)
    record = gold_records(restored, result["dataset_id"])[0]
    (destination / "objects" / record["raw_sha256"]).write_bytes(b"damaged raw")
    with pytest.raises(IngestionError):
        verify_export(destination)


def test_recomputed_hash_cannot_legitimize_forged_gold_membership(tmp_path):
    store = RunStore(tmp_path)
    run, _ = capture(store, card("a"))
    result = build_dataset(store, run["run_id"])
    dataset, _, _, gold = read_dataset(store, result["dataset_id"])
    gold.document_ids = []
    dataset.gold_sha256 = store.put(json_bytes(gold.model_dump(mode="json")))
    forged = store.put(json_bytes(dataset.model_dump(mode="json")))
    with pytest.raises(IngestionError, match="frozen inputs"):
        read_dataset(store, forged)


def test_cli_requires_explicit_dates_and_refuses_non_html_sources(tmp_path, capsys):
    with pytest.raises(SystemExit):
        main(["--store", str(tmp_path), "collect"])
    registry = Path(__file__).resolve().parents[1] / "config/signal_sources_legacy_sample.json"
    assert (
        main(
            [
                "--store",
                str(tmp_path),
                "collect",
                "--registry",
                str(registry),
                "--mindate",
                "2026-10-12",
                "--maxdate",
                "2026-10-16",
            ]
        )
        == 1
    )
    assert '"status": "failed"' in capsys.readouterr().out


def test_cli_build_show_catalog_and_partial_exit_code(tmp_path, capsys):
    store = RunStore(tmp_path)
    run, _ = capture(store, card("a"), pages={HOST + "/article/a": 404})
    assert main(["--store", str(tmp_path), "build", run["run_id"]]) == 1
    result = json.loads(capsys.readouterr().out)
    assert main(["--store", str(tmp_path), "show", result["dataset_id"]]) == 1
    capsys.readouterr()
    assert main(["--store", str(tmp_path), "catalog"]) == 0
    assert len(json.loads(capsys.readouterr().out)["datasets"]) == 1


def test_resume_uses_frozen_history_and_does_not_repeat_completed_requests(tmp_path):
    store = RunStore(tmp_path)
    capture(store, card("a") + card("b"))

    def interrupt(request):
        if request.url.path == "/article/b":
            raise KeyboardInterrupt()
        return httpx.Response(
            200,
            text=card("a") if request.url.path == "/news" else article(),
            headers={"content-type": "text/html"},
        )

    policy = FetchPolicy(max_attempts=2, request_interval=0)
    with (
        httpx.Client(transport=httpx.MockTransport(interrupt)) as client,
        pytest.raises(KeyboardInterrupt),
    ):
        collect(
            store,
            Registry(schema_version="signal-registry-3", sources=[source()]),
            **WINDOW,
            rss_client=client,
            fetch_policy=policy,
            remember_candidates=True,
        )
    pending = next(
        store.load(p.parent.name)
        for p in (store.root / "runs").glob("*/manifest.json")
        if store.load(p.parent.name)["status"] == "running"
    )
    requests = []

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(200, text=article(), headers={"content-type": "text/html"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        resumed = resume_collection(store, pending["run_id"], rss_client=client)
    assert requests == [HOST + "/article/b"]
    assert build_dataset(store, resumed["run_id"])["documents"] == 2


def test_replaying_old_capture_cannot_roll_back_newer_live_catalog(tmp_path):
    from healthlab.signal.storage import collection_storage

    store = RunStore(tmp_path)
    first, _ = capture(store, card("a"))
    second, _ = capture(
        store,
        card("a"),
        pages={HOST + "/article/a": article().replace("community", "updated community")},
    )
    before = collection_storage(store).state.load_index()
    replay_collection(store, first["run_id"])
    assert collection_storage(store).state.load_index() == before
    assert next(iter(before.values()))["run_id"] == second["run_id"]


def test_all_three_default_connectors_publish_same_medallion_contract(tmp_path):
    from tests.test_signal_moh_portal import detail, listing

    root = Path(__file__).resolve().parents[1]
    registry = Registry.model_validate_json((root / "config/signal_sources.json").read_bytes())
    who = registry.sources[2]
    who_seeds = [who.url] + who.html_discovery.additional_listing_urls
    text = "Written health policy content for the pipeline fixture. " * 8

    def respond(request):
        if request.url.path == "/api/news/newsitems":
            import gzip

            return httpx.Response(
                200,
                content=gzip.decompress(
                    (
                        root / "tests/fixtures/signal_html/who_ageing_widget_20261028.json.gz"
                    ).read_bytes()
                ),
                headers={"content-type": "application/json"},
            )
        if request.url.path == "/api/news/speeches":
            return httpx.Response(200, json={"@odata.count": 0, "value": []})
        if request.url.host == "moh.gov.vn":
            payload = json.loads(request.content)
            if "MA_DANH_MUC" in payload:
                value = listing()
                value[0]["MA"] = payload["MA_DANH_MUC"]
                return httpx.Response(200, json=value)
            return httpx.Response(200, json=detail())
        if request.url.host == "medinet.hochiminhcity.gov.vn":
            if request.url.path == "/Search.aspx":
                return httpx.Response(
                    200,
                    text='<span id="plcall_Search1_lblMessage">Hệ thống tìm thấy 0 kết quả</span><table id="plcall_Search1_grvmn"></table><input type="hidden" name="__VIEWSTATE" value="state"><input type="hidden" name="__EVENTVALIDATION" value="validation">',
                    headers={"content-type": "text/html"},
                )
            content = (
                f'<h1>Health report</h1><div class="ContentODM">{text}</div>'
                if request.url.path.endswith("-12.aspx")
                else '<div id="plcall_dtn_UpdatePanel1"><article><a href="/health-c1780-12.aspx">Health report</a><span class="time">09/10/2026</span></article></div>'
            )
        elif str(request.url) in who_seeds:
            index = who_seeds.index(str(request.url))
            content = f'<article><a href="/news/item/w-{index}"><span class="timestamp">9 October 2026</span>WHO report</a></article>'
            if str(request.url) == who.url:
                from urllib.parse import urlencode

                from healthlab.signal.who_inventory import AGEING_QUERY

                content += (
                    '<script>read: "/api/news/newsitems?' + urlencode(AGEING_QUERY) + '"</script>'
                )
        else:
            content = f'<meta property="article:published_time" content="2026-10-09"><h1>WHO report</h1><div class="sf-detail-body-wrapper">{text}</div>'
        return httpx.Response(200, text=content, headers={"content-type": "text/html"})

    store = RunStore(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        run = collect(
            store,
            registry,
            mindate=date(2026, 10, 7),
            maxdate=date(2026, 10, 13),
            rss_client=client,
            fetch_policy=POLICY,
            remember_candidates=True,
        )
    result = build_dataset(store, run["run_id"])
    assert result["status"] == "ready" and result["documents"] == 7
    assert {s["source_id"] for s in result["sources"]} == {"moh_portal", "hcm_health", "who_ageing"}
    replayed = replay_collection(store, run["run_id"])
    replay_result = build_dataset(store, replayed["run_id"])
    assert replay_result["status"] == "ready" and replay_result["documents"] == 7
    assert {r["content_version"] for r in gold_records(store, result["dataset_id"])} == {
        r["content_version"] for r in gold_records(store, replay_result["dataset_id"])
    }


def test_sidebar_candidates_cannot_break_chronological_boundary_but_keep_date_gaps(tmp_path):
    from healthlab.signal.html_rules import ElementSelector

    src = source()
    src.html_discovery.stop_before_window = True
    src.html_discovery.chronological_scope = ElementSelector(attributes={"id": "chronological"})
    listing = (
        '<div id="chronological">' + card("old", "2026-10-01") + "</div>"
        '<aside><a href="/article/undated">Promoted story</a></aside><a href="/news/2">Next</a>'
    )
    store = RunStore(tmp_path)
    run, requests = capture(store, listing, src=src)
    assert HOST + "/news/2" not in requests
    assert HOST + "/article/undated" in requests
    result = build_dataset(store, run["run_id"])
    assert result["status"] == "partial"
    assert any(i["code"] == "html_date_missing" for i in result["sources"][0]["issues"])
    assert not any(i["code"] == "listing_budget_exhausted" for i in result["sources"][0]["issues"])


def test_disappearing_chronological_container_cannot_claim_coverage(tmp_path):
    from healthlab.signal.html_rules import ElementSelector

    src = source()
    src.html_discovery.chronological_scope = ElementSelector(attributes={"id": "missing"})
    store = RunStore(tmp_path)
    run, _ = capture(store, card("a"), src=src)
    result = build_dataset(store, run["run_id"])
    assert result["status"] == "partial"
    assert any(
        i["code"] == "listing_chronological_scope_changed" for i in result["sources"][0]["issues"]
    )


@pytest.mark.parametrize("policy", ["publication", "source_displayed"])
def test_captured_hcm_sidebar_respects_explicit_date_policy(tmp_path, policy):
    import gzip
    import hashlib

    root = Path(__file__).resolve().parents[1]
    fixtures = root / "tests/fixtures/signal_html"
    entries = [
        e
        for e in json.loads((fixtures / "provenance.json").read_bytes())
        if e["file"]
        in {"hcm_sidebar_listing_20261026.html.gz", "hcm_updated_only_20261026.html.gz"}
    ]
    responses = {e["source_url"]: e for e in entries}
    src = Registry.model_validate_json((root / "config/signal_sources.json").read_bytes()).sources[
        1
    ]
    # Historical HTML/date fixture, isolated from the separately tested search supplement.
    src.html_discovery.hcm_search_inventory = False
    src.html_rules.window_date_policy = policy
    requests = []

    def respond(request):
        requests.append(str(request.url))
        entry = responses[str(request.url)]
        raw = gzip.decompress((fixtures / entry["file"]).read_bytes())
        assert hashlib.sha256(raw).hexdigest() == entry["raw_sha256"]
        return httpx.Response(200, content=raw, headers={"content-type": "text/html"})

    store = RunStore(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        run = collect(
            store,
            Registry(schema_version="signal-registry-3", sources=[src]),
            mindate=date(2026, 10, 7),
            maxdate=date(2026, 10, 13),
            rss_client=client,
            fetch_policy=POLICY,
            remember_candidates=True,
        )
    result = build_dataset(store, run["run_id"])
    assert len(requests) == 2
    assert result["status"] == ("partial" if policy == "publication" else "ready")
    assert run["source_checks"][0]["listing_pages"][0]["stop_reason"] == "publication_boundary"
    _, _, silver, _ = read_dataset(store, result["dataset_id"])
    pending = next(r for r in silver.records if r.url.endswith("-76378.aspx"))
    assert pending.publication_date is None and pending.modified_date_values == ["2026-10-24"]
    if policy == "source_displayed":
        assert pending.outcome == "outside_window"
        assert pending.selection_date == date(2026, 10, 24)
        assert pending.selection_date_basis == "article_modified"
        assert pending.selection_date_evidence[0]["raw_sha256"] == pending.raw_sha256


@pytest.mark.parametrize("published", [None, "2026-09-01"])
def test_newsletter_update_date_selects_without_relabelling_publication(tmp_path, published):
    src = source()
    src.html_rules.window_date_policy = "source_displayed"
    content = article(published) + '<meta property="article:modified_time" content="2026-10-14">'
    store = RunStore(tmp_path / "store")
    run, _ = capture(
        store, '<a href="/article/a">Report</a>', src=src, pages={HOST + "/article/a": content}
    )
    result = build_dataset(store, run["run_id"])
    assert result["status"] == "ready"
    row = gold_records(store, result["dataset_id"])[0]
    assert row["publication_date"] == published
    assert row["selection_date"] == "2026-10-14"
    assert row["selection_date_basis"] == "article_modified"
    assert row["selection_date_evidence"][0]["raw_sha256"] == row["raw_sha256"]
    exported = export_dataset(store, result["dataset_id"], tmp_path / "export")
    assert exported["verified"]
    assert "Selection date / basis" in (tmp_path / "export/report.html").read_text()
    # Disappearing links retain the selected date and original raw evidence.
    second, requests = capture(store, card("b"), src=src, pages={HOST + "/article/a": content})
    assert HOST + "/article/a" in requests
    assert any(
        r["url"] == HOST + "/article/a"
        for r in gold_records(store, build_dataset(store, second["run_id"])["dataset_id"])
    )


def test_newsletter_conflicting_display_dates_remain_partial(tmp_path):
    src = source()
    src.html_rules.window_date_policy = "source_displayed"
    store = RunStore(tmp_path)
    run, _ = capture(
        store,
        card("a"),
        src=src,
        pages={
            HOST + "/article/a": article()
            + '<meta property="article:modified_time" content="2026-10-15">'
        },
    )
    result = build_dataset(store, run["run_id"])
    assert result["status"] == "partial"
    assert result["documents"] == 0
    assert any(i["code"] == "html_date_conflict" for i in result["sources"][0]["issues"])
