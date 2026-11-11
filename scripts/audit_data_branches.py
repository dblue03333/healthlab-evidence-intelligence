"""Offline branch diagnostics with explicit provenance gaps; never auto-certifies coverage."""

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import date
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from healthlab.data.coverage import compare, identity
from healthlab.data.datasets import read_dataset
from healthlab.signal.contracts import Registry
from healthlab.signal.history import source_scope
from healthlab.store import RunStore, json_bytes


def branch_for_page(page, source_id, branches):
    if page.get("branch_id"):
        return page["branch_id"]
    if page.get("category_id"):
        return "moh_" + page["category_id"]
    path = urlsplit(page["url"]).path.rstrip("/")
    for branch in branches:
        if branch["source_id"] != source_id:
            continue
        seed = urlsplit(branch["seed_url"]).path.rstrip("/")
        if branch["branch_id"] == "hcm_c1780":
            if path.startswith("/tin-tuc-su-kien-c1780"):
                return branch["branch_id"]
        elif source_id == "who_ageing":
            base = seed.rsplit("/", 1)[0] if seed.rsplit("/", 1)[-1].isdigit() else seed
            if path == base or path == seed or path.startswith(base + "/"):
                return branch["branch_id"]
    return None


def audit(store, dataset_id, matrix_path, reference_path):
    matrix_path, reference_path = Path(matrix_path), Path(reference_path)
    matrix = json.loads(matrix_path.read_bytes())
    reference = json.loads(reference_path.read_bytes())
    # Reuse strict reference hashes, policy checks and frozen dataset lineage.
    source_report = compare(store, dataset_id, reference_path)
    _, bronze, silver, gold = read_dataset(store, dataset_id)
    registry = Registry.model_validate(bronze.registry)
    scope = [source_scope(s) for s in registry.sources if s.enabled]
    if hashlib.sha256(json_bytes(scope)).hexdigest() != matrix["source_scope_sha256"]:
        raise ValueError("Branch plan and dataset membership scopes differ")
    collection = json.loads(store.read(bronze.collection_manifest_sha256))
    branches = matrix["branches"]
    if len({b["branch_id"] for b in branches}) != len(branches):
        raise ValueError("Duplicate branch identity")
    expected_branches = {
        "moh_" + c
        for s in registry.sources
        if s.enabled and s.html_discovery
        for c in s.html_discovery.moh_portal_categories
    }
    expected_branches |= {
        "hcm_c1780",
        "who_ageing",
        "who_releases",
        "who_feature_stories",
        "who_commentaries",
        "who_speeches",
    }
    if {b["branch_id"] for b in branches} != expected_branches:
        raise ValueError("Plan must cover all twelve configured branches")
    actual = defaultdict(lambda: defaultdict(list))
    for row in silver.records:
        actual[row.source_id][identity(row.source_id, row.url)].append(row)
    members, pages = defaultdict(set), defaultdict(list)
    unmapped = []
    for check in collection["source_checks"]:
        for page in check.get("listing_pages", []):
            branch = branch_for_page(page, check["source_id"], branches)
            if branch not in expected_branches:
                unmapped.append({"source_id": check["source_id"], "url": page["url"]})
                continue
            pages[branch].append(
                {
                    k: page[k]
                    for k in [
                        "url",
                        "status",
                        "raw_sha256",
                        "stop_reason",
                        "total_posts",
                        "returned_count",
                        "publication_range",
                    ]
                    if k in page
                }
            )
            members[branch].update(
                identity(check["source_id"], u) for u in page.get("candidate_urls", [])
            )
    refs = {r["source_id"]: r for r in reference["sources"]}
    output = []
    for branch in branches:
        key, source_id = branch["branch_id"], branch["source_id"]
        expected = defaultdict(set)
        evidence = defaultdict(list)
        for row in refs[source_id]["records"]:
            if row["raw_file"] in branch["reference_files"]:
                native = identity(source_id, row["url"])
                expected[native].add(row["date"])
                evidence[native].append(
                    {k: row[k] for k in ["url", "date", "raw_file", "raw_sha256", "locator"]}
                )
        expected = {
            k: values
            for k, values in expected.items()
            if any(
                gold.window.mindate <= date.fromisoformat(v) <= gold.window.maxdate for v in values
            )
        }
        matched, missing, unread, mismatched, conflicts, unproven = [], [], [], [], [], []
        for native, days in sorted(expected.items()):
            if len(days) != 1:
                conflicts.append(native)
                continue
            rows = actual[source_id].get(native, [])
            if not rows:
                missing.append(native)
                continue
            if native not in members[key]:
                unproven.append(native)
            selected = [r for r in rows if r.outcome == "included"]
            if not selected:
                unread.append({"identity": native, "outcomes": sorted({r.outcome for r in rows})})
            elif any(
                str(getattr(r, "selection_date", r.publication_date)) not in days for r in selected
            ):
                mismatched.append(native)
            else:
                matched.append(native)
        selected_members = {
            k
            for k in members[key]
            if any(r.outcome == "included" for r in actual[source_id].get(k, []))
        }
        extra = sorted(selected_members - expected.keys())
        duplicates = sorted(
            k for k, rows in actual[source_id].items() if k in members[key] and len(rows) > 1
        )
        differences = bool(missing or unread or mismatched or conflicts or extra or duplicates)
        output.append(
            {
                "branch_id": key,
                "source_id": source_id,
                "name": branch["name"],
                "date_policy": branch["date_policy"],
                "expected": len(expected),
                "matched_in_source_gold": len(matched),
                "missing_from_source_discovery": missing,
                "unread": unread,
                "date_mismatch": mismatched,
                "reference_date_conflicts": conflicts,
                "not_in_reference_via_branch": extra,
                "duplicate_native_identities": duplicates,
                "branch_discovery_provenance_unproven": unproven,
                "listing_pages": pages[key],
                "reference_evidence": {native: evidence[native] for native in expected},
                "reference_complete": False,
                "reviewer_signoff": None,
                "status": "differences" if differences else "limited_reference",
                "reference_recall": None,
                "limitations": refs[source_id]["limitations"]
                + [
                    "Branch inventory closure and reference semantics require review; this diagnostic never flips complete or certifies coverage."
                ],
            }
        )
    return {
        "schema_version": "healthlab-branch-diagnostic-1",
        "dataset_id": dataset_id,
        "window": gold.window.model_dump(mode="json"),
        "processing_status": gold.status,
        "status": "needs_review",
        "coverage_accepted": False,
        "website_recall": None,
        "registry_sha256_canonical": hashlib.sha256(json_bytes(bronze.registry)).hexdigest(),
        "reference_sha256": source_report["reference_sha256"],
        "matrix_sha256": hashlib.sha256(matrix_path.read_bytes()).hexdigest(),
        "branch_counts_non_additive": True,
        "unmapped_listing_pages": unmapped,
        "branches": output,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    # Atomic input verification happens before creating any report output.
    result = audit(RunStore(args.store), args.dataset, args.matrix, args.reference)
    with args.out.open("x") as out:
        json.dump(result, out, ensure_ascii=False, indent=2)
        out.write("\n")
    rows = "".join(
        f"<tr><td>{escape(r['branch_id'])}</td><td>{r['expected']}</td><td>{r['matched_in_source_gold']}</td>"
        f"<td>{len(r['missing_from_source_discovery'])}</td><td>{len(r['unread'])}</td><td>{escape(r['status'])}</td></tr>"
        for r in result["branches"]
    )
    with args.out.with_suffix(".html").open("x") as out:
        out.write(
            '<!doctype html><meta charset="utf-8"><title>Branch diagnostics</title><h1>Branch diagnostics</h1>'
            "<p>Coverage remains unaccepted; counts are non-additive. Reference integrity does not prove completeness.</p>"
            '<table border="1"><tr><th>Branch</th><th>Expected</th><th>Matched</th><th>Missing</th><th>Unread</th><th>Status</th></tr>'
            + rows
            + "</table><pre>"
            + escape(json.dumps(result, ensure_ascii=False, indent=2))
            + "</pre>"
        )
    print(
        json.dumps(
            {"status": result["status"], "branches": len(result["branches"]), "out": str(args.out)}
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
