"""Opt-in paired prompt experiment; production extraction and cache stay unchanged.

Run with python -m healthlab.sample_size_experiment --frozen PATH --out DIRECTORY.
All interpretation checks are engineering hypotheses, not human factual labels.
"""

import argparse
import hashlib
import json
import time
from pathlib import Path

from healthlab.benchmark import load_frozen, save
from healthlab.config import FPTSettings
from healthlab.evidence import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    VALIDATOR_VERSION,
    build_messages,
    parse_content,
    verify_passages,
)
from healthlab.models import Document, IngestionError
from healthlab.pipeline import audit_writer, finish
from healthlab.provider import FPTProvider
from healthlab.store import RunStore, json_bytes

CANDIDATE_VERSION = PROMPT_VERSION + "-sample-entity-experiment-1"
SAMPLE_RULE = (
    "Sample-size entity rule: sample_sizes counts HUMAN study participants only. "
    "Never put counts of articles, papers, studies, trials, recommendations, statements, "
    "sessions, measurements or percentages in sample_sizes. For a review, return [] unless "
    "an explicit human participant total or named human cohort count is stated. "
    "Experts/respondents in a Delphi or consensus study are human participants: retain "
    "explicitly reported counts but identify the expert panel and phase in group; do not "
    "present them as patients. Do not add counts together or subtract dropouts to derive "
    "an unstated total. Assign recruited/randomized/analyzed/completed only when the source "
    "explicitly supports that stage; otherwise use unspecified. A randomized count does "
    "not establish how many completed follow-up. Preserve distinct explicit human counts "
    "and their group/stage, and use [] when none is stated."
)

# Predeclared targeted development cases, not a random or held-out sample.
CASES = [
    {
        "pmid": "26626157",
        "category": "review_article_counts",
        "forbidden_values": [507, 9],
        "expect_empty": True,
    },
    {
        "pmid": "36349684",
        "category": "expert_panel",
        "forbidden_values": [67],
        "expected_values": [29, 62, 5, 47],
        "expert_groups": True,
    },
    {
        "pmid": "41036686",
        "category": "trial_stage_ambiguity",
        "expected_roles": [[19, "recruited"], [12, "randomized"]],
        "flag_roles": [[12, "completed"]],
    },
    {
        "pmid": "39024000",
        "category": "explicit_trial_stages",
        "expected_roles": [[96, "recruited"], [60, "randomized"], [55, "completed"]],
    },
    {"pmid": "39743381", "category": "no_explicit_human_count", "expect_empty": True},
]


def experiment_messages(document, variant):
    messages = build_messages(document)
    if variant == "candidate":
        messages[0]["content"] += " " + SAMPLE_RULE
    elif variant != "control":
        raise ValueError("Expected control or candidate")
    return messages


def inspect_samples(evidence, document, snapshot_id, case):
    """Report explicit diagnostic checks; never alter or accept model evidence."""
    values = [sample.value for sample in evidence.sample_sizes]
    roles = {(sample.value, sample.role) for sample in evidence.sample_sizes}
    # Isolate sample quotation validation from failures in unrelated fields.
    sample_only = evidence.model_copy(
        update={
            "main_finding": None,
            "supporting_passage": None,
            "section_index": None,
            "author_limitations": [],
        }
    )
    quote_error = None
    try:
        verify_passages(sample_only, document, snapshot_id, validator_version="passage-1")
    except IngestionError as exc:
        quote_error = {"code": exc.code, "message": str(exc)}
    return {
        "sample_quote_valid": quote_error is None,
        "sample_quote_error": quote_error,
        "values": values,
        "roles": [list(pair) for pair in sorted(roles)],
        "empty_as_expected": not values if case.get("expect_empty") else None,
        "forbidden_values_present": sorted(set(values) & set(case.get("forbidden_values", []))),
        "missing_expected_values": [n for n in case.get("expected_values", []) if n not in values],
        "missing_expected_roles": [
            p for p in case.get("expected_roles", []) if tuple(p) not in roles
        ],
        "ambiguous_roles_present": [p for p in case.get("flag_roles", []) if tuple(p) in roles],
        "expert_groups_present": all(
            sample.group
            and any(
                word in sample.group.lower()
                for word in ("expert", "panel", "task force", "respondent")
            )
            for sample in evidence.sample_sizes
        )
        if case.get("expert_groups") and values
        else None,
        "assessment_origin": "predeclared_engineering_checks_not_human_gold",
    }


