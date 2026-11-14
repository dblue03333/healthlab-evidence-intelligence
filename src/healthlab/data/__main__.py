"""Collect/replay public text sources and publish local Medallion datasets; no AI."""

import argparse
import json
import logging
from datetime import date
from pathlib import Path

from healthlab.common import JsonObject, configure_logging
from healthlab.data.bundle import export_dataset, verify_export
from healthlab.data.datasets import build_dataset, read_dataset, summary
from healthlab.data.supervision import record_operation_failure, supervise
from healthlab.models import IngestionError
from healthlab.signal.collection import (
    collect,
    repair_collection,
    replay_collection,
    resume_collection,
)
from healthlab.signal.contracts import Registry
from healthlab.signal.source_http import FetchPolicy
from healthlab.signal.storage import collection_storage
from healthlab.store import RunStore

logger = logging.getLogger(__name__)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=Path("outputs/data"))
    parser.add_argument(
        "--storage-config", type=Path, help="Optional existing GCS/PostgreSQL collector adapter"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    collect_parser = commands.add_parser(
        "collect", help="Collect fresh data; reconcile compatible historical candidates"
    )
    collect_parser.add_argument("--registry", type=Path, default=Path("config/signal_sources.json"))
    collect_parser.add_argument("--mindate", required=True, type=date.fromisoformat)
    collect_parser.add_argument("--maxdate", required=True, type=date.fromisoformat)
    collect_parser.add_argument("--max-attempts", type=int, default=3)
    collect_parser.add_argument("--request-interval", type=float, default=0.25)
    for command in ("resume", "replay", "repair", "build"):
        action = commands.add_parser(command)
        action.add_argument("collection_id")
        if command in {"replay", "repair"}:
            action.add_argument(
                "--registry",
                type=Path,
                help="Changed source rules/budgets; replay stays offline, repair may fetch missing responses",
            )
    commands.add_parser("show").add_argument("dataset_id")
    export = commands.add_parser("export")
    export.add_argument("dataset_id")
    export.add_argument("--out", type=Path, required=True)
    commands.add_parser("verify").add_argument("path", type=Path)
    commands.add_parser("catalog")
    args = parser.parse_args(argv)
    result: JsonObject
    try:
        if args.command == "verify":
            result = verify_export(args.path)
        else:
            if args.storage_config:
                from healthlab.signal.cloud_storage import storage_from_config

                store = storage_from_config(args.storage_config)
            else:
                store = RunStore(args.store)

            def started(run_id):
                logger.info("Collection %s is running", run_id)

            if args.command == "collect":
                registry = Registry.model_validate_json(args.registry.read_bytes())
                if not any(s.enabled for s in registry.sources) or any(
                    s.enabled and s.kind != "html" for s in registry.sources
                ):
                    raise ValueError("Data pipeline accepts HTML/API sources only")
                run = collect(
                    store,
                    registry,
                    mindate=args.mindate,
                    maxdate=args.maxdate,
                    fetch_policy=FetchPolicy(
                        max_attempts=args.max_attempts, request_interval=args.request_interval
                    ),
                    on_started=started,
                    remember_candidates=True,
                )
                result = build_dataset(store, run["run_id"])
            elif args.command in {"resume", "replay", "repair", "build"}:
                if args.command == "resume":
                    run = resume_collection(store, args.collection_id)
                elif args.command in {"replay", "repair"}:
                    replay_registry = (
                        Registry.model_validate_json(args.registry.read_bytes())
                        if args.registry
                        else None
                    )
                    reprocess = replay_collection if args.command == "replay" else repair_collection
                    run = reprocess(
                        store, args.collection_id, registry=replay_registry, on_started=started
                    )
                else:
                    run = {"run_id": args.collection_id}
                result = build_dataset(store, run["run_id"])
            elif args.command == "show":
                _, bronze, silver, gold = read_dataset(store, args.dataset_id)
                result = summary(args.dataset_id, bronze, silver, gold)
            elif args.command == "export":
                result = export_dataset(store, args.dataset_id, args.out)
            else:
                storage = collection_storage(store)
                datasets: dict[str, JsonObject] = {}
                for run_id in storage.state.list_runs("signal_dataset"):
                    run = storage.load(run_id)
                    if run["status"] == "completed":
                        datasets[run["dataset_sha256"]] = {
                            "dataset_id": run["dataset_sha256"],
                            "status": run["dataset_status"],
                            "window": run["question"],
                            "collection_id": run["collection_id"],
                        }
                result = {
                    "datasets": sorted(
                        datasets.values(), key=lambda r: (r["window"]["mindate"], r["dataset_id"])
                    )
                }
        if args.command in {"collect", "resume", "replay", "repair", "build"} and isinstance(
            store, RunStore
        ):
            result["supervision"] = supervise(store, result["dataset_id"])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return (
            1
            if (
                result.get("status") in {"partial", "failed"}
                or result.get("supervision", {}).get("gate") in {"blocked", "needs_review"}
            )
            else 0
        )
    except (IngestionError, ValueError, OSError, KeyError, TypeError) as exc:
        incident_ref = None
        if not args.storage_config and args.command in {
            "collect",
            "resume",
            "replay",
            "repair",
            "build",
        }:
            try:
                incident_ref = record_operation_failure(RunStore(args.store), args.command, exc)
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
                    else "data_contract_or_io",
                    "message": "Inspect configuration and saved artifacts; no ready dataset was implied.",
                    "incident_assessment_sha256": incident_ref,
                }
            )
        )
        return 1


def main(argv: list[str] | None = None) -> int:
    from healthlab.data.power import awake_for_cli

    configure_logging()
    with awake_for_cli():
        return _main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
