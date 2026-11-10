"""Manual, versioned Gold → AI evidence candidates → review → unapproved draft.

Every Gold article is read in full. No keyword prefilter, retry or silent fallback.
AI agreement and exact citations do not certify semantic truth or human approval.
"""

import argparse
import json
from pathlib import Path

from healthlab.config import FPTSettings
from healthlab.data.power import awake_for_cli
from healthlab.editorial.inference import run_calls
from healthlab.editorial.planning import plan_draft, plan_review, plan_scope
from healthlab.editorial.report import export_report
from healthlab.editorial.runs import prepare
from healthlab.provider import FPTProvider
from healthlab.store import RunStore


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m healthlab.editorial", description=__doc__)
    parser.add_argument("--store", type=Path, default=Path("outputs/editorial_20261029"))
    parser.add_argument(
        "--fpt-thinking-mode",
        choices=("disabled", "enabled", "provider_default"),
        help="Override FPT_THINKING_MODE; pinned when preparing the run",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("dataset_id")
    prep.add_argument("--source-store", type=Path, default=Path("outputs/data_20261026"))
    prep.add_argument("--call-budget", type=int, default=180)
    for role in ("classify", "scope", "review", "draft", "report"):
        command = commands.add_parser(role)
        command.add_argument("run_id")
        if role in {"classify", "scope", "review", "draft"}:
            command.add_argument("--workers", type=int, default=3)
            command.add_argument("--max-calls", type=int, default=180)
        if role in {"scope", "review"}:
            command.add_argument("--all-articles", action="store_true")
    args = parser.parse_args(argv)
    store = RunStore(args.store)
    settings = FPTSettings(
        **({"thinking_mode": args.fpt_thinking_mode} if args.fpt_thinking_mode else {})
    )
    provider = FPTProvider(settings, lazy=args.command in {"prepare", "report"})
    try:
        with awake_for_cli():
            if args.command == "prepare":
                result = prepare(
                    RunStore(args.source_store),
                    store,
                    args.dataset_id,
                    provider,
                    call_budget=args.call_budget,
                )
            elif args.command == "report":
                result = export_report(store, args.run_id)
            else:
                if args.command == "review":
                    plan_review(store, args.run_id, all_articles=args.all_articles)
                elif args.command == "scope":
                    plan_scope(store, args.run_id, all_articles=args.all_articles)
                elif args.command == "draft":
                    plan_draft(store, args.run_id)
                result = run_calls(
                    store,
                    args.run_id,
                    provider,
                    role=args.command,
                    workers=args.workers,
                    max_calls=args.max_calls,
                )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return (
            1
            if result.get("errors")
            or result.get("classified", result.get("articles")) != result.get("articles")
            else 0
        )
    finally:
        provider.close()
