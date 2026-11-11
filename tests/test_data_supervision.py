"""Operational supervision: durable incidents, release gates and source onboarding."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from healthlab.data import incidents, pilot, supervision
from healthlab.data.datasets import build_dataset, gold_records
from healthlab.models import IngestionError
from healthlab.signal.collection import collect, replay_collection
from healthlab.signal.contracts import Registry
from healthlab.store import RunStore
from tests.test_data_pipeline import POLICY, WINDOW, capture
from tests.test_signal_discovery import HOST, article, card, source


def observation(sequence="first"):
    return {
        "dataset_id": sequence,
        "window": {"mindate": "2026-10-12", "maxdate": "2026-10-16"},
        "findings": [supervision._finding("fixture", "body_missing", HOST + "/article/a")],
    }


def test_observation_retry_is_durable_and_does_not_duplicate_or_reopen(tmp_path):
    store = RunStore(tmp_path)
    value = observation()
    ref, state, changes = incidents.record_assessment(store, value)
    identifier = next(iter(state["incidents"]))
    assert changes[0]["action"] == "opened"
    incidents.transition(
        store,
        identifier,
        "resolve",
        actor="Kelvin",
        note="Reviewed repair",
        evidence="corrected dataset reference",
    )
    pointer = (tmp_path / "supervision/current.json").read_bytes()
    same_ref, state, changes = incidents.record_assessment(RunStore(tmp_path), value)
    assert same_ref == ref and not changes
    assert state["incidents"][identifier]["status"] == "resolved"
    assert pointer == (tmp_path / "supervision/current.json").read_bytes()
    _, state, changes = incidents.record_assessment(store, observation("new-capture"))
    assert changes[0]["action"] == "reopened"
    assert state["incidents"][identifier]["occurrences"] == 2


def test_concurrent_duplicate_observations_have_one_committed_event(tmp_path):
    store = RunStore(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: incidents.record_assessment(store, observation()), range(12)))
    state = incidents.snapshot(store)
    assert len(state["events"]) == len(state["observations"]) == 1
    assert next(iter(state["incidents"].values()))["occurrences"] == 1


def test_atomic_pointer_failure_preserves_old_state_and_retry_recovers(tmp_path, monkeypatch):
    store = RunStore(tmp_path)
    incidents.record_assessment(store, observation())
    before = incidents.snapshot(store)
    real_atomic = incidents.atomic_json
    monkeypatch.setattr(incidents, "atomic_json", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        incidents.record_assessment(store, observation("next"))
    assert incidents.snapshot(store) == before
    monkeypatch.setattr(incidents, "atomic_json", real_atomic)
    incidents.record_assessment(store, observation("next"))
    assert next(iter(incidents.snapshot(store)["incidents"].values()))["occurrences"] == 2


def test_corrupt_incident_state_fails_closed_and_is_not_reset(tmp_path):
    store = RunStore(tmp_path)
    incidents.record_assessment(store, observation())
    pointer = json.loads((tmp_path / "supervision/current.json").read_bytes())
    (tmp_path / "objects" / pointer["state_sha256"]).write_bytes(b"corrupt")
    with pytest.raises(IngestionError, match="hash"):
        incidents.record_assessment(store, observation("next"))
    assert json.loads((tmp_path / "supervision/current.json").read_bytes()) == pointer


@pytest.mark.parametrize(
    "kwargs",
    [
        {"actor": " ", "note": "checked", "evidence": "dataset"},
        {"actor": "Kelvin", "note": " ", "evidence": "dataset"},
        {"actor": "Kelvin", "note": "checked"},
    ],
)
def test_resolve_requires_actor_note_and_evidence(tmp_path, kwargs):
    store = RunStore(tmp_path)
    _, state, _ = incidents.record_assessment(store, observation())
    with pytest.raises(ValueError, match="required"):
        incidents.transition(store, next(iter(state["incidents"])), "resolve", **kwargs)


def test_healthy_run_requires_review_of_relevant_unresolved_incidents(tmp_path):
    store = RunStore(tmp_path)
    run, _ = capture(store)
    dataset_id = build_dataset(store, run["run_id"])["dataset_id"]
    incidents.record_assessment(store, observation())
    result = supervision.supervise(store, dataset_id)
    identifier = result["active_incident_ids"][0]
    assert result["gate"] == "needs_review" and not result["current_finding_ids"]
    incidents.transition(store, identifier, "acknowledge", actor="Kelvin", note="Investigating")
    assert supervision.supervise(store, dataset_id)["gate"] == "needs_review"
    incidents.transition(
        store,
        identifier,
        "resolve",
        actor="Kelvin",
        note="Verified raw and repair",
        evidence=dataset_id,
    )
    assert supervision.supervise(store, dataset_id)["gate"] == "ready"
    # Acknowledgement of a resolved incident is not an implicit reopen.
    with pytest.raises(ValueError):
        incidents.transition(store, identifier, "acknowledge", actor="Kelvin", note="Again")


def test_old_window_and_other_source_incidents_remain_visible_without_blocking_unrelated_data(
    tmp_path,
):
    store = RunStore(tmp_path)
    first = observation()
    first["window"] = {"mindate": "2026-09-01", "maxdate": "2026-09-07"}
    incidents.record_assessment(store, first)
    second = observation("other-source")
    second["findings"][0]["source_id"] = "other"
    incidents.record_assessment(store, second)
    run, _ = capture(store)
    result = supervision.supervise(store, build_dataset(store, run["run_id"])["dataset_id"])
    assert result["gate"] == "ready" and not result["active_incident_ids"]
    assert len(incidents.snapshot(store)["incidents"]) == 2


def test_resolving_incidents_cannot_release_a_still_broken_dataset(tmp_path):
    store = RunStore(tmp_path)
    run, _ = capture(store, pages={HOST + "/article/a": 404})
    dataset_id = build_dataset(store, run["run_id"])["dataset_id"]
    result = supervision.supervise(store, dataset_id)
    assert result["gate"] == "blocked"
    for identifier in result["current_finding_ids"]:
        incidents.transition(
            store,
            identifier,
            "resolve",
            actor="Kelvin",
            note="Historical record",
            evidence="repair must be assessed separately",
        )
    result = supervision.supervise(store, dataset_id)
    assert result["gate"] == "blocked" and not result["active_incident_ids"]


def test_immutable_assessment_dedup_does_not_overwrite_dataset_or_publication(tmp_path):
    store = RunStore(tmp_path)
    run, _ = capture(store)
    dataset_id = build_dataset(store, run["run_id"])["dataset_id"]
    before = {str(p): p.read_bytes() for p in (tmp_path / "runs").glob("*/manifest.json")}
    first = supervision.supervise(store, dataset_id)
    pointer = (tmp_path / "supervision/current.json").read_bytes()
    second = supervision.supervise(store, dataset_id)
    assert first == second and first["gate"] == "ready"
    assert pointer == (tmp_path / "supervision/current.json").read_bytes()
    assert all(Path(p).read_bytes() == content for p, content in before.items())


def test_silent_omission_of_a_required_branch_is_blocked_even_if_gold_says_ready(tmp_path):
    store = RunStore(tmp_path)
    run, _ = capture(store)
    # Negative control: a future connector bug falsely declares success after
    # losing its listing ledger. Data lineage alone does not prove discovery.
    run["source_checks"][0]["listing_pages"] = []
    store.save(run)
    dataset = build_dataset(store, run["run_id"])
    assert dataset["status"] == "ready"
    result = supervision.supervise(store, dataset["dataset_id"])
    assert result["gate"] == "blocked"
    value = json.loads(store.read(result["assessment_sha256"]))
    assert value["findings"][0]["code"] == "required_branch_unverified"


def test_broken_object_raises_durable_incident_without_implying_ready(tmp_path):
    store = RunStore(tmp_path)
    run, _ = capture(store)
    dataset_id = build_dataset(store, run["run_id"])["dataset_id"]
    (tmp_path / "objects" / dataset_id).write_bytes(b"broken")
    result = supervision.supervise(store, dataset_id)
    assert result["gate"] == "blocked" and result["technical_status"] == "failed"
    row = next(iter(incidents.snapshot(store)["incidents"].values()))
    assert row["finding"]["code"] == "dataset_verification_failed"


def test_current_official_registry_declares_all_15_operational_requirements():
    registry = Registry.model_validate_json(Path("config/signal_sources.json").read_bytes())
    contracts = {s.source_id: supervision.source_contract(s) for s in registry.sources if s.enabled}
    assert [len(contracts[s]["required_branches"]) for s in contracts] == [6, 2, 7]
    assert contracts["hcm_health"]["window_date_policy"] == "source_displayed"
    assert contracts["who_ageing"]["limitations"]


def test_fourth_html_source_needs_configuration_only_and_replays_consistently(tmp_path):
    sources, pages = [], {}
    for index in range(1, 5):
        host = f"source{index}.example.org"
        config = source().model_dump(mode="json")
        config.update(source_id=f"fixture{index}", url=f"https://{host}/news")
        config["html_rules"]["host"] = host
        if index == 4:
            config["html_rules"]["body"] = {"attributes": {"id": "story-content"}}
        sources.append(config)
        pages[f"https://{host}/news"] = card("a")
        pages[f"https://{host}/article/a"] = (
            article().replace("<main>", '<div id="story-content">').replace("</main>", "</div>")
            if index == 4
            else article()
        )
    requests = []

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(
            200, text=pages[str(request.url)], headers={"content-type": "text/html"}
        )

    registry = Registry.model_validate({"schema_version": "signal-registry-3", "sources": sources})
    store = RunStore(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        run = collect(store, registry, **WINDOW, fetch_policy=POLICY, rss_client=client)
    first = build_dataset(store, run["run_id"])
    assert first["documents"] == 4 and first["status"] == "ready" and len(requests) == 8
    assert supervision.supervise(store, first["dataset_id"])["gate"] == "ready"
    assert build_dataset(store, run["run_id"])["dataset_id"] == first["dataset_id"]
    replay = replay_collection(store, run["run_id"])
    second = build_dataset(store, replay["run_id"])

    def logical(identifier):
        return sorted(
            (r["document_id"], r["body"], r["publication_date"])
            for r in gold_records(store, identifier)
        )

    assert logical(first["dataset_id"]) == logical(second["dataset_id"])
    assert len(requests) == 8


def test_pilot_interruption_records_incident_and_resume_keeps_review_gate(tmp_path):
    from tests.test_data_pilot import execute, transport

    store = RunStore(tmp_path)
    client, requests = transport()

    def interrupt(_):
        raise KeyboardInterrupt()

    with client:
        with pytest.raises(KeyboardInterrupt):
            execute(store, client, on_started=interrupt)
        result = execute(store, client)
    assert result["status"] == "ready" and result["supervision"]["gate"] == "needs_review"
    assert len(requests) == 2
    row = next(iter(incidents.snapshot(store)["incidents"].values()))
    assert row["finding"]["code"] == "pilot_interrupted"


def test_pilot_preserves_original_failure_when_incident_persistence_also_fails(
    tmp_path, monkeypatch, capsys
):
    from tests.test_data_pilot import execute, transport

    monkeypatch.setattr(
        pilot, "record_pilot_failure", lambda *a: (_ for _ in ()).throw(OSError("disk"))
    )

    def interrupt(_):
        raise KeyboardInterrupt()

    client, _ = transport()
    with client, pytest.raises(KeyboardInterrupt):
        execute(RunStore(tmp_path), client, on_started=interrupt)
    assert "incident_persistence_failed" in capsys.readouterr().err


def test_cli_incident_review_and_html_snapshot_do_not_overwrite_evidence(tmp_path, capsys):
    store = RunStore(tmp_path)
    run, _ = capture(store)
    dataset_id = build_dataset(store, run["run_id"])["dataset_id"]
    output = tmp_path / "review.html"
    arguments = ["--store", str(tmp_path), "assess", dataset_id, "--html", str(output)]
    assert supervision.main(arguments) == 0
    capsys.readouterr()
    assert "Giám sát" in output.read_text()
    assert supervision.main(arguments) == 1
    assert "File exists" in capsys.readouterr().err


def test_consumer_guard_blocks_unreviewed_data_and_releases_after_evidenced_review(tmp_path):
    store = RunStore(tmp_path)
    run, _ = capture(store)
    dataset_id = build_dataset(store, run["run_id"])["dataset_id"]
    incidents.record_assessment(store, observation())
    with pytest.raises(IngestionError, match="incident review"):
        supervision.supervised_records(store, dataset_id)
    identifier = next(iter(incidents.snapshot(store)["incidents"]))
    incidents.transition(
        store,
        identifier,
        "resolve",
        actor="Kelvin",
        note="Verified correction",
        evidence=dataset_id,
    )
    assert len(supervision.supervised_records(store, dataset_id)) == 1


def test_pilot_will_not_start_next_day_or_changed_runtime_while_previous_attempt_pending(
    tmp_path, monkeypatch
):
    from datetime import date

    from tests.test_data_pilot import POLICY, execute, registry, transport

    store = RunStore(tmp_path)
    client, requests = transport()
    with client:
        with pytest.raises(KeyboardInterrupt):
            execute(store, client, on_started=lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
        before = list((tmp_path / "pilot").glob("*/*/*/journal.json"))
        with pytest.raises(IngestionError) as error:
            pilot.run_pilot(
                store, registry(), as_of=date(2026, 10, 18), rss_client=client, fetch_policy=POLICY
            )
        assert error.value.code == "pilot_pending_attempt"
        original_fingerprint = pilot.runtime_fingerprint()
        monkeypatch.setattr(pilot, "runtime_fingerprint", lambda: "f" * 64)
        with pytest.raises(IngestionError) as error:
            execute(store, client)
        assert error.value.code == "pilot_pending_attempt"
        assert not requests and list((tmp_path / "pilot").glob("*/*/*/journal.json")) == before
        monkeypatch.setattr(pilot, "runtime_fingerprint", lambda: original_fingerprint)
        assert execute(store, client)["status"] == "ready"
        assert len(requests) == 2


def test_pilot_cli_logs_corrupt_completed_report_without_rewriting_it(tmp_path, capsys):
    from tests.test_data_pilot import AS_OF, registry, transport

    reg = tmp_path / "registry.json"
    reg.write_text(registry().model_dump_json())
    client, requests = transport()
    store = RunStore(tmp_path)
    with client:
        result = pilot.run_pilot(store, registry(), as_of=AS_OF, rss_client=client)
    report = Path(result["journal"]).parent / "report.html"
    report.write_text("corrupt")
    assert (
        pilot.main(["--store", str(tmp_path), "--registry", str(reg), "--as-of", str(AS_OF)]) == 1
    )
    output = json.loads(capsys.readouterr().out)
    assert output["incident_assessment_sha256"] and len(requests) == 2
    assert report.read_text() == "corrupt"
