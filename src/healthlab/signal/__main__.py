"""Local pilot commands. Email export is offline; send requires explicit --send."""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from healthlab.config import FPTSettings, GeminiSettings, OpenRouterSettings, Settings
from healthlab.gemini import GeminiProvider
from healthlab.models import IngestionError
from healthlab.openrouter import OpenRouterProvider
from healthlab.provider import FPTProvider
from healthlab.pubmed import PubMedClient
from healthlab.signal.backup import export_collection, verify_bundle
from healthlab.signal.collection import collect, collection_status, resume_collection
from healthlab.signal.contracts import Registry
from healthlab.signal.newsletter import (
    approve,
    draft_issue,
    export_email,
    load_issue,
    render_preview,
    revise,
    send_gmail,
)
from healthlab.signal.source_http import FetchPolicy
from healthlab.signal.source_probe import ProbeTargets, probe_sources
from healthlab.signal.triage import (
    draft_selection,
    prepare_triage,
    propose_triage,
    render_triage_packet,
    revalidate_model_attempt,
    submit_triage,
    triage_packet,
)
from healthlab.store import RunStore


def signal_provider(name):
    if name == "fpt":
        return FPTProvider(FPTSettings())
    if name == "openrouter":
        return OpenRouterProvider(OpenRouterSettings())
    if name == "gemini":
        return GeminiProvider(GeminiSettings())
    raise ValueError("Unknown Signal provider")


