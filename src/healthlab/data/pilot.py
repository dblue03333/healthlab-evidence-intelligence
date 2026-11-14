"""Journaled local daily collection. Run again to resume; --fresh starts a new capture."""

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from healthlab.common import configure_logging
from healthlab.data.bundle import export_dataset, verify_export
from healthlab.data.datasets import build_dataset, read_dataset, summary
from healthlab.data.power import awake_for_cli
from healthlab.data.supervision import record_operation_failure, record_pilot_failure, supervise
from healthlab.models import IngestionError
from healthlab.signal.collection import (
    COLLECTION_VERSION,
    collect,
    repair_collection,
    resume_collection,
)
from healthlab.signal.contracts import Registry
from healthlab.signal.diagnostics import Span, exception_diagnostic
from healthlab.signal.history import source_scope
from healthlab.signal.html_adapter import PARSER_VERSION
from healthlab.signal.storage import atomic_json, collection_storage
from healthlab.store import RunStore, json_bytes, sync_directory

logger = logging.getLogger(__name__)

VERSION = "data-pilot-1"
TIMEZONE = "Asia/Ho_Chi_Minh"


def sha(value):
    return hashlib.sha256(json_bytes(value)).hexdigest()


def now_iso():
    return datetime.now(UTC).isoformat()


def daily_window(as_of=None, *, days=14, now=None):
    if not 1 <= days <= 366:
        raise ValueError("Window must contain 1 to 366 days")
    if now is not None and now.tzinfo is None:
        raise ValueError("An aware clock is required")
    today = (now or datetime.now(UTC)).astimezone(ZoneInfo(TIMEZONE)).date()
    as_of = as_of or today
    if as_of > today:
        raise ValueError("A future pilot date is not allowed")
    maximum = as_of - timedelta(days=1)
    return as_of, maximum - timedelta(days=days - 1), maximum


