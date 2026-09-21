"""Frozen development-set runner and denominator-explicit human review metrics.

python -m healthlab.benchmark freeze|run|report --help
No model judges its own semantic correctness. Review labels start unjudged.
"""

import argparse
import hashlib
import json
import time
from pathlib import Path

from healthlab.config import FPTSettings, OpenRouterSettings, Settings
from healthlab.extraction import extract_run
from healthlab.models import IngestionError, Question
from healthlab.openrouter import OpenRouterProvider
from healthlab.orchestration import contract, metrics, verify_objects
from healthlab.pipeline import ingest
from healthlab.provider import FPTProvider
from healthlab.pubmed import PubMedClient, utc_now
from healthlab.store import RunStore, json_bytes
from healthlab.synthesis import synthesize_run, write_preview

FIELDS = (
    "study_type",
    "population",
    "sample_sizes",
    "main_finding",
    "supporting_passage",
    "author_limitations",
)


def save(path, value):
    write_preview(Path(path), json_bytes(value).decode())


def read(path):
    return json.loads(Path(path).read_text())


def freeze(suite_path, destination, store, client):
    suite = read(suite_path)
    ids = [c["id"] for c in suite["cases"]]
    if suite["k"] != 5 or len(ids) != len(set(ids)):
        raise ValueError("Expected k=5 and unique case IDs")
    destination.mkdir(parents=True, exist_ok=False)
    frozen = {
        "version": 1,
        "created_at": utc_now(),
        "suite": suite,
        "suite_sha256": store.put(json_bytes(suite)),
        "cases": [],
    }
    for case in suite["cases"]:
        start = time.monotonic()
        result = ingest(
            store,
            Question(
                raw_question=case["question"],
                query=case["query"],
                scope=case["scope"],
                sort=case["sort"],
            ),
            client,
        )
        frozen["cases"].append(
            {
                "case_id": case["id"],
                "ingestion_run_id": result["run_id"],
                "manifest_sha256": store.put(json_bytes(result)),
                "status": result["status"],
                "elapsed_seconds": time.monotonic() - start,
            }
        )
        save(destination / "freeze-progress.json", frozen)
        print(case["id"], result["status"], result.get("counts"), flush=True)
    digest = store.put(json_bytes(frozen))
    save(destination / "frozen.json", {"frozen_sha256": digest})
    return digest


def load_frozen(store, path):
    pointer = read(path)
    frozen = json.loads(store.read(pointer["frozen_sha256"]))
    if json_bytes(frozen["suite"]) != store.read(frozen["suite_sha256"]):
        raise IngestionError("benchmark_identity", "Suite identity changed")
    for case in frozen["cases"]:
        child = store.load(case["ingestion_run_id"])
        if json_bytes(child) != store.read(case["manifest_sha256"]):
            raise IngestionError("benchmark_identity", "Frozen ingestion manifest changed")
        verify_objects(store, child)
    return frozen


