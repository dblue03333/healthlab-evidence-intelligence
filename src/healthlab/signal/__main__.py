"""Public-source collection commands: check, probe, collect, resume, export and import."""

import argparse
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from healthlab.common import configure_logging
from healthlab.config import Settings
from healthlab.models import IngestionError
from healthlab.pubmed import PubMedClient
from healthlab.signal.backup import export_collection, verify_bundle
from healthlab.signal.collection import collect, collection_status, resume_collection
from healthlab.signal.contracts import Registry
from healthlab.signal.source_http import FetchPolicy
from healthlab.signal.source_probe import ProbeTargets, probe_sources
from healthlab.store import RunStore

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=Path("outputs/signal"))
    parser.add_argument(
        "--storage-config", type=Path, help="Opt-in GCS/PostgreSQL collector config"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check-config")
    check.add_argument("--registry", type=Path, default=Path("config/signal_sources.json"))
    probe = sub.add_parser("probe-official", help="Probe public source pages; no AI or email")
    probe.add_argument(
        "--targets", type=Path, default=Path("config/signal_official_probe_targets.json")
    )
    collection = sub.add_parser("collect")
    collection.add_argument("--registry", type=Path, default=Path("config/signal_sources.json"))
    collection.add_argument("--mindate", type=date.fromisoformat)
    collection.add_argument("--maxdate", type=date.fromisoformat)
    collection.add_argument("--max-attempts", type=int, default=3)
    collection.add_argument("--request-interval", type=float, default=0.25)
    resume = sub.add_parser("resume-collection", help="Resume interrupted HTML collection")
    resume.add_argument("collection_id")
    status = sub.add_parser("collection-status", help="Inspect collection progress without network")
    status.add_argument("collection_id")
    backup = sub.add_parser("export-collection", help="Export a verified portable HTML collection")
    backup.add_argument("collection_id")
    backup.add_argument("--out", required=True, type=Path)
    verify = sub.add_parser("verify-bundle", help="Verify a portable collection offline")
    verify.add_argument("path", type=Path)
    sub.add_parser("init-storage", help="Explicitly initialize the configured PostgreSQL namespace")
    restore = sub.add_parser(
        "import-collection", help="Import a verified collection bundle into cloud storage"
    )
    restore.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.storage_config:
            if args.command not in {
                "collect",
                "resume-collection",
                "collection-status",
                "export-collection",
                "init-storage",
                "import-collection",
            }:
                raise ValueError("Cloud storage is available for collector commands only")
            from healthlab.signal.cloud_storage import storage_from_config

            store = storage_from_config(args.storage_config)
        else:
            if args.command in {"init-storage", "import-collection"}:
                raise ValueError("This command requires --storage-config")
            store = RunStore(args.store)
        if args.command == "check-config":
            registry = Registry.model_validate_json(args.registry.read_bytes())
            result = {
                "sources": [
                    {
                        "source_id": s.source_id,
                        "organization": s.organization,
                        "url": s.url,
                        "kind": s.kind,
                        "enabled": s.enabled,
                        "required": s.required,
                        "scope": s.usage_policy,
                        "media_policy": s.html_media_policy if s.kind == "html" else None,
                    }
                    for s in registry.sources
                ],
                "gmail_sender_configured": bool(os.environ.get("HEALTHLAB_GMAIL_USER")),
                "gmail_app_password_configured": bool(
                    os.environ.get("HEALTHLAB_GMAIL_APP_PASSWORD")
                ),
                "recipients_configured": bool(os.environ.get("HEALTHLAB_EMAIL_TO")),
                "scheduler": "not_configured",
                "storage": "local_only",
            }
        elif args.command == "init-storage":
            store.state.initialize()
            result = {"storage": "gcs_postgres", "schema_version": 1, "initialized": True}
        elif args.command == "import-collection":
            from healthlab.signal.cloud_storage import import_bundle

            result = import_bundle(store, args.path)
        elif args.command == "resume-collection":
            run = resume_collection(store, args.collection_id)
            result = {k: run[k] for k in ("run_id", "status", "source_checks", "documents")}
        elif args.command == "collection-status":
            result = collection_status(store, args.collection_id)
        elif args.command == "export-collection":
            result = export_collection(store, args.collection_id, args.out)
        elif args.command == "verify-bundle":
            result = verify_bundle(args.path)
        elif args.command == "probe-official":
            targets = ProbeTargets.model_validate_json(args.targets.read_bytes())
            run = probe_sources(store, targets)
            result = {key: run[key] for key in ("run_id", "status", "source_checks", "warnings")}
        elif args.command == "collect":
            registry = Registry.model_validate_json(args.registry.read_bytes())
            if args.storage_config and any(s.kind != "html" for s in registry.sources if s.enabled):
                raise ValueError("Cloud collection currently supports official HTML sources only")
            # Explicit client only when PubMed is enabled; RSS-only setup needs no contact.
            client = None
            if any(s.kind == "pubmed" and s.enabled for s in registry.sources):
                client = PubMedClient(Settings())
            try:
                today = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date()
                run = collect(
                    store,
                    registry,
                    mindate=args.mindate or today - timedelta(days=7),
                    maxdate=args.maxdate or today,
                    pubmed_client=client,
                    fetch_policy=FetchPolicy(
                        max_attempts=args.max_attempts, request_interval=args.request_interval
                    ),
                    on_started=lambda run_id: logger.info("Run %s is running", run_id),
                )
                result = {k: run[k] for k in ("run_id", "status", "source_checks", "documents")}
            finally:
                if client:
                    client.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("status") in {"failed", "partial"}:
            return 1
        return 0
    except (IngestionError, ValueError, OSError) as exc:
        # Schema errors may embed input. Do not print raw exception text.
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error_code": exc.code
                    if isinstance(exc, IngestionError)
                    else "configuration_or_io",
                    "message": "Check local configuration or saved run.",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
