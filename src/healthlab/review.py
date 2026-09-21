"""Offline, version-bound human review. Local reviewer names are declarations, not auth."""

import argparse
import fcntl
import json
from pathlib import Path

from healthlab.brief import md, render_brief
from healthlab.models import IngestionError
from healthlab.pubmed import utc_now
from healthlab.store import RunStore, json_bytes
from healthlab.synthesis import render_saved_brief, write_preview


def packet(store, run_id):
    # Revalidate references and live source objects, not a mutable Markdown preview.
    render_saved_brief(store, run_id)
    run = store.load(run_id)
    brief = json.loads(store.read(run["brief_sha256"]))
    if brief["run_id"] != run_id or brief["brief_version_id"] != run["brief_version_id"]:
        raise ValueError("Brief version does not match run")
    return {
        "review_schema": "human-review-1",
        "run_id": run_id,
        "brief_sha256": run["brief_sha256"],
        "brief_version_id": brief["brief_version_id"],
        "brief": brief,
        "limits": "Automated checks validate references, not factual correctness. Reviewer identity is self-declared locally.",
    }


def history(store, run_id, source=None):
    source = source or packet(store, run_id)
    pointer = store.root / "runs" / run_id / "review-head.json"
    head = json.loads(pointer.read_bytes()) if pointer.exists() else None
    events, seen, cursor = [], set(), head
    while cursor is not None:
        if not isinstance(cursor, str) or cursor in seen:
            raise ValueError("Invalid review history")
        seen.add(cursor)
        event = json.loads(store.read(cursor))
        if any(event.get(k) != source[k] for k in ("run_id", "brief_sha256", "brief_version_id")):
            raise ValueError("Review belongs to another brief version")
        events.append(dict(event, event_sha256=cursor))
        cursor = event["previous"]
    return {"head": head, "events": list(reversed(events))}


def submit(store, run_id, *, expected_head, action, reviewer, rationale, edits=None):
    if action not in {"accept", "edit", "reject", "needs_revision"}:
        raise ValueError("Unknown review action")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("Named human reviewer required")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("Review rationale required")
    source = packet(store, run_id)
    claims = {c["claim_id"] for c in source["brief"]["synthesis"]["claims"]}
    if edits is not None and action != "edit":
        raise ValueError("Only edit actions may contain Vietnamese text")
    if action == "edit" and (
        not isinstance(edits, dict)
        or not edits
        or not set(edits) <= claims
        or any(not isinstance(v, str) or not v.strip() for v in edits.values())
    ):
        raise ValueError("Edits must map existing claim IDs to nonempty Vietnamese text")
    directory = store.root / "runs" / run_id
    with (directory / "review.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = history(store, run_id, source)
        if expected_head != state["head"]:
            raise ValueError("Stale review head: reload history before submitting")
        current_edits = state["events"][-1]["vietnamese_edits"] if state["events"] else {}
        current_edits = dict(current_edits)
        if action == "edit":
            current_edits.update(edits)
        event = {
            "review_schema": "human-review-1",
            **{k: source[k] for k in ("run_id", "brief_sha256", "brief_version_id")},
            "previous": state["head"],
            "action": action,
            "status": "approved" if action == "accept" else action,
            "reviewer": reviewer.strip(),
            "reviewer_type": "human",
            "rationale": rationale.strip(),
            "created_at": utc_now(),
            "vietnamese_edits": current_edits,
        }
        digest = store.put(json_bytes(event))
        # The immutable event is durable before atomically advancing its pointer.
        write_preview(directory / "review-head.json", json.dumps(digest))
    return dict(event, event_sha256=digest)


def export_marketing(store, run_id):
    source = packet(store, run_id)
    directory = store.root / "runs" / run_id
    with (directory / "review.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = history(store, run_id, source)
        if not state["events"] or state["events"][-1]["status"] != "approved":
            raise ValueError("Latest review must approve this exact version before export")
        event = state["events"][-1]
        lines = [
            "# Human-reviewed marketing handoff",
            f"Brief version: {source['brief_version_id']}",
            f"Review event: {state['head']}",
            f"Reviewer: {md(event['reviewer'])}; timestamp: {event['created_at']}",
            f"Rationale: {md(event['rationale'])}",
            "Human approval is recorded locally; it is not independent clinical or regulatory certification.",
            "## Vietnamese editorial text (separate from source)",
        ]
        for claim in source["brief"]["synthesis"]["claims"]:
            text = event["vietnamese_edits"].get(claim["claim_id"])
            if text is not None:
                lines.append(f"### {md(claim['claim_id'])}\n\n{md(text)}")
                for ref in claim["references"]:
                    pid = ref["passage_id"]
                    lines.append(f"[Source passage](#passage-{pid})")
        if not event["vietnamese_edits"]:
            lines.append("No Vietnamese editorial text supplied.")
        lines.extend(
            ["## Original AI draft and source evidence (preserved)", render_brief(source["brief"])]
        )
        content = "\n\n".join(lines) + "\n"
        digest = store.put(content.encode())
        # Same directory depth as original brief so source-object links still resolve.
        path = directory / f"marketing-{state['head']}.md"
        write_preview(path, content)
    return {"path": str(path), "sha256": digest, "review_event": state["head"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=Path("outputs"))
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("packet", "history", "export"):
        sub.add_parser(name).add_argument("run_id")
    command = sub.add_parser("submit")
    command.add_argument("run_id")
    command.add_argument("--expected-head", required=True, help="Last event hash, or 'none'")
    command.add_argument(
        "--action", required=True, choices=["accept", "edit", "reject", "needs_revision"]
    )
    command.add_argument("--reviewer", required=True)
    command.add_argument("--rationale", required=True)
    command.add_argument(
        "--edits", type=Path, help="JSON object: claim ID to Vietnamese editorial text"
    )
    args = parser.parse_args(argv)
    try:
        store = RunStore(args.store)
        if args.command == "submit":
            result = submit(
                store,
                args.run_id,
                expected_head=None if args.expected_head == "none" else args.expected_head,
                action=args.action,
                reviewer=args.reviewer,
                rationale=args.rationale,
                edits=json.loads(args.edits.read_bytes()) if args.edits else None,
            )
        elif args.command == "export":
            result = export_marketing(store, args.run_id)
        elif args.command == "history":
            result = history(store, args.run_id)
        else:
            result = packet(store, args.run_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (IngestionError, ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