def run_one(store, provider, document, snapshot_id, case, variant):
    messages = experiment_messages(document, variant)
    run = store.new("sample_size_experiment", {"pmid": case["pmid"], "variant": variant})
    run.update(
        snapshot_id=snapshot_id,
        provider=provider.identity(),
        prompt_version=PROMPT_VERSION if variant == "control" else CANDIDATE_VERSION,
        schema_version=SCHEMA_VERSION,
        validator_version=VALIDATOR_VERSION,
        stage="generation",
        input_sha256=store.put(json_bytes(messages)),
    )
    store.save(run)
    start = time.monotonic()
    try:
        completion = provider.complete(messages, audit_writer(store, run))
        run.update(
            content_sha256=store.put(completion.content.encode()),
            usage=completion.usage,
            actual_model=completion.actual_model,
            finish_reason=completion.finish_reason,
        )
        if completion.finish_reason != "stop":
            raise IngestionError("completion_incomplete", "Completion did not finish normally")
        evidence = parse_content(completion.content)
        run["schema_valid"] = True
        run["evidence"] = evidence.model_dump()
        run["sample_diagnostics"] = inspect_samples(evidence, document, snapshot_id, case)
        run["stage"] = "passage_validation"
        verify_passages(evidence, document, snapshot_id)
        run.update(status="completed", passage_valid=True)
    except IngestionError as exc:
        run.update(status="failed", errors=[{"code": exc.code, "message": str(exc)}])
    finally:
        run["elapsed_seconds"] = time.monotonic() - start
        finish(store, run)
    return run


def run_experiment(store, frozen_path, destination, provider):
    frozen = load_frozen(store, frozen_path)
    by_pmid = {}
    for row in frozen["cases"]:
        for ref in store.load(row["ingestion_run_id"])["documents"]:
            by_pmid.setdefault(ref["pmid"], ref)
    missing = [c["pmid"] for c in CASES if c["pmid"] not in by_pmid]
    if missing:
        raise ValueError(f"Frozen set lacks required PMIDs: {missing}")
    destination.mkdir(parents=True, exist_ok=False)
    experiment = {
        "version": 1,
        "frozen_sha256": json.loads(Path(frozen_path).read_text())["frozen_sha256"],
        "provider": provider.identity(),
        "control_prompt_version": PROMPT_VERSION,
        "candidate_prompt_version": CANDIDATE_VERSION,
        "candidate_instruction": SAMPLE_RULE,
        "experiment_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "extraction_code_sha256": hashlib.sha256(
            Path(__file__).with_name("evidence.py").read_bytes()
        ).hexdigest(),
        "cases": CASES,
        "results": [],
        "human_review_status": "unreviewed",
        "design": "paired five targeted frozen abstracts; alternating order; one fresh call per arm per case; no retries, no cache, no synthesis",
    }
    save(destination / "experiment.json", experiment)
    for index, case in enumerate(CASES):
        reference = by_pmid[case["pmid"]]
        snapshot_id = reference["snapshot_id"]
        snapshot = json.loads(store.read(snapshot_id))
        store.read(snapshot["raw_record_sha256"])
        document = Document.model_validate(snapshot["document"])
        variants = ("control", "candidate") if index % 2 == 0 else ("candidate", "control")
        for variant in variants:
            child = run_one(store, provider, document, snapshot_id, case, variant)
            experiment["results"].append(
                {
                    "pmid": case["pmid"],
                    "variant": variant,
                    "run_id": child["run_id"],
                    "status": child["status"],
                    "manifest_sha256": store.put(json_bytes(child)),
                }
            )
            save(destination / "experiment.json", experiment)
            print(
                case["pmid"], variant, child["status"], child.get("sample_diagnostics"), flush=True
            )
    return experiment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=Path("outputs"))
    parser.add_argument("--frozen", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    provider = FPTProvider(FPTSettings(), lazy=True)
    try:
        run_experiment(RunStore(args.store), args.frozen, args.out, provider)
    finally:
        provider.close()


if __name__ == "__main__":
    main()
