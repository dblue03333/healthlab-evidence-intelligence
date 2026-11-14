"""Audited candidate memory. Historical listings are evidence, not fresh discovery."""

import json
from datetime import date

from healthlab.common import JsonObject
from healthlab.signal.contracts import Registry, Source
from healthlab.signal.storage import CollectionStorage
from healthlab.store import json_bytes


def source_scope(source: Source) -> JsonObject:
    """Operational budgets may change without forgetting the same source scope."""
    value = source.model_dump()
    for key in (
        "max_documents",
        "max_article_fetches",
        "html_max_response_bytes",
        "required",
        "usage_policy",
    ):
        value.pop(key)
    if value.get("html_discovery"):
        for key in ("max_listing_pages", "api_page_size", "portal_inventory_size"):
            value["html_discovery"].pop(key, None)
        # This controls only when pagination may stop, not candidate membership.
        value["html_discovery"].pop("chronological_scope", None)
        # A supplemental inventory for the same already-accepted speech URLs
        # changes discovery completeness, not document membership scope.
        value["html_discovery"].pop("who_speeches_api", None)
        value["html_discovery"].pop("who_speeches_max_pages", None)
        value["html_discovery"].pop("who_ageing_widget", None)
        value["html_discovery"].pop("hcm_search_inventory", None)
    return value


def candidate_history(
    storage: CollectionStorage, registry: Registry, mindate: date, maxdate: date
) -> JsonObject:
    """Called under the collection lock; capture compatible finished observations.

    Older windows remain available for later backfills. Unknown dates in an
    overlapping window are retained, never converted into an editorial exclude.
    """
    sources = {s.source_id: s for s in registry.sources if s.enabled and s.kind == "html"}
    found: dict[str, JsonObject] = {key: {} for key in sources}
    latest: dict[str, JsonObject] = {key: {} for key in sources}
    runs = [storage.load(i) for i in storage.state.list_runs("signal_collection")]
    for run in sorted(
        runs, key=lambda r: (r.get("observed_at", r["created_at"]), r["created_at"], r["run_id"])
    ):
        if run["status"] == "running":
            continue
        window = run["question"]
        if window["maxdate"] < str(mindate) or window["mindate"] > str(maxdate):
            continue
        old_sources = {s.source_id: s for s in Registry.model_validate(run["registry"]).sources}
        frozen = None
        for check in run.get("source_checks", []):
            key = check["source_id"]
            if key not in sources or source_scope(sources[key]) != source_scope(old_sources[key]):
                continue
            displayed = sources[key].resolved_html_rules.window_date_policy == "source_displayed"
            day_key = "selection_date" if displayed else "publication_date"
            evidence_key = day_key + "_evidence"
            days_key = "selection_dates" if displayed else "publication_dates"
            checks = {c["url"]: c for c in check.get("article_checks", [])}
            discoveries = {c["url"]: c for c in check.get("candidate_observations", [])}
            for url in sorted(set(check.get("candidate_urls", [])) | checks.keys()):
                observation = checks.get(url, {})
                observed_at = observation.get(
                    "observed_at", run.get("observed_at", run["created_at"])
                )
                observed_at = min(observed_at, run["created_at"])
                if observed_at < latest[key].get(url, ""):
                    continue
                latest[key][url] = observed_at
                discovered = discoveries.get(url, {})
                parsed = (
                    json.loads(storage.read(observation["extraction_sha256"]))
                    if observation.get("extraction_sha256")
                    else {}
                )
                day = observation.get(day_key)
                if day is None and len(discovered.get(days_key, [])) == 1:
                    day = discovered[days_key][0]
                if day and not str(mindate) <= day <= str(maxdate):
                    # A newer observation can supersede a previously in-window date.
                    found[key].pop(url, None)
                    continue
                evidence = (
                    parsed.get(evidence_key, [])
                    or observation.get(evidence_key, [])
                    or discovered.get(evidence_key, [])
                )
                evidence = [
                    dict(e, raw_sha256=e.get("raw_sha256") or observation.get("raw_sha256"))
                    for e in evidence
                ]
                if any(not e["raw_sha256"] for e in evidence):
                    raise ValueError("Historical publication evidence lacks raw lineage")
                for e in evidence:
                    storage.read(e["raw_sha256"])
                frozen = frozen or storage.put(json_bytes(run))
                found[key][url] = {
                    "url": url,
                    "title": parsed.get("title", discovered.get("title", url)),
                    # Conflicts must be fetched/reviewed again without borrowing a date.
                    day_key: None
                    if observation.get("error_code") == "html_date_conflict"
                    or len(discovered.get(days_key, [])) > 1
                    else day,
                    evidence_key: evidence,
                    "collection_manifest_sha256": frozen,
                    "collection_id": run["run_id"],
                }
    return {key: list(value.values()) for key, value in found.items()}