def main(argv=None):
    load_dotenv()
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
    triage = sub.add_parser("triage", help="Prepare/review versioned editorial selection offline")
    triage_commands = triage.add_subparsers(dest="triage_command", required=True)
    triage_prepare = triage_commands.add_parser("prepare")
    triage_prepare.add_argument("collection_id")
    triage_prepare.add_argument("--mindate", type=date.fromisoformat)
    triage_prepare.add_argument("--maxdate", type=date.fromisoformat)
    triage_propose = triage_commands.add_parser(
        "propose", help="Opt-in model calls; outputs remain AI proposals"
    )
    triage_propose.add_argument("triage_id")
    triage_propose.add_argument("--expected-head", required=True)
    triage_propose.add_argument(
        "--provider", choices=["fpt", "openrouter", "gemini"], default="fpt"
    )
    triage_propose.add_argument("--max-documents", type=int, default=6)
    triage_propose.add_argument("--max-model-calls", type=int, default=30)
    triage_propose.add_argument(
        "--resumable", action="store_true", help="Explicit bounded recovery of a new managed run"
    )
    triage_propose.add_argument(
        "--total-call-budget",
        type=int,
        help="Pinned lifetime budget across restarts (required on first managed invocation)",
    )
    triage_propose.add_argument(
        "--max-attempts", type=int, default=3, help="Pinned attempts per article for resumable runs"
    )
    triage_propose.add_argument(
        "--prompt-version",
        choices=[
            "signal-triage-prompt-6",
            "signal-triage-prompt-7",
            "signal-triage-prompt-8",
            "signal-triage-prompt-9",
            "signal-triage-prompt-10",
        ],
        help="Explicit opt-in contract; defaults to the stable runtime version",
    )
    revalidate = triage_commands.add_parser(
        "revalidate", help="Recheck saved invalid output; no API call"
    )
    revalidate.add_argument("triage_id")
    revalidate.add_argument("snapshot_id")
    revalidate.add_argument("--expected-head", required=True)
    revalidate.add_argument("--prompt-version", required=True)
    verify = triage_commands.add_parser(
        "verify", help="Append evidence-backed agent proposals, not human labels"
    )
    verify.add_argument("triage_id")
    verify.add_argument("--evidence", type=Path, required=True)
    verify.add_argument("--expected-head", required=True)
    for name in ("status", "packet", "submit"):
        command = triage_commands.add_parser(name)
        command.add_argument("triage_id")
        if name == "packet":
            command.add_argument(
                "--html", action="store_true", help="Render local HTML/JSON review packet"
            )
        if name == "submit":
            command.add_argument("--decisions", type=Path, required=True)
            command.add_argument("--expected-head", required=True)
            command.add_argument("--reviewer", required=True)
            command.add_argument("--rationale", required=True)
    draft = sub.add_parser("draft")
    draft.add_argument("collection_id")
    draft.add_argument("--provider", choices=["fpt", "openrouter", "gemini"], default="fpt")
    draft.add_argument(
        "--structured-output",
        action="store_true",
        help="Request native JSON schema; no text fallback",
    )
    draft.add_argument("--max-documents", type=int, default=6)
    draft.add_argument("--triage-run", required=True)
    for name in ("preview", "status", "approve", "revise", "email"):
        command = sub.add_parser(name)
        command.add_argument("issue_id")
        if name in {"approve", "revise"}:
            command.add_argument("--expected-version", required=True)
        if name == "approve":
            command.add_argument("--reviewer", required=True)
            command.add_argument("--rationale", required=True)
        if name == "revise":
            command.add_argument("--content", required=True, type=Path)
        if name == "email":
            command.add_argument("--send", action="store_true", help="Actually send via Gmail SMTP")
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
                "triage",
            }:
                raise ValueError(
                    "Cloud storage is available for collector and triage commands only"
                )
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
                    {"source_id": s.source_id, "kind": s.kind, "enabled": s.enabled}
                    for s in registry.sources
                ],
                "gmail_sender_configured": bool(os.environ.get("HEALTHLAB_GMAIL_USER")),
                "gmail_app_password_configured": bool(
                    os.environ.get("HEALTHLAB_GMAIL_APP_PASSWORD")
                ),
                "recipients_configured": bool(os.environ.get("HEALTHLAB_EMAIL_TO")),
                "gemini_api_key_configured": bool(GeminiSettings().api_key.get_secret_value()),
                "gemini_model": GeminiSettings().model,
                "gemini_billing_tier": "not_verifiable_from_api_key",
                "scheduler": "not_configured",
                "storage": "local_only",
            }
        elif args.command == "triage":
            if args.triage_command == "prepare":
                run = prepare_triage(
                    store, args.collection_id, mindate=args.mindate, maxdate=args.maxdate
                )
                result = {
                    "triage_run_id": run["run_id"],
                    "head": run["head"],
                    "stage": run["stage"],
                }
            elif args.triage_command == "propose":
                packet = triage_packet(store, args.triage_id)
                attempted = {item["snapshot_id"] for item in packet["model_attempts"]}
                needs_calls = any(
                    row["date_eligible"]
                    and row["effective_decision"] is None
                    and (args.resumable or row["snapshot_id"] not in attempted)
                    for row in packet["rows"]
                )
                provider = None if not needs_calls else signal_provider(args.provider)
                try:
                    result = propose_triage(
                        store,
                        args.triage_id,
                        provider,
                        expected_head=None if args.expected_head == "none" else args.expected_head,
                        max_documents=args.max_documents,
                        max_model_calls=args.max_model_calls,
                        prompt_version=args.prompt_version,
                        resumable=args.resumable,
                        total_call_budget=args.total_call_budget,
                        max_attempts=args.max_attempts,
                    )
                finally:
                    if provider:
                        provider.close()
            elif args.triage_command == "revalidate":
                result = revalidate_model_attempt(
                    store,
                    args.triage_id,
                    args.snapshot_id,
                    expected_head=None if args.expected_head == "none" else args.expected_head,
                    prompt_version=args.prompt_version,
                )
            elif args.triage_command == "verify":
                from healthlab.signal.verification import submit_agent_verification

                result = submit_agent_verification(
                    store,
                    args.triage_id,
                    json.loads(args.evidence.read_bytes()),
                    expected_head=None if args.expected_head == "none" else args.expected_head,
                )
            elif args.triage_command == "submit":
                result = submit_triage(
                    store,
                    args.triage_id,
                    json.loads(args.decisions.read_bytes()),
                    expected_head=None if args.expected_head == "none" else args.expected_head,
                    reviewer=args.reviewer,
                    rationale=args.rationale,
                )
            elif args.triage_command == "packet":
                if args.html:
                    if args.storage_config:
                        raise ValueError("HTML packet rendering requires a local store")
                    result = {"preview": str(render_triage_packet(store, args.triage_id))}
                else:
                    result = triage_packet(store, args.triage_id)
            else:
                packet = triage_packet(store, args.triage_id)
                result = {
                    k: packet[k]
                    for k in (
                        "triage_run_id",
                        "head",
                        "monitoring_window",
                        "counts",
                        "unjudged",
                        "collection_window_covers_monitoring",
                        "recovery",
                    )
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
                    on_started=lambda run_id: print(
                        json.dumps({"run_id": run_id, "status": "running"}),
                        file=sys.stderr,
                        flush=True,
                    ),
                )
                result = {k: run[k] for k in ("run_id", "status", "source_checks", "documents")}
            finally:
                if client:
                    client.close()
        elif args.command == "draft":
            eligible, _ = draft_selection(
                store, args.collection_id, args.triage_run, require_current=True
            )
            provider = None if not eligible else signal_provider(args.provider)
            try:
                run = draft_issue(
                    store,
                    args.collection_id,
                    provider,
                    triage_id=args.triage_run,
                    max_documents=args.max_documents,
                    structured_output=args.structured_output,
                )
                result = {k: run.get(k) for k in ("run_id", "status", "issue_sha256", "errors")}
            finally:
                if provider:
                    provider.close()
        elif args.command == "preview":
            result = {"preview": str(render_preview(store, args.issue_id))}
        elif args.command == "status":
            run, _, _ = load_issue(store, args.issue_id)
            result = run
        elif args.command == "approve":
            run = approve(
                store,
                args.issue_id,
                expected_version=args.expected_version,
                reviewer=args.reviewer,
                rationale=args.rationale,
            )
            result = {
                "approval_sha256": run["approval_sha256"],
                "issue_sha256": run["issue_sha256"],
            }
        elif args.command == "revise":
            run = revise(
                store,
                args.issue_id,
                json.loads(args.content.read_bytes()),
                expected_version=args.expected_version,
            )
            result = {"issue_sha256": run["issue_sha256"], "approval": "reset"}
        else:
            sender = os.environ.get("HEALTHLAB_GMAIL_USER", "")
            recipients = [
                r.strip() for r in os.environ.get("HEALTHLAB_EMAIL_TO", "").split(",") if r.strip()
            ]
            if args.send:
                result = {
                    "delivery": send_gmail(
                        store,
                        args.issue_id,
                        sender,
                        recipients,
                        os.environ.get("HEALTHLAB_GMAIL_APP_PASSWORD", ""),
                    )
                }
            else:
                result = {
                    "sent": False,
                    "email_previews": export_email(store, args.issue_id, sender, recipients),
                }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("status") in {"failed", "partial"}:
            return 1
        if "delivery" in result and any(o["status"] != "accepted" for o in result["delivery"]):
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
                    "message": "Check local configuration or saved run; no email was implied successful.",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
