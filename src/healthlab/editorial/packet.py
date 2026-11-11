"""Build the draft evidence packet from source-bound proposals and review decisions."""

import json

from healthlab.editorial.contracts import (
    CONTEXT_CONTRACTS,
    ArticleProposal,
    canonical_proposal,
    passage_inputs,
)
from healthlab.editorial.runs import jobs, load
from healthlab.editorial_review_policy import review_gate
from healthlab.editorial_scope import scope_gate


def requires_scope_assessment(run):
    return run["version"] not in {f"gold-editorial-{i}" for i in range(1, 8)}


def evidence_packet(store, run_id):
    run, records = load(store, run_id)
    calls = jobs(store, run)
    reviews = {
        j["document_id"]: json.loads(store.read(j["result_sha256"]))
        for j in calls
        if j["role"] == "review" and j["status"] == "completed"
    }
    scopes = {
        j["document_id"]: json.loads(store.read(j["result_sha256"]))
        for j in calls
        if j["role"] == "scope" and j["status"] == "completed"
    }
    evidence, articles = [], []
    for job in calls:
        if job["role"] != "classify" or job["status"] != "completed":
            continue
        row = records[job["document_id"]]
        saved = json.loads(store.read(job["result_sha256"]))
        repaired = CONTEXT_CONTRACTS.get(run["version"]) == "fact-context-2"
        fields = [*ArticleProposal.model_fields, *(["binding_repairs"] if repaired else [])]
        p = canonical_proposal(
            {k: saved[k] for k in fields},
            row,
            run["question"],
            version=run["version"],
        )
        review = reviews.get(job["document_id"])
        units, _ = passage_inputs(row, version=run["version"])
        gate = review_gate(p, review, [u["passage_id"] for u in units], run["question"])
        scope = scopes.get(job["document_id"])
        scope_result = scope_gate(scope, units) if requires_scope_assessment(run) else None
        if scope_result:
            gate = {
                "eligible": gate["eligible"] and scope_result["eligible"],
                "reasons": gate["reasons"] + scope_result["reasons"],
            }
        eligible = gate["eligible"]
        articles.append(
            dict(
                document_id=row["document_id"],
                title=row["title"],
                url=row["url"],
                proposal=p,
                review=review,
                scope_assessment=scope,
                scope_gate=scope_result,
                eligible_for_unapproved_draft=eligible,
                review_gate=gate,
            )
        )
        if eligible:
            for index in review["supported_fact_indexes"]:
                f = p["resolved_facts"][index]
                if f.get("date_evidence") and index not in review.get(
                    "supported_date_fact_indexes", []
                ):
                    continue  # Never pass an unreviewed cross-passage date to the writer.
                if row["body"][f["start"] : f["end"]] != f["quote"]:
                    raise ValueError("Evidence offsets differ from frozen body")
                evidence.append(
                    dict(
                        evidence_id=f"E{len(evidence) + 1}",
                        document_id=row["document_id"],
                        snapshot_id=row["snapshot_id"],
                        url=row["url"],
                        title=row["title"],
                        raw_sha256=row["raw_sha256"],
                        **f,
                    )
                )
    return {
        "dataset_id": run["dataset_id"],
        "window": run["question"],
        "articles": articles,
        "draft_evidence": evidence,
        "human_approval": False,
        "semantic_accuracy": "not_measured_against_human_labels",
    }
