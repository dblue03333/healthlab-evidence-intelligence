"""Assess captured data, raise local incidents and review them without AI or HTTP.

Operational readiness is separate from immutable Gold and editorial acceptance.
"""

import argparse
import json
import logging
import uuid
from html import escape
from pathlib import Path

from healthlab.common import configure_logging
from healthlab.data.datasets import gold_records, read_dataset
from healthlab.data.incidents import (
    digest,
    incident_id,
    record_assessment,
    relevant_open,
    snapshot,
    transition,
)
from healthlab.models import IngestionError
from healthlab.signal.contracts import Registry
from healthlab.store import RunStore

logger = logging.getLogger(__name__)

VERSION = "healthlab-supervision-1"


def source_contract(source):
    discovery = source.html_discovery
    branches = []
    if discovery and discovery.moh_portal_categories:
        branches = [{"category_id": c} for c in discovery.moh_portal_categories]
    elif not (discovery and discovery.wordpress_api_url):
        branches = [
            {"url": u}
            for u in [source.url] + (discovery.additional_listing_urls if discovery else [])
        ]
    else:
        branches = [{"api_url_prefix": discovery.wordpress_api_url}]
    if discovery:
        for field, branch in (
            ("hcm_search_inventory", "hcm_c1780"),
            ("who_speeches_api", "who_speeches"),
            ("who_ageing_widget", "who_ageing"),
        ):
            if getattr(discovery, field):
                branches.append({"branch_id": branch})
    return {
        "source_id": source.source_id,
        "configuration_sha256": digest(source.model_dump(mode="json")),
        "window_date_policy": source.resolved_html_rules.window_date_policy,
        "required_branches": branches,
        "limits": {
            "article_fetches": source.max_article_fetches,
            "listing_pages": discovery.max_listing_pages if discovery else 1,
            "response_bytes": source.html_max_response_bytes,
        },
        "limitations": ["ageing_widget_current_snapshot_only; historical_membership_unknown"]
        if discovery and discovery.who_ageing_widget
        else [],
    }


def _finding(source_id, code, entity, *, severity="error", evidence=None):
    return {
        "source_id": source_id,
        "code": code,
        "entity": entity,
        "severity": severity,
        "evidence": evidence or {},
        "next_action": "Inspect saved raw/checkpoint and source rules; repair or review, then resolve with evidence.",
    }


def _branch_matches(branch, page):
    if "api_url_prefix" in branch:
        return page.get("url", "").startswith(branch["api_url_prefix"])
    return all(page.get(k) == v for k, v in branch.items())


def assessment(store, dataset_id):
    """Deterministic assessment of frozen inputs; does not change their contents."""
    result = {
        "schema_version": VERSION,
        "dataset_id": dataset_id,
        "window": None,
        "source_ids": [],
        "technical_status": "failed",
        "contracts": [],
        "findings": [],
        "coverage_acceptance": "not_assessed",
    }
    try:
        _, bronze, silver, gold = read_dataset(store, dataset_id)
    except (IngestionError, ValueError, OSError) as exc:
        result["findings"] = [
            _finding(
                "pipeline",
                "dataset_verification_failed",
                dataset_id,
                evidence={"error_code": getattr(exc, "code", type(exc).__name__)},
            )
        ]
        return result
    result.update(
        window=gold.window.model_dump(mode="json"),
        technical_status=gold.status,
        collection_id=bronze.collection_id,
    )
    # Use the verified frozen manifest, not a possibly edited operational copy.
    run = json.loads(store.read(bronze.collection_manifest_sha256))
    checks = {c["source_id"]: c for c in run["source_checks"]}
    qualities = {q.source_id: q for q in silver.sources}
    sources = [s for s in Registry.model_validate(bronze.registry).sources if s.enabled]
    result["source_ids"] = sorted(s.source_id for s in sources)
    findings = []
    for source in sources:
        sid = source.source_id
        contract = source_contract(source)
        result["contracts"].append(contract)
        quality = qualities.get(sid)
        check = checks.get(sid, {})
        if quality is None:
            findings.append(_finding(sid, "source_missing", sid))
            continue
        for issue in quality.issues:
            findings.append(
                _finding(
                    sid,
                    issue.get("code", "source_issue"),
                    issue.get("url", sid),
                    severity="review" if "date" in issue.get("code", "") else "error",
                    evidence=issue,
                )
            )
        if not quality.coverage_complete and not quality.issues:
            findings.append(
                _finding(sid, "source_incomplete", sid, evidence={"status": quality.status})
            )
        pages = check.get("listing_pages", [])
        for branch in contract["required_branches"]:
            matched = [p for p in pages if _branch_matches(branch, p)]
            if not any(p.get("status") == "completed" and p.get("raw_sha256") for p in matched):
                findings.append(
                    _finding(
                        sid,
                        "required_branch_unverified",
                        digest(branch),
                        evidence={"branch": branch},
                    )
                )
    # Deduplicate equal findings; never let dictionary/list order alter identity.
    result["findings"] = [
        json.loads(v)
        for v in sorted({json.dumps(f, ensure_ascii=False, sort_keys=True) for f in findings})
    ]
    return result


