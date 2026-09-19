"""Run with python -m healthlab; offline modes never construct an HTTP client."""

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from healthlab.config import FPTSettings, Settings
from healthlab.extraction import extract_run
from healthlab.models import IngestionError, Question
from healthlab.orchestration import run_workflow
from healthlab.pipeline import import_probe, ingest, replay
from healthlab.provider import FPTProvider
from healthlab.pubmed import PubMedClient
from healthlab.store import RunStore
from healthlab.synthesis import render_saved_brief, synthesize_run


def main(argv=None):
    parser = argparse.ArgumentParser(description="HealthLab evidence pipeline")
    parser.add_argument(
        "--store", type=Path, help="Data root (default HEALTHLAB_STORE_DIR or outputs)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    online = sub.add_parser("ingest", help="Search, fetch and persist top 5 PubMed records")
    online.add_argument(
        "--query", required=True, help="Explicit PubMed query; no automatic translation"
    )
    online.add_argument("--question", help="Original user question; defaults to query")
    online.add_argument("--scope")
    online.add_argument("--sort", choices=["relevance", "pub_date"], default="relevance")
    online.add_argument("--mindate", help="YYYY-MM-DD publication date")
    online.add_argument("--maxdate", help="YYYY-MM-DD publication date")
    workflow = sub.add_parser(
        "run", help="Run ingestion, extraction and brief with durable stage tracking"
    )
    workflow.add_argument("--query", required=True)
    workflow.add_argument("--question")
    workflow.add_argument("--scope")
    workflow.add_argument("--sort", choices=["relevance", "pub_date"], default="relevance")
    workflow.add_argument("--mindate")
    workflow.add_argument("--maxdate")
    resume = sub.add_parser(
        "resume", help="Resume a workflow with unchanged code/model configuration"
    )
    resume.add_argument("run_id")
    offline = sub.add_parser("replay", help="Reparse cached raw objects without network")
    offline.add_argument("run_id")
    imported = sub.add_parser("import-probe", help="Import saved CP3 files without network")
    imported.add_argument("--artifact", type=Path, required=True)
    imported.add_argument("--xml", type=Path, required=True)
    extraction = sub.add_parser("extract", help="Extract evidence from saved snapshots with FPT")
    extraction.add_argument("run_id")
    extraction.add_argument("--refresh", action="store_true", help="Bypass extraction cache")
    extraction.add_argument(
        "--cache-only",
        action="store_true",
        help="Use cached evidence only; no network or API key needed",
    )
    extraction.add_argument(
        "--max-documents", type=int, help="Limit eligible snapshots (run marked partial)"
    )
    synthesis = sub.add_parser("brief", help="Generate a draft brief from a saved extraction run")
    synthesis.add_argument("run_id")
    rendering = sub.add_parser("render-brief", help="Restore a saved brief preview offline")
    rendering.add_argument("run_id")
    args = parser.parse_args(argv)
    try:
        settings = Settings()
        store = RunStore(args.store or settings.store_dir)
        if args.command in {"run", "resume"}:
            provider = FPTProvider(FPTSettings(), lazy=True)
            try:
                question = (
                    None
                    if args.command == "resume"
                    else Question(
                        raw_question=args.question or args.query,
                        query=args.query,
                        scope=args.scope,
                        sort=args.sort,
                        mindate=args.mindate,
                        maxdate=args.maxdate,
                    )
                )
                run = run_workflow(
                    store,
                    provider,
                    question=question,
                    client_factory=lambda: PubMedClient(settings),
                    resume_id=args.run_id if args.command == "resume" else None,
                    on_started=lambda run_id: print(
                        json.dumps({"workflow_run_id": run_id}), file=sys.stderr, flush=True
                    ),
                )
            finally:
                provider.close()
        elif args.command == "ingest":
            question = Question(
                raw_question=args.question or args.query,
                query=args.query,
                scope=args.scope,
                sort=args.sort,
                mindate=args.mindate,
                maxdate=args.maxdate,
            )
            client = PubMedClient(settings)
            try:
                run = ingest(store, question, client)
            finally:
                client.close()
        elif args.command == "replay":
            run = replay(store, args.run_id)
        elif args.command == "extract":
            if args.refresh and args.cache_only:
                raise ValueError("--refresh and --cache-only cannot be used together")
            provider = FPTProvider(FPTSettings(), cache_only=args.cache_only)
            try:
                run = extract_run(
                    store,
                    args.run_id,
                    provider,
                    refresh=args.refresh,
                    max_documents=args.max_documents,
                )
            finally:
                provider.close()
        elif args.command == "brief":
            provider = FPTProvider(FPTSettings(), lazy=True)
            try:
                run = synthesize_run(store, args.run_id, provider)
            finally:
                provider.close()
        elif args.command == "render-brief":
            path = render_saved_brief(store, args.run_id)
            print(json.dumps({"brief": str(path)}, ensure_ascii=False))
            return 0
        else:
            run = import_probe(store, args.artifact, args.xml)
        print(
            json.dumps(
                {
                    "run_id": run["run_id"],
                    "status": run["status"],
                    "counts": run.get("counts"),
                    "metrics": run.get("metrics"),
                    "stages": run.get("stages"),
                    "brief": run.get("brief")
                    or (
                        str(store.root / "runs" / run["run_id"] / "brief.md")
                        if run.get("markdown_sha256")
                        else None
                    ),
                    "errors": run["errors"],
                    "warnings": run["warnings"],
                    "manifest": str(store.root / "runs" / run["run_id"] / "manifest.json"),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0 if run["status"] == "completed" else 1
    except (IngestionError, ValueError, ValidationError, OSError) as exc:
        # Network exceptions never reach here with a credential-bearing URL.
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