def run_baseline(store, frozen_path, destination, provider):
    frozen = load_frozen(store, frozen_path)
    identity = {
        "frozen_sha256": read(frozen_path)["frozen_sha256"],
        "contract": contract(provider),
        "benchmark_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "baseline.json"
    baseline = (
        read(path)
        if path.exists()
        else {"identity": identity, "created_at": utc_now(), "cases": []}
    )
    if baseline["identity"] != identity:
        raise IngestionError(
            "benchmark_identity", "Use a new destination for changed inputs/code/model"
        )
    finished = {c["case_id"] for c in baseline["cases"]}
    for case in frozen["cases"]:
        if case["case_id"] in finished:
            continue
        row = {
            "case_id": case["case_id"],
            "ingestion_run_id": case["ingestion_run_id"],
            "stages": {},
        }
        if case["status"] not in {"completed", "partial"}:
            row["status"] = "blocked_ingestion"
        else:
            for name, operation, parent in [
                ("extraction", extract_run, case["ingestion_run_id"]),
                ("synthesis", synthesize_run, None),
            ]:
                start = time.monotonic()
                if name == "synthesis":
                    parent = row["stages"]["extraction"]["run_id"]
                child = operation(store, parent, provider)
                row["stages"][name] = {
                    "run_id": child["run_id"],
                    "status": child["status"],
                    "manifest_sha256": store.put(json_bytes(child)),
                    "elapsed_seconds": time.monotonic() - start,
                    **metrics(child),
                }
                save(destination / "active-case.json", row)
            row["status"] = row["stages"]["synthesis"]["status"]
        baseline["cases"].append(row)
        save(path, baseline)
        print(case["case_id"], row["status"], flush=True)
    return baseline


def ratio(numerator, denominator):
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def retrieval_metrics(rows, k=5):
    labels = [r["label"] for r in rows]
    if len(rows) > k or any(
        v not in {None, "direct", "indirect", "irrelevant", "uncertain"} for v in labels
    ):
        raise ValueError("Invalid retrieval labels or candidate count")
    judged = sum(v in {"direct", "indirect", "irrelevant"} for v in labels)
    direct = labels.count("direct")
    return {
        "returned": len(rows),
        "judgment_coverage": ratio(judged, len(rows)),
        "precision_at_5": ratio(direct, k) if judged == len(rows) else None,
        "precision_among_judged": ratio(direct, judged),
        "direct_precision_bounds_at_5": [direct / k, (direct + len(rows) - judged) / k],
        "recall_at_5": None,
        "recall_note": "No independently judged relevant corpus; recall cannot be estimated",
    }


def score_reviews(rows):
    accepted = {"correct", "incorrect", "missing", "not_assessable"}
    if any(r["label"] not in accepted | {None} for r in rows):
        raise ValueError("Invalid factual review label")
    assessed = [r for r in rows if r["label"] in accepted - {"not_assessable"}]
    return {
        "reviewed": ratio(sum(r["label"] is not None for r in rows), len(rows)),
        "correct_among_assessable": ratio(
            sum(r["label"] == "correct" for r in assessed), len(assessed)
        ),
        "not_assessable": sum(r["label"] == "not_assessable" for r in rows),
        "critical_errors": sum(
            r.get("severity") == "critical" and r["label"] in {"incorrect", "missing"} for r in rows
        ),
    }


def report(store, frozen_path, baseline_path, destination):
    frozen = load_frozen(store, frozen_path)
    baseline = read(baseline_path)
    if baseline["identity"]["frozen_sha256"] != read(frozen_path)["frozen_sha256"]:
        raise IngestionError("benchmark_identity", "Baseline belongs to a different frozen set")
    destination.mkdir(parents=True, exist_ok=True)
    retrieval, fields, claims, cases = [], [], [], []
    for row in baseline["cases"]:
        case_id = row["case_id"]
        ing = store.load(row["ingestion_run_id"])
        packet = {
            "case": next(c for c in frozen["suite"]["cases"] if c["id"] == case_id),
            "documents": [],
            "claims": [],
        }
        by_pmid = {d["pmid"]: d for d in ing["documents"]}
        for rank, pmid in enumerate(ing.get("selection", {}).get("selected_pmids", []), 1):
            ref = by_pmid.get(pmid, {"pmid": pmid, "snapshot_id": None})
            snapshot = (
                json.loads(store.read(ref["snapshot_id"]))
                if ref["snapshot_id"]
                else {"pmid": pmid, "source_missing": True}
            )
            retrieval.append(
                {
                    "unit_id": f"{case_id}:{ref['pmid']}:{ref['snapshot_id']}",
                    "case_id": case_id,
                    "rank": rank,
                    "pmid": ref["pmid"],
                    "snapshot_id": ref["snapshot_id"],
                    "label": None,
                    "reviewer": None,
                    "reviewer_type": None,
                    "rationale": None,
                }
            )
            packet["documents"].append(snapshot)
        summary = {
            "case_id": case_id,
            "status": row["status"],
            "ingestion_counts": ing.get("counts"),
            "ingestion_errors": ing["errors"],
            "stages": row["stages"],
        }
        for name, stage in row["stages"].items():
            child = store.load(stage["run_id"])
            if json_bytes(child) != store.read(stage["manifest_sha256"]):
                raise IngestionError("benchmark_identity", "Baseline child changed")
            summary[name + "_counts"] = child.get("counts")
            summary[name + "_errors"] = child["errors"]
            if name == "extraction":
                packet["extraction"] = []
                for entry in child["results"]:
                    if entry.get("evidence_item_id"):
                        evidence = json.loads(store.read(entry["evidence_item_id"]))
                    else:
                        evidence = {
                            "snapshot_id": entry["snapshot_id"],
                            "status": entry["status"],
                            "error": entry.get("error"),
                            "raw_content": None,
                        }
                        if entry.get("content_sha256"):
                            evidence["raw_content"] = store.read(entry["content_sha256"]).decode()
                    packet["extraction"].append(evidence)
                    for field in FIELDS:
                        fields.append(
                            {
                                "unit_id": f"{case_id}:{child['run_id']}:{entry['snapshot_id']}:{field}",
                                "case_id": case_id,
                                "evidence_item_id": entry.get("evidence_item_id"),
                                "snapshot_id": entry["snapshot_id"],
                                "extraction_run_id": child["run_id"],
                                "machine_status": entry["status"],
                                "field": field,
                                "label": None,
                                "severity": None,
                                "reviewer": None,
                                "reviewer_type": None,
                                "rationale": None,
                            }
                        )
            if name == "synthesis" and child.get("content_sha256"):
                # Include rejected but parseable model claims for review, not only successful briefs.
                try:
                    payload = json.loads(store.read(child["content_sha256"]))
                    packet["claims"] = payload.get("claims", [])
                except (ValueError, AttributeError):
                    packet["claims"] = []
                for claim in packet["claims"]:
                    claims.append(
                        {
                            "unit_id": f"{case_id}:{child['run_id']}:{claim['claim_id']}",
                            "case_id": case_id,
                            "claim_id": claim["claim_id"],
                            "synthesis_run_id": child["run_id"],
                            "label": None,
                            "severity": None,
                            "reviewer": None,
                            "reviewer_type": None,
                            "rationale": None,
                        }
                    )
        cases.append(summary)
        save(destination / f"{case_id}-packet.json", packet)
    templates = {"retrieval": retrieval, "fields": fields, "claims": claims}
    # Preserve prior human annotations; reject IDs from a different run.
    review_path = destination / "reviews.json"
    if review_path.exists():
        reviews = read(review_path)
        for name, rows in templates.items():
            if [r["unit_id"] for r in reviews[name]] != [r["unit_id"] for r in rows]:
                raise ValueError("Review units differ; use another report directory")
            mutable = {"label", "severity", "reviewer", "reviewer_type", "rationale"}
            for saved, expected in zip(reviews[name], rows, strict=True):
                if {k: v for k, v in saved.items() if k not in mutable} != {
                    k: v for k, v in expected.items() if k not in mutable
                }:
                    raise ValueError("Review unit metadata was altered")
            for r in reviews[name]:
                if r["label"] is not None and (
                    r.get("reviewer_type") != "human"
                    or not r.get("reviewer")
                    or not r.get("rationale")
                ):
                    raise ValueError(
                        "Scored labels require identified human reviewer and rationale"
                    )
    else:
        reviews = templates
        save(review_path, reviews)
    scores = {
        "cases": cases,
        "factual_fields": score_reviews(reviews["fields"]),
        "claims": score_reviews(reviews["claims"]),
        "retrieval": {},
        "factual_by_field": {
            field: score_reviews([r for r in reviews["fields"] if r["field"] == field])
            for field in FIELDS
        },
    }
    for case in cases:
        if case["status"] == "blocked_ingestion":
            scores["retrieval"][case["case_id"]] = {"status": "source_failed_not_zero_relevance"}
        else:
            scores["retrieval"][case["case_id"]] = retrieval_metrics(
                [r for r in reviews["retrieval"] if r["case_id"] == case["case_id"]]
            )
    scores["brief_completed"] = ratio(sum(c["status"] == "completed" for c in cases), len(cases))
    scores["warning"] = (
        "Development examples; unjudged is not incorrect. Machine validation is not factual correctness. No held-out quality claim."
    )
    save(destination / "metrics.json", scores)
    return scores


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["freeze", "run", "report"])
    parser.add_argument("--provider", choices=["fpt", "openrouter"], default="fpt")
    parser.add_argument("--store", type=Path, default=Path("outputs"))
    parser.add_argument("--suite", type=Path, default=Path("benchmarks/questions_v0.1.json"))
    parser.add_argument("--frozen", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    store = RunStore(args.store)
    if args.command == "freeze":
        client = PubMedClient(Settings())
        try:
            freeze(args.suite, args.out, store, client)
        finally:
            client.close()
    elif args.command == "run":
        provider = (
            OpenRouterProvider(OpenRouterSettings(), lazy=True)
            if args.provider == "openrouter"
            else FPTProvider(FPTSettings(), lazy=True)
        )
        try:
            run_baseline(store, args.frozen, args.out, provider)
        finally:
            provider.close()
    else:
        report(store, args.frozen, args.baseline, args.out)


if __name__ == "__main__":
    main()