def supervise(store, dataset_id):
    value = assessment(store, dataset_id)
    ref, state, changes = record_assessment(store, value)
    active = relevant_open(state, value["source_ids"], value["window"])
    # Even a human resolution cannot turn a broken historical dataset into ready.
    errors = [f for f in value["findings"] if f["severity"] == "error"]
    gate = (
        "blocked"
        if errors or value["technical_status"] == "failed"
        else (
            "needs_review"
            if value["findings"] or active or value["technical_status"] != "ready"
            else "ready"
        )
    )
    return {
        "schema_version": VERSION,
        "dataset_id": dataset_id,
        "assessment_sha256": ref,
        "technical_status": value["technical_status"],
        "gate": gate,
        "current_finding_ids": sorted({incident_id(f) for f in value["findings"]}),
        "active_incident_ids": sorted(r["incident_id"] for r in active),
        "incident_changes": changes,
        "coverage_acceptance": "not_assessed",
    }


def record_pilot_failure(store, journal, exc):
    """Best-effort caller must preserve the original exception if storage fails."""
    code = getattr(exc, "code", "pilot_interrupted")
    value = {
        "schema_version": VERSION,
        "attempt_id": journal["attempt_id"],
        "window": journal["config"]["window"],
        "stage": journal["stage"],
        "findings": [
            _finding(
                "pipeline",
                code,
                journal["stage"],
                evidence={
                    "exception_type": type(exc).__name__,
                    "collection_id": journal.get("collection_id"),
                    "dataset_id": journal.get("dataset_id"),
                },
            )
        ],
    }
    return record_assessment(store, value)[0]


def record_operation_failure(store, operation, exc, *, context=None):
    """Catch failures outside a journal; do not persist raw exception text."""
    value = {
        "schema_version": VERSION,
        "attempt_id": uuid.uuid4().hex,
        "window": None,
        "operation": operation,
        "findings": [
            _finding(
                "pipeline",
                getattr(exc, "code", "operation_contract_or_io"),
                operation,
                evidence={"exception_type": type(exc).__name__, "context": context or {}},
            )
        ],
    }
    return record_assessment(store, value)[0]


def supervised_records(store, dataset_id):
    """Consumer entry point: readiness plus outstanding human review gate."""
    result = supervise(store, dataset_id)
    if result["gate"] != "ready":
        raise IngestionError("supervision_gate", "Dataset requires incident review before use")
    return gold_records(store, dataset_id)


