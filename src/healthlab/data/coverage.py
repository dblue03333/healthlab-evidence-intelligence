"""Offline coverage comparison against an explicitly scoped, evidenced reference.

This is a separate acceptance artifact; it never changes a dataset's membership
or converts a partial reference into a claim of website-wide recall.
"""

import argparse
import hashlib
import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from healthlab.common import JsonObject, configure_logging
from healthlab.data.datasets import read_dataset
from healthlab.signal.contracts import Registry
from healthlab.store import RunStore


def identity(source_id: str, url: str) -> str:
    parsed = urlsplit(url)
    if source_id == "moh_portal":
        return "aid:" + parse_qs(parsed.query)["aid"][0]
    if source_id == "hcm_health":
        match = re.search(r"-c(\d+)-(\d+)\.aspx$", parsed.path, re.I)
        if not match:
            raise ValueError("HCMC reference lacks native article identity")
        return "article:" + match[2]
    return parsed.path.rstrip("/")


def compare(store: RunStore, dataset_id: str, reference_path: Path) -> JsonObject:
    reference_path = Path(reference_path)
    reference = json.loads(reference_path.read_bytes())
    if reference["schema_version"] != "coverage-reference-1":
        raise ValueError("Unsupported reference")
    _, bronze, silver, gold = read_dataset(store, dataset_id)
    registry = {s.source_id: s for s in Registry.model_validate(bronze.registry).sources}
    sources = {q.source_id for q in silver.sources}
    refs = reference["sources"]
    if len(refs) != len(sources) or {r["source_id"] for r in refs} != sources:
        raise ValueError("Reference must explicitly cover every enabled source")
    results = []
    for ref in refs:
        source_id = ref["source_id"]
        source = registry[source_id]
        if ref["date_policy"] != source.resolved_html_rules.window_date_policy:
            raise ValueError("Reference and collection date policies differ")
        actual = defaultdict(list)
        for row in silver.records:
            if row.source_id == source_id:
                actual[identity(source_id, row.url)].append(row)
        expected = defaultdict(list)
        all_dates = defaultdict(set)
        verified = set()
        for row in ref["records"]:
            if urlsplit(row["url"]).hostname != urlsplit(source.url).hostname:
                raise ValueError("Reference article belongs to another source host")
            raw_path = (reference_path.parent / row["raw_file"]).resolve()
            if not raw_path.is_relative_to(reference_path.parent.resolve()):
                raise ValueError("Reference evidence must be inside the audit directory")
            evidence_key = (str(raw_path), row["raw_sha256"])
            if evidence_key not in verified:
                if hashlib.sha256(raw_path.read_bytes()).hexdigest() != row["raw_sha256"]:
                    raise ValueError("Reference evidence hash mismatch")
                verified.add(evidence_key)
            if not row["locator"] or not row["url"]:
                raise ValueError("Reference evidence requires a locator and article URL")
            day = date.fromisoformat(row["date"])
            all_dates[identity(source_id, row["url"])].add(row["date"])
            if gold.window.mindate <= day <= gold.window.maxdate:
                expected[identity(source_id, row["url"])].append(row)
        missing, unread, mismatched, matched = [], [], [], []
        conflicts = []
        for key, rows in sorted(expected.items()):
            if len(all_dates[key]) != 1:
                conflicts.append(key)
                continue
            if key not in actual:
                missing.append({"identity": key, "url": rows[0]["url"], "date": rows[0]["date"]})
                continue
            included = [r for r in actual[key] if r.outcome == "included"]
            if not included:
                unread.append(
                    {"identity": key, "outcomes": sorted({r.outcome for r in actual[key]})}
                )
            elif any(
                str(getattr(r, "selection_date", r.publication_date)) != rows[0]["date"]
                for r in included
            ):
                mismatched.append(key)
            else:
                matched.append(key)
        selected = {
            key for key, rows in actual.items() if any(r.outcome == "included" for r in rows)
        }
        extra = sorted(selected - expected.keys())
        duplicates = sorted(key for key, rows in actual.items() if len(rows) > 1)
        reference_complete = (
            ref["complete"] is True
            and not ref["limitations"]
            and not conflicts
            and date.fromisoformat(ref["mindate"]) <= gold.window.mindate
            and date.fromisoformat(ref["maxdate"]) >= gold.window.maxdate
        )
        differences = bool(missing or unread or mismatched or extra or duplicates or conflicts)
        results.append(
            {
                "source_id": source_id,
                "method": ref["method"],
                "independence": ref["independence"],
                "scope": ref["scope"],
                "reference_complete": reference_complete,
                "limitations": ref["limitations"],
                "expected": len(expected),
                "matched": len(matched),
                "missing_from_discovery": missing,
                "discovered_but_not_included": unread,
                "date_mismatch": mismatched,
                "not_in_reference": extra,
                "duplicate_identities": duplicates,
                "reference_date_conflicts": conflicts,
                "reference_recall": len(matched) / len(expected)
                if reference_complete and expected
                else None,
                "status": "differences"
                if differences
                else "matched"
                if reference_complete
                else "limited_reference",
            }
        )
    return {
        "schema_version": "coverage-comparison-1",
        "dataset_id": dataset_id,
        "collection_id": bronze.collection_id,
        "window": gold.window.model_dump(mode="json"),
        "reference_sha256": hashlib.sha256(reference_path.read_bytes()).hexdigest(),
        "website_recall": None,
        "status": "matched_reference"
        if all(r["status"] == "matched" for r in results)
        else "needs_review",
        "sources": results,
    }


def main(argv=None):
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = compare(RunStore(args.store), args.dataset, args.reference)
    # Never overwrite a previous acceptance record.
    with args.out.open("x") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
    print(json.dumps({"status": result["status"], "report": str(args.out)}))
    return 0 if result["status"] == "matched_reference" else 1


if __name__ == "__main__":
    raise SystemExit(main())