def runtime_fingerprint():
    root = Path(__file__).resolve().parents[1]
    paths = sorted((root / "data").glob("*.py")) + sorted((root / "signal").glob("*.py"))
    paths += [root / "store.py", root / "models.py"]
    return sha(
        {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    )


@contextmanager
def runner_lock(root):
    # Distinct from signal.lock. Collector acquires its own lock inside this one.
    # No collector callback ever attempts to acquire runner_lock in reverse order.
    with (root / "pilot.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise IngestionError("pilot_busy", "Another pilot invocation is active") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _load(path):
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("Invalid pilot JSON object")
    return value


def _journal(directory, config, *, fresh):
    directory.mkdir(parents=True, exist_ok=True)
    pointer = directory / "current.json"
    previous = None
    if pointer.exists():
        attempt = _load(pointer)["attempt_id"]
        if not re.fullmatch(r"[a-f0-9]{32}", attempt):
            raise ValueError("Invalid pilot attempt identity")
        previous = directory / attempt / "journal.json"
        saved = _load(previous)
        if saved["config"] != config or saved["attempt_id"] != attempt:
            raise ValueError("Pilot journal configuration differs")
        if not fresh:
            return previous, saved
        if saved["status"] != "completed":
            raise ValueError("Resume the pending attempt before starting a fresh capture")
    attempt = uuid.uuid4().hex
    folder = directory / attempt
    folder.mkdir()
    journal = {
        "schema_version": VERSION,
        "attempt_id": attempt,
        "config": config,
        "created_at": now_iso(),
        "status": "running",
        "stage": "collect",
        "collection_id": None,
        "repair_collection_id": None,
        "repair_planned": None,
        "dataset_id": None,
        "previous_attempt": str(previous) if previous else None,
        "errors": [],
    }
    path = folder / "journal.json"
    atomic_json(path, journal)
    # No HTTP occurs before this durable pointer. An orphan before it is harmless.
    atomic_json(pointer, {"attempt_id": attempt})
    return path, journal


def _ensure_collection(store, journal, save, *, repair, client, on_started):
    field = "repair_collection_id" if repair else "collection_id"
    operation = sha({"attempt_id": journal["attempt_id"], "phase": field})
    storage = collection_storage(store)
    run_id = journal[field]
    if run_id is None:
        # Recover a process death after collection.save but before its callback
        # saved the ID in our journal. The operation tag is durable before HTTP.
        with storage.state.lock():
            matches = [
                storage.load(i)
                for i in storage.state.list_runs("signal_collection")
                if storage.load(i).get("operation_id") == operation
            ]
        if len(matches) > 1:
            raise ValueError("Ambiguous pilot collection operation")
        if matches:
            run_id = matches[0]["run_id"]
            journal[field] = run_id
            save()

    def started(identifier):
        journal[field] = identifier
        save()
        if on_started:
            on_started(identifier)

    config = journal["config"]
    if run_id is None:
        if repair:
            return repair_collection(
                store,
                journal["collection_id"],
                on_started=started,
                rss_client=client,
                operation_id=operation,
            )
        if config.get("repair_from"):
            return repair_collection(
                store,
                config["repair_from"],
                registry=Registry.model_validate(config["registry"]),
                on_started=started,
                rss_client=client,
                operation_id=operation,
            )
        return collect(
            store,
            Registry.model_validate(config["registry"]),
            mindate=date.fromisoformat(config["window"]["mindate"]),
            maxdate=date.fromisoformat(config["window"]["maxdate"]),
            fetch_policy=_fetch_policy(config["fetch_policy"]),
            rss_client=client,
            on_started=started,
            remember_candidates=True,
            operation_id=operation,
        )
    run = storage.load(run_id)
    if (
        run.get("operation_id") != operation
        or run["registry"] != config["registry"]
        or run["question"] != config["window"]
        or run["fetch_policy"] != config["fetch_policy"]
    ):
        raise ValueError("Pilot collection lineage differs from its journal")
    if (repair or config.get("repair_from")) and not run.get("repair_of_manifest_sha256"):
        raise ValueError("Expected a repair collection")
    return (
        resume_collection(store, run_id, rss_client=client) if run["status"] == "running" else run
    )


def _fetch_policy(value):
    from healthlab.signal.source_http import FetchPolicy

    return FetchPolicy(**value)


def _retryable_missing(store, run):
    if run["status"] == "completed":
        return False
    for check in run["source_checks"]:
        if not check.get("checkpoint_sha256"):
            continue
        checkpoint = json.loads(store.read(check["checkpoint_sha256"]))
        latest = {}
        for attempt in checkpoint.get("http_attempts", []):
            latest[attempt.get("request_key", attempt["url"])] = attempt
        if any(
            key not in checkpoint.get("responses", {})
            and event["status"] == "failed"
            and event.get("retryable")
            for key, event in latest.items()
        ):
            return True
    return False


def _dataset(store, collection_id):
    storage = collection_storage(store)
    with storage.state.lock():
        published = {
            r["dataset_sha256"]
            for i in storage.state.list_runs("signal_dataset")
            if (r := storage.load(i)).get("collection_id") == collection_id
            and r["status"] == "completed"
        }
    if len(published) > 1:
        raise ValueError("A frozen collection has conflicting dataset publications")
    if not published:
        return build_dataset(store, collection_id)
    identifier = published.pop()
    _, bronze, silver, gold = read_dataset(store, identifier)
    if (
        bronze.collection_manifest_sha256
        != hashlib.sha256(json_bytes(storage.load(collection_id))).hexdigest()
    ):
        raise ValueError("Published collection was modified")
    return summary(identifier, bronze, silver, gold)


def _previous(root, journal):
    candidates = []
    for path in (root / "pilot").glob("*/*/*/journal.json"):
        row = _load(path)
        if (
            row["status"] == "completed"
            and row["attempt_id"] != journal["attempt_id"]
            and row["created_at"] < journal["created_at"]
            and row["config"]["as_of"] <= journal["config"]["as_of"]
            and row["config"]["scope_sha256"] == journal["config"]["scope_sha256"]
        ):
            candidates.append(row)
    return max(candidates, key=lambda r: (r["created_at"], r["attempt_id"]), default=None)


def dataset_delta(store, current_id, previous_id):
    _, _, current, current_gold = read_dataset(store, current_id)
    if previous_id is None:
        return {"baseline_only": True, "previous_dataset_id": None}
    _, _, previous, previous_gold = read_dataset(store, previous_id)
    lower = max(current_gold.window.mindate, previous_gold.window.mindate)
    upper = min(current_gold.window.maxdate, previous_gold.window.maxdate)

    def selected(silver):
        return {r.document_id: r for r in silver.records if r.outcome == "included"}

    def day(row):
        return getattr(row, "selection_date", row.publication_date)

    before, after = selected(previous), selected(current)
    old = {k: r for k, r in before.items() if lower <= day(r) <= upper}
    new = {k: r for k, r in after.items() if lower <= day(r) <= upper}
    observed = {r.document_id: r for r in current.records}
    return {
        "baseline_only": False,
        "previous_dataset_id": previous_id,
        "overlap": {"mindate": str(lower), "maxdate": str(upper)} if lower <= upper else None,
        "previous_status": previous_gold.status,
        "current_status": current_gold.status,
        "newly_observed_in_overlap": sorted(new.keys() - old.keys()),
        "changed_in_overlap": sorted(
            k for k in new.keys() & old.keys() if new[k].content_version != old[k].content_version
        ),
        "not_in_current_gold_in_overlap": [
            {
                "document_id": k,
                "current_outcome": observed[k].outcome if k in observed else "not_observed",
                "current_date": str(day(observed[k])) if k in observed else None,
            }
            for k in sorted(old.keys() - new.keys())
        ],
        "entered_window": sorted(k for k, r in after.items() if not lower <= day(r) <= upper),
        "exited_window": sorted(k for k, r in before.items() if not lower <= day(r) <= upper),
        "interpretation": "Changes in captured observations; absent Gold is not proof of publisher deletion or completeness.",
    }


def _metrics(store, journal):
    attempts = []
    for field in ("collection_id", "repair_collection_id"):
        if journal.get(field):
            run = store.load(journal[field])
            for check in run["source_checks"]:
                if check.get("checkpoint_sha256"):
                    checkpoint = json.loads(store.read(check["checkpoint_sha256"]))
                    attempts.extend(checkpoint.get("http_attempts", []))
    return {
        "http_attempts": len(attempts),
        "http_failed_attempts": sum(a["status"] == "failed" for a in attempts),
        "http_interrupted_attempts": sum(a["status"] == "interrupted" for a in attempts),
        "stage_timings": journal.get("stage_timings", []),
        "response_bytes": sum(a.get("response_bytes", 0) for a in attempts),
        "elapsed_wall_seconds_including_downtime": round(
            (datetime.now(UTC) - datetime.fromisoformat(journal["created_at"])).total_seconds(), 3
        ),
    }


def _html(result):
    return (
        '<!doctype html><html lang="vi"><meta charset="utf-8"><title>HealthLab pilot</title>'
        '<body style="font:16px system-ui;max-width:1100px;margin:32px auto;padding:20px">'
        f"<h1>HealthLab pilot · {escape(result['as_of'])}</h1>"
        f"<p>Processing: <strong>{escape(result['status'])}</strong>. "
        "Coverage acceptance remains unassessed; this run is not editorial approval.</p>"
        '<p><a href="bundle/report.html">Articles, source coverage and unresolved cases</a></p>'
        f'<pre style="white-space:pre-wrap">{escape(json.dumps(result, ensure_ascii=False, indent=2))}</pre>'
        "</body></html>"
    )


def _atomic_report(path, content):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def measured_stage(journal, save, name):
    journal["stage"] = name
    event = {"stage": name, "status": "started"}
    journal.setdefault("stage_timings", []).append(event)
    save()
    span = Span()
    try:
        yield
    except BaseException:
        event.update(status="interrupted", **span.finish())
        save()
        raise
    else:
        event.update(status="completed", **span.finish())
        save()


def run_pilot(
    store,
    registry,
    *,
    as_of=None,
    days=14,
    fresh=False,
    repair_once=True,
    repair_from=None,
    fetch_policy=None,
    rss_client=None,
    on_started=None,
    now=None,
):
    if not isinstance(store, RunStore):
        raise ValueError("Pilot runner currently requires a local RunStore")
    enabled = [s for s in registry.sources if s.enabled]
    if not enabled or any(s.kind != "html" for s in enabled):
        raise ValueError("Pilot requires enabled HTML/API text sources")
    as_of, minimum, maximum = daily_window(as_of, days=days, now=now)
    if repair_from:
        parent = store.load(repair_from)
        if (
            parent.get("mode") != "signal_collection"
            or parent["status"] == "running"
            or parent["question"] != {"mindate": str(minimum), "maxdate": str(maximum)}
        ):
            raise ValueError("Repair parent must be finished with the same requested window")
    policy = fetch_policy or _fetch_policy({})
    config = {
        "version": VERSION,
        "as_of": str(as_of),
        "timezone": TIMEZONE,
        "window": {"mindate": str(minimum), "maxdate": str(maximum)},
        "registry": registry.model_dump(mode="json"),
        "registry_sha256": sha(registry.model_dump(mode="json")),
        "scope_sha256": sha([source_scope(s) for s in enabled]),
        "collector_version": COLLECTION_VERSION,
        "parser_version": PARSER_VERSION,
        "runtime_sha256": runtime_fingerprint(),
        "fetch_policy": policy.as_dict(),
        "repair_once": repair_once,
        "repair_from": repair_from,
    }
    root = store.root.resolve()
    with runner_lock(root):
        pending = [
            (p, _load(p))
            for p in (root / "pilot").glob("*/*/*/journal.json")
            if _load(p)["status"] != "completed"
        ]
        if pending and not any(row["config"] == config for _, row in pending):
            exc = IngestionError(
                "pilot_pending_attempt",
                "Resume the pending date/configuration before starting another logical run",
            )
            exc._healthlab_incident = record_operation_failure(
                store,
                "pending_attempt",
                exc,
                context={"pending_journals": sorted(str(p) for p, _ in pending)},
            )
            raise exc
        path, journal = _journal(root / "pilot" / str(as_of) / sha(config), config, fresh=fresh)

        def save():
            atomic_json(path, journal)

        if journal["status"] == "completed":
            raw = (path.parent / "result.json").read_bytes()
            if hashlib.sha256(raw).hexdigest() != journal["result_sha256"]:
                raise ValueError("Pilot result integrity differs")
            if (
                hashlib.sha256((path.parent / "report.html").read_bytes()).hexdigest()
                != journal["report_sha256"]
            ):
                raise ValueError("Pilot report integrity differs")
            verified = verify_export(path.parent / "bundle")
            read_dataset(store, journal["dataset_id"])
            if verified["dataset_id"] != journal["dataset_id"]:
                raise ValueError("Pilot bundle identity differs")
            result = dict(json.loads(raw), reused=True)
            # The archived result stays frozen. Human incident reviews are live
            # operational state and may legitimately differ since publication.
            result["supervision"] = supervise(store, journal["dataset_id"])
            return result
        journal["status"] = "running"
        save()
        try:
            with measured_stage(journal, save, "collect"):
                run = _ensure_collection(
                    store, journal, save, repair=False, client=rss_client, on_started=on_started
                )
            if journal["repair_planned"] is None:
                journal["repair_planned"] = bool(
                    repair_once and not repair_from and _retryable_missing(store, run)
                )
                save()
            if journal["repair_planned"]:
                journal["stage"] = "repair"
                save()
                with measured_stage(journal, save, "repair"):
                    run = _ensure_collection(
                        store, journal, save, repair=True, client=rss_client, on_started=on_started
                    )
            journal["stage"] = "dataset"
            save()
            with measured_stage(journal, save, "dataset"):
                result = _dataset(store, run["run_id"])
            journal["dataset_id"] = result["dataset_id"]
            journal["stage"] = "export"
            save()
            bundle = path.parent / "bundle"
            with measured_stage(journal, save, "export"):
                verification = (
                    verify_export(bundle)
                    if bundle.exists()
                    else export_dataset(store, result["dataset_id"], bundle)
                )
            if verification["dataset_id"] != result["dataset_id"]:
                raise ValueError("Pilot export differs from its release")
            prior = _previous(root, journal)
            result.update(
                pilot_version=VERSION,
                as_of=str(as_of),
                attempt_id=journal["attempt_id"],
                journal=str(path),
                bundle=str(bundle),
                objects=verification["objects"],
                verified=True,
                repair_used=journal["repair_planned"],
                metrics=_metrics(store, journal),
                delta=dataset_delta(
                    store, result["dataset_id"], prior["dataset_id"] if prior else None
                ),
                coverage_acceptance="not_assessed",
                reused=False,
            )
            journal["stage"] = "supervision"
            save()
            with measured_stage(journal, save, "supervision"):
                result["supervision"] = supervise(store, result["dataset_id"])
            # Atomic result and report, then the completed journal is the commit.
            atomic_json(path.parent / "result.json", result)
            report_bytes = _html(result).encode()
            _atomic_report(path.parent / "report.html", report_bytes)
            journal.update(
                status="completed",
                stage="finished",
                finished_at=now_iso(),
                result_sha256=hashlib.sha256(
                    (path.parent / "result.json").read_bytes()
                ).hexdigest(),
                report_sha256=hashlib.sha256(report_bytes).hexdigest(),
            )
            save()
            return result
        except BaseException as exc:
            journal["status"] = "interrupted"
            journal["errors"].append(
                {
                    "at": now_iso(),
                    "diagnostic": exception_diagnostic(exc),
                    "stage": journal["stage"],
                    "type": type(exc).__name__,
                    "code": exc.code if isinstance(exc, IngestionError) else "pilot_interrupted",
                }
            )
            save()
            try:
                exc._healthlab_incident = record_pilot_failure(store, journal, exc)
            except Exception as incident_exc:
                # Disk/ledger failure must never silently imply that an incident
                # was persisted, or replace the original collection exception.
                logger.error(
                    "incident_persistence_failed: could not record the incident (%s)",
                    type(incident_exc).__name__,
                )
            raise


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=Path("outputs/data_20261026"))
    parser.add_argument("--registry", type=Path, default=Path("config/signal_sources.json"))
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument(
        "--fresh", action="store_true", help="Explicit new capture after a finished run"
    )
    parser.add_argument("--no-repair", action="store_true")
    parser.add_argument(
        "--repair-from",
        help="Explicitly reparse a finished same-window capture and fetch only missing responses",
    )
    args = parser.parse_args(argv)
    try:
        result = run_pilot(
            RunStore(args.store),
            Registry.model_validate_json(args.registry.read_bytes()),
            as_of=args.as_of,
            days=args.days,
            fresh=args.fresh,
            repair_once=not args.no_repair,
            repair_from=args.repair_from,
            on_started=lambda run_id: logger.info("Collection %s is running", run_id),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "ready" and result["supervision"]["gate"] == "ready" else 1
    except (IngestionError, ValueError, OSError, KeyError, TypeError) as exc:
        incident_ref = getattr(exc, "_healthlab_incident", None)
        busy = isinstance(exc, IngestionError) and exc.code == "pilot_busy"
        if not busy and incident_ref is None:
            try:
                incident_ref = record_operation_failure(
                    RunStore(args.store),
                    "pilot",
                    exc,
                    context={"as_of": str(args.as_of), "days": args.days},
                )
            except Exception as incident_exc:
                logger.error(
                    "incident_persistence_failed: could not record the incident (%s)",
                    type(incident_exc).__name__,
                )
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": exc.code
                    if isinstance(exc, IngestionError)
                    else "pilot_contract_or_io",
                    "incident_assessment_sha256": incident_ref,
                }
            )
        )
        return 2 if isinstance(exc, IngestionError) and exc.code == "pilot_busy" else 1


def main(argv=None):
    configure_logging()
    with awake_for_cli():
        return _main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