def report_html(store, result=None):
    state = snapshot(store)
    content = {
        "assessment": result,
        "incidents": list(state["incidents"].values()),
        "events": state["events"],
    }
    rows = "".join(
        "<tr>"
        + "".join(
            f"<td>{escape(str(v))}</td>"
            for v in (
                row["incident_id"][:20] + "…",
                row["finding"]["source_id"],
                row["finding"]["code"],
                row["finding"]["severity"],
                row["status"],
                row["occurrences"],
            )
        )
        + "</tr>"
        for row in state["incidents"].values()
    )
    active = sum(r["status"] != "resolved" for r in state["incidents"].values())
    gate = result.get("gate", "not assessed") if result else "not assessed"
    return (
        '<!doctype html><html lang="vi"><meta charset="utf-8"><title>HealthLab supervision</title>'
        "<style>td,th{padding:10px;text-align:left;border-bottom:1px solid #ddd}"
        "table{border-collapse:collapse;width:100%}body{color:#172b3a}</style>"
        '<body style="font:16px system-ui;max-width:1100px;margin:32px auto;padding:20px">'
        "<h1>HealthLab · Giám sát dữ liệu</h1>"
        f"<p>Gate: <strong>{escape(gate)}</strong> · Incident chưa đóng: <strong>{active}</strong></p>"
        "<p>Snapshot của sổ incident local. Xác nhận đã xem không gỡ lỗi dữ liệu. "
        "Ready chỉ áp dụng phạm vi đã capture; chưa là duyệt coverage hoặc biên tập.</p>"
        "<table><thead><tr><th>Mã</th><th>Nguồn</th><th>Vấn đề</th><th>Mức ảnh hưởng</th>"
        "<th>Trạng thái</th><th>Số quan sát</th></tr></thead>"
        f"<tbody>{rows or '<tr><td colspan=6>Chưa có incident được ghi nhận.</td></tr>'}</tbody></table>"
        "<details><summary>Bằng chứng và lịch sử xử lý</summary>"
        f'<pre style="white-space:pre-wrap">{escape(json.dumps(content, ensure_ascii=False, indent=2))}</pre>'
        "</details>"
        "</body></html>"
    )


def main(argv=None):
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=Path("outputs/data_20261026"))
    commands = parser.add_subparsers(dest="command", required=True)
    assess = commands.add_parser("assess")
    assess.add_argument("dataset_id")
    for command in ("assess", "incidents"):
        action = assess if command == "assess" else commands.add_parser(command)
        action.add_argument("--html", type=Path)
    for command in ("acknowledge", "resolve"):
        action = commands.add_parser(command)
        action.add_argument("incident_id")
        action.add_argument("--actor", required=True)
        action.add_argument("--note", required=True)
        if command == "resolve":
            action.add_argument("--evidence", required=True)
    contracts = commands.add_parser("sources")
    contracts.add_argument("--registry", type=Path, default=Path("config/signal_sources.json"))
    args = parser.parse_args(argv)
    try:
        store = RunStore(args.store)
        if args.command == "assess":
            result = supervise(store, args.dataset_id)
        elif args.command == "incidents":
            state = snapshot(store)
            result = {"incidents": list(state["incidents"].values()), "events": state["events"]}
        elif args.command == "sources":
            registry = Registry.model_validate_json(args.registry.read_bytes())
            result = {"sources": [source_contract(s) for s in registry.sources if s.enabled]}
        else:
            result = transition(
                store,
                args.incident_id,
                args.command,
                actor=args.actor,
                note=args.note,
                evidence=getattr(args, "evidence", None),
            )
        if getattr(args, "html", None):
            # Reports are explicit new snapshots, never overwrite historical evidence.
            with args.html.open("x", encoding="utf-8") as handle:
                handle.write(report_html(store, result))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("gate") in {"blocked", "needs_review"} else 0
    except (IngestionError, ValueError, OSError, KeyError, TypeError) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": getattr(exc, "code", "supervision_io_or_contract"),
                    "message": "Supervision failed; inspect stderr and saved evidence.",
                }
            )
        )
        logger.error("Supervision failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
