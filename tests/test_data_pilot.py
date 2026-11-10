"""Daily operation, crash recovery, bounded repairs and honest overlap comparisons."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from healthlab.data import pilot
from healthlab.models import IngestionError
from healthlab.signal.contracts import Registry
from healthlab.signal.source_http import FetchPolicy
from healthlab.store import RunStore
from tests.test_signal_discovery import HOST, article, card, source

AS_OF = date(2026, 10, 17)
POLICY = FetchPolicy(max_attempts=1, request_interval=0)


def registry():
    return Registry(schema_version="signal-registry-3", sources=[source()])


def execute(store, client, **kwargs):
    return pilot.run_pilot(
        store, registry(), rss_client=client, as_of=AS_OF, fetch_policy=POLICY, **kwargs
    )


def transport(pages=None):
    requests = []
    pages = pages if pages is not None else {HOST + "/news": card("a")}

    def respond(request):
        url = str(request.url)
        requests.append(url)
        value = pages.get(url, article())
        if isinstance(value, int):
            return httpx.Response(value)
        return httpx.Response(200, text=value, headers={"content-type": "text/html"})

    return httpx.Client(transport=httpx.MockTransport(respond)), requests


def test_window_uses_vietnam_midnight_and_inclusive_calendar_days():
    assert pilot.daily_window(now=datetime(2026, 10, 27, 17, tzinfo=UTC)) == (
        date(2026, 10, 28),
        date(2026, 10, 14),
        date(2026, 10, 27),
    )
    assert pilot.daily_window(date(2024, 3, 1))[1:] == (date(2024, 2, 16), date(2024, 2, 29))
    assert pilot.daily_window(date(2026, 1, 1))[1:] == (date(2025, 12, 18), date(2025, 12, 31))
    with pytest.raises(ValueError, match="future"):
        pilot.daily_window(date(2026, 10, 29), now=datetime(2026, 10, 28, 0, tzinfo=UTC))
    with pytest.raises(ValueError):
        pilot.daily_window(days=0)


def test_same_day_reuses_release_without_http_and_explicit_fresh_becomes_current(tmp_path):
    store = RunStore(tmp_path)
    pages = {HOST + "/news": card("a")}
    client, requests = transport(pages)
    with client:
        first = execute(store, client)
        manifests = list((tmp_path / "runs").glob("*/manifest.json"))
        second = execute(store, client)
        assert second["dataset_id"] == first["dataset_id"] and second["reused"]
        assert len(requests) == 2 and len(list((tmp_path / "runs").glob("*/manifest.json"))) == len(
            manifests
        )
        pages[HOST + "/article/a"] = article().replace("community care", "updated community care")
        fresh = execute(store, client, fresh=True)
        assert fresh["attempt_id"] != first["attempt_id"] and not fresh["reused"]
        assert len(fresh["delta"]["changed_in_overlap"]) == 1
        assert execute(store, client)["attempt_id"] == fresh["attempt_id"]
        assert len(requests) == 4
    assert first["verified"] and first["status"] == "ready"
    assert first["coverage_acceptance"] == "not_assessed"
    assert first["delta"]["baseline_only"]


def test_interrupted_http_resumes_saved_responses_without_repeating_successes(tmp_path):
    requests, interrupted = [], []

    def respond(request):
        url = str(request.url)
        requests.append(url)
        if url.endswith("/b") and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt()
        body = card("a") + card("b") if url.endswith("/news") else article()
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    store = RunStore(tmp_path)
    policy = FetchPolicy(max_attempts=3, request_interval=0)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(KeyboardInterrupt):
            pilot.run_pilot(store, registry(), as_of=AS_OF, fetch_policy=policy, rss_client=client)
        with pytest.raises(ValueError, match="pending"):
            pilot.run_pilot(
                store, registry(), as_of=AS_OF, fetch_policy=policy, rss_client=client, fresh=True
            )
        result = pilot.run_pilot(
            store, registry(), as_of=AS_OF, fetch_policy=policy, rss_client=client
        )
    assert result["status"] == "ready" and result["documents"] == 2
    assert requests.count(HOST + "/news") == requests.count(HOST + "/article/a") == 1
    assert requests.count(HOST + "/article/b") == 2
    assert len(list((tmp_path / "runs").glob("*/manifest.json"))) == 2


def test_operation_tag_recovers_collection_when_callback_journal_id_was_lost(tmp_path):
    store = RunStore(tmp_path)
    client, requests = transport()
    with client:
        with pytest.raises(KeyboardInterrupt):
            execute(store, client, on_started=lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
        path = next((tmp_path / "pilot").glob("*/*/*/journal.json"))
        journal = json.loads(path.read_bytes())
        original = journal["collection_id"]
        # Fault injection: the durable collection exists, while its journal callback
        # did not commit. A process death at this point leaves precisely this state.
        journal["collection_id"] = None
        path.write_text(json.dumps(journal))
        result = execute(store, client)
    assert result["collection_id"] == original and len(requests) == 2
    assert len(list((tmp_path / "runs").glob("*/manifest.json"))) == 2


@pytest.mark.parametrize("crash_point", ["after_export", "after_report"])
def test_restart_after_publication_does_not_duplicate_release_or_fetch(
    tmp_path, monkeypatch, crash_point
):
    store = RunStore(tmp_path)
    client, requests = transport()
    crashed = []
    real_export, real_save = pilot.export_dataset, pilot.atomic_json

    def export_then_crash(*args):
        result = real_export(*args)
        if not crashed:
            crashed.append(True)
            raise KeyboardInterrupt()
        return result

    def save_then_crash(path, value):
        if path.name == "journal.json" and value.get("status") == "completed" and not crashed:
            crashed.append(True)
            raise KeyboardInterrupt()
        return real_save(path, value)

    monkeypatch.setattr(
        pilot,
        "export_dataset" if crash_point == "after_export" else "atomic_json",
        export_then_crash if crash_point == "after_export" else save_then_crash,
    )
    with client:
        with pytest.raises(KeyboardInterrupt):
            execute(store, client)
        count = len(list((tmp_path / "runs").glob("*/manifest.json")))
        result = execute(store, client)
    assert result["verified"] and result["status"] == "ready"
    assert (
        len(requests) == 2 and len(list((tmp_path / "runs").glob("*/manifest.json"))) == count == 2
    )


@pytest.mark.parametrize("recover", [True, False])
def test_repair_only_missing_response_and_never_repeats_its_budget(tmp_path, recover):
    requests = []

    def respond(request):
        url = str(request.url)
        requests.append(url)
        if url.endswith("/a") and (not recover or requests.count(url) == 1):
            return httpx.Response(503)
        body = card("a") + card("b") if url.endswith("/news") else article()
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        store = RunStore(tmp_path)
        result = execute(store, client)
        assert result["repair_used"] and result["status"] == ("ready" if recover else "partial")
        assert result["metrics"]["http_attempts"] == len(requests) == 4
        assert requests.count(HOST + "/news") == requests.count(HOST + "/article/b") == 1
        assert requests.count(HOST + "/article/a") == 2
        again = execute(store, client)
        assert again["reused"] and len(requests) == 4


def test_nonretryable_http_failure_stays_visible_without_automatic_repair(tmp_path):
    client, requests = transport({HOST + "/news": card("a"), HOST + "/article/a": 404})
    with client:
        result = execute(RunStore(tmp_path), client)
    assert result["status"] == "partial" and not result["repair_used"]
    assert len(requests) == 2 and result["sources"][0]["outcomes"]["failed"] == 1


def test_explicit_repair_parent_reuses_capture_in_new_journal_without_hidden_extra_repair(tmp_path):
    pages = {HOST + "/news": card("a"), HOST + "/article/a": 404}
    client, requests = transport(pages)
    store = RunStore(tmp_path)
    with client:
        first = execute(store, client)
        pages[HOST + "/article/a"] = article()
        result = execute(store, client, repair_from=first["collection_id"])
        assert result["status"] == "ready" and result["capture_kind"] == "repair"
        assert result["metrics"]["http_attempts"] == 1 and len(requests) == 3
        assert not result["repair_used"]
        assert execute(store, client, repair_from=first["collection_id"])["reused"]
    assert result["dataset_id"] != first["dataset_id"]


def test_runner_overlap_lock_fails_before_any_http(tmp_path):
    store = RunStore(tmp_path)
    client, requests = transport()
    with client, pilot.runner_lock(tmp_path):
        with pytest.raises(IngestionError) as exc:
            execute(store, client)
    assert exc.value.code == "pilot_busy" and not requests


def test_overlap_delta_does_not_call_window_exit_a_missing_article(tmp_path):
    pages = {HOST + "/news": card("a") + card("leaving", "2026-10-03")}
    store = RunStore(tmp_path)
    client, _ = transport(pages)
    with client:
        first = execute(store, client)
        pages[HOST + "/news"] = card("a") + card("new") + card("entering", "2026-10-17")
        pages[HOST + "/article/a"] = article().replace("community care", "updated care")
        second = pilot.run_pilot(
            store, registry(), as_of=date(2026, 10, 18), rss_client=client, fetch_policy=POLICY
        )
    delta = second["delta"]
    assert delta["previous_dataset_id"] == first["dataset_id"]
    assert len(delta["entered_window"]) == len(delta["exited_window"]) == 1
    assert len(delta["changed_in_overlap"]) == len(delta["newly_observed_in_overlap"]) == 1
    assert not delta["not_in_current_gold_in_overlap"]


def test_completed_run_rechecks_bundle_and_report_integrity_without_http(tmp_path):
    client, requests = transport()
    store = RunStore(tmp_path)
    with client:
        result = execute(store, client)
        report = Path(result["journal"]).parent / "report.html"
        report.write_text("corrupted")
        with pytest.raises(ValueError, match="report integrity"):
            execute(store, client)
    assert len(requests) == 2


def test_cli_partial_is_nonzero_and_reuse_does_not_start_network(tmp_path, capsys):
    reg = tmp_path / "registry.json"
    reg.write_text(registry().model_dump_json())
    client, _ = transport({HOST + "/news": card("a"), HOST + "/article/a": 404})
    with client:
        result = pilot.run_pilot(RunStore(tmp_path), registry(), as_of=AS_OF, rss_client=client)
    assert result["status"] == "partial"
    assert (
        pilot.main(["--store", str(tmp_path), "--registry", str(reg), "--as-of", str(AS_OF)]) == 1
    )
    assert json.loads(capsys.readouterr().out)["reused"]
