"""Select and persist review, scope and draft jobs within the run budget."""

import json

from healthlab.data.pilot import runner_lock
from healthlab.editorial.packet import evidence_packet
from healthlab.editorial.runs import allocated_calls, jobs, load
from healthlab.store import json_bytes


def plan_review(store, run_id, *, excluded_sample=16, all_articles=False):
    with runner_lock(store.root):
        run, records = load(store, run_id, for_inference=True)
        if "review" in run["prepared_review_roles"]:
            return run
        if any(j["role"] == "classify" and j["status"] == "prepared" for j in jobs(store, run)):
            raise ValueError("Finish attempting all articles before planning review")
        relevant, excluded = [], []
        for job in jobs(store, run):
            if job["role"] != "classify" or job["status"] != "completed":
                continue
            p = json.loads(store.read(job["result_sha256"]))
            (excluded if p["relevance"] == "irrelevant" else relevant).append(job)
        # Deterministic sample; negatives are checked too, never called human gold labels.
        slots = run["call_budget"] - allocated_calls(run, jobs(store, run)) - 1
        if slots < 1:
            raise ValueError("No review budget remains")
        if all_articles and slots < len(relevant) + len(excluded):
            raise ValueError("Budget cannot cover full review and one reserved draft")
        negative_count = min(excluded_sample, max(0, slots // 2), len(excluded))
        relevant.sort(
            key=lambda j: (
                not json.loads(store.read(j["result_sha256"]))["newsletter_candidate"],
                j["document_id"],
            )
        )
        selected = (
            relevant[: max(0, slots - negative_count)]
            + sorted(excluded, key=lambda j: j["document_id"])[:negative_count]
        )
        if all_articles:
            selected = sorted(relevant + excluded, key=lambda j: j["document_id"])
        run["review_scope"] = {
            "relevant_available": len(relevant),
            "relevant_selected": len(relevant)
            if all_articles
            else min(len(relevant), max(0, slots - negative_count)),
            "irrelevant_sample": len(excluded) if all_articles else negative_count,
            "selection": "all valid proposals, stable document ID; failed classifications remain unresolved"
            if all_articles
            else "dated newsletter candidates first, then stable document ID; bounded sample, not full accuracy evaluation",
            "selected_document_ids": [j["document_id"] for j in selected],
            "all_articles": all_articles,
        }
        for original in selected:
            job = store.new("editorial_call", run["question"])
            job.update(
                role="review",
                document_id=original["document_id"],
                status="prepared",
                proposal_sha256=original["result_sha256"],
            )
            store.save(job)
            run["jobs"].append(job["run_id"])
        run["prepared_review_roles"].append("review")
        store.save(run)
        return run


def plan_scope(store, run_id, *, all_articles=False):
    """Plan independent full-source scope checks within the same lifetime budget."""
    with runner_lock(store.root):
        run, records = load(store, run_id, for_inference=True)
        if "scope" in run["prepared_review_roles"]:
            return run
        calls = jobs(store, run)
        if any(j["role"] == "classify" and j["status"] == "prepared" for j in calls):
            raise ValueError("Attempt classifications before planning scope assessment")
        selected = (
            sorted(records)
            if all_articles
            else sorted(
                j["document_id"]
                for j in calls
                if j["role"] == "classify"
                and j["status"] == "completed"
                and json.loads(store.read(j["result_sha256"]))["relevance"] != "irrelevant"
            )
        )
        if allocated_calls(run, calls) + len(selected) + 1 > run["call_budget"]:
            raise ValueError("Budget cannot cover scope assessment and one reserved draft")
        for identifier in selected:
            job = store.new("editorial_call", run["question"])
            job.update(role="scope", document_id=identifier, status="prepared")
            store.save(job)
            run["jobs"].append(job["run_id"])
        run["scope_plan"] = {
            "all_articles": all_articles,
            "selected_document_ids": selected,
            "prior_proposal_supplied": False,
        }
        run["prepared_review_roles"].append("scope")
        store.save(run)
        return run


def plan_draft(store, run_id):
    with runner_lock(store.root):
        run, _ = load(store, run_id, for_inference=True)
        if run["draft_planned"]:
            return run
        packet = evidence_packet(store, run_id)
        if not packet["draft_evidence"]:
            return run  # No invented stories to fill a quota.
        if allocated_calls(run, jobs(store, run)) >= run["call_budget"]:
            raise ValueError("Lifetime call budget exhausted")
        job = store.new("editorial_call", run["question"])
        job.update(
            role="draft",
            status="prepared",
            evidence_sha256=store.put(json_bytes(packet["draft_evidence"])),
        )
        store.save(job)
        run["jobs"].append(job["run_id"])
        run["draft_planned"] = True
        store.save(run)
        return run
