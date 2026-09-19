"""Conservative lexical context checks, not semantic entailment evaluation."""

import re
import unicodedata

from healthlab.models import IngestionError


def normalized(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[‐‑‒–—−-]", " ", text)


SCOPED = re.compile(r"\b(subset|subgroup|sub group)\b|nhóm con|phân nhóm")
COMPARATIVE = re.compile(r"\b(compared|versus|vs\.?|control|comparator|placebo)\b|so với|đối chứng")
COMPARATORS = {
    "waitlist": r"\bwait\s*list\b|danh sách chờ",
    "placebo": r"\bplacebo\b|giả dược",
    "standard_care": r"\bstandard care\b|chăm sóc tiêu chuẩn",
    "usual_care": r"\busual care\b|chăm sóc thông thường",
}


def context_requirements(item):
    """Expose exact source passages so the model can satisfy deterministic rules.

    A subset anywhere in the abstract deliberately triggers a conservative
    study-level check. This can flag a whole-cohort claim as well; it is not an
    outcome-to-subgroup inference engine.
    """
    sections = item["document"]["abstract_sections"]
    scoped = [i for i, section in enumerate(sections) if SCOPED.search(normalized(section["text"]))]
    return {
        "scope_section_indexes": scoped,
        "scope_passages": [
            p
            for p in item["passages"]
            if p["section_index"] in scoped and SCOPED.search(normalized(p["text"]))
        ],
    }


def validate_context(claim, items):
    text = normalized(claim.text)
    for item in items:
        cited_ids = {
            r.passage_id for r in claim.references if r.evidence_item_id == item["evidence_item_id"]
        }
        cited = [p for p in item["passages"] if p["passage_id"] in cited_ids]
        cited_text = normalized(" ".join(p["text"] for p in cited))
        if claim.kind not in {"finding", "conflict"}:
            continue
        requirements = context_requirements(item)
        if requirements["scope_section_indexes"]:
            if not SCOPED.search(text):
                raise IngestionError(
                    "missing_subgroup_scope",
                    f"{claim.claim_id}: source reports a subset/subgroup; retain its scope explicitly",
                )
            if not any(p["passage_id"] in cited_ids for p in requirements["scope_passages"]):
                raise IngestionError(
                    "missing_scope_citation",
                    f"{claim.claim_id}: cite the source passage describing the subset/subgroup",
                )
        comparator_described = COMPARATIVE.search(cited_text) or re.search(
            r"\b(?:allocated|randomi[sz]ed|assigned)\b.{0,200}\b(?:either|versus|vs|or)\b",
            cited_text,
        )
        if COMPARATIVE.search(text) and not comparator_described:
            raise IngestionError(
                "missing_comparator_citation",
                f"{claim.claim_id}: cited passages do not describe a comparator",
            )
        for pattern in COMPARATORS.values():
            if re.search(pattern, text) and not re.search(pattern, cited_text):
                raise IngestionError(
                    "missing_comparator_citation",
                    f"{claim.claim_id}: cite the named comparator, not just a generic control group",
                )
