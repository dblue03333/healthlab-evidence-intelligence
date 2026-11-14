"""Build the draft evidence packet from source-bound proposals and review decisions."""

import json

from healthlab.editorial.contracts import (
    CONTEXT_CONTRACTS,
    ArticleProposal,
    canonical_proposal,
    passage_inputs,
)
from healthlab.editorial.review_policy import (
    NO_REVIEW_POLICIES,
    PUBLICATION_WINDOW_POLICY,
    review_gate,
    topic_gate,
)
from healthlab.editorial.runs import jobs, load
from healthlab.editorial.scope import scope_gate


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
        topic_only = p.get("release_policy") in NO_REVIEW_POLICIES
        gate = (
            topic_gate(p, run["question"])
            if topic_only
            else review_gate(p, review, [u["passage_id"] for u in units], run["question"])
        )
        scope = scopes.get(job["document_id"])
        scope_result = scope_gate(scope, units) if requires_scope_assessment(run) else None
        if scope_result:
            gate = dict(
                gate,
                eligible=gate["eligible"] and scope_result["eligible"],
                reasons=gate["reasons"] + scope_result["reasons"],
            )
        eligible = gate["eligible"]
        articles.append(
            {
                "document_id": row["document_id"],
                "title": row["title"],
                "url": row["url"],
                "proposal": p,
                "review": review,
                "scope_assessment": scope,
                "scope_gate": scope_result,
                "eligible_for_unapproved_draft": eligible,
                "review_gate": gate,
            }
        )
        if eligible:
            # topic-only-1 passes every source-bound fact; the approver checks them against
            # the source. fact-flags-1 passes every undropped fact with its reviewer flags;
            # earlier policies pass only reviewer-supported facts.
            flagged = (
                {f["fact_index"]: f["flags"] for f in gate["facts"] if f["released"]}
                if "facts" in gate and not topic_only
                else None
            )
            # publication-window-1 also records how the writer must date each fact.
            bases = (
                {f["fact_index"]: f["time_basis"] for f in gate["facts"]}
                if gate.get("release_policy") == PUBLICATION_WINDOW_POLICY
                else None
            )
            if topic_only:
                indexes = [f["fact_index"] for f in gate["facts"] if f["released"]]
            else:
                indexes = (
                    sorted(flagged) if flagged is not None else review["supported_fact_indexes"]
                )
            for index in indexes:
                f = p["resolved_facts"][index]
                if (
                    not topic_only
                    and f.get("date_evidence")
                    and index not in review.get("supported_date_fact_indexes", [])
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
                        **({"review_flags": flagged[index]} if flagged is not None else {}),
                        **(
                            {
                                "time_basis": bases[index],
                                "publication_date": row["publication_date"],
                            }
                            if bases is not None
                            else {}
                        ),
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
