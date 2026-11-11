"""Version-bound delegated agent editorial approvals, without publishing.

Literal spans and hashes protect review identity. Semantic support is the named
reviewer's judgment; this record never becomes an independent human label.
"""

import hashlib
import json
from typing import Literal

from pydantic import Field

from healthlab.models import Contract
from healthlab.store import json_bytes

VERSION = "agent-editorial-approval-1"


class ReviewedUnit(Contract):
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1)
    kind: Literal["source_fact", "editorial_analysis", "metadata"]
    evidence_ids: list[str]
    rationale: str = Field(min_length=1)


class AgentArticleReview(Contract):
    actor_type: Literal["agent"]
    actor: str = Field(min_length=1)
    authorization: str = Field(min_length=1)
    reviewed_at: str = Field(min_length=1)
    verdict: Literal["approved_for_local_editorial_use"]
    unresolved_findings: list[str]
    units: list[ReviewedUnit] = Field(min_length=1)
    human_approval: Literal[False] = False


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def approve_article(article, evidence, review, dependencies):
    """Validate complete literal review coverage and pin all supplied bytes.

    The caller must check evidence against frozen sources before declaring its
    verdict. Hash agreement alone does not establish the truth of any claim.
    """
    value = AgentArticleReview.model_validate(review)
    if value.unresolved_findings:
        raise ValueError("Unresolved findings block editorial approval")
    text = article.decode("utf-8")
    facts = json.loads(evidence)
    if not isinstance(facts, list) or not facts:
        raise ValueError("Approval requires a nonempty evidence packet")
    ids = [f["evidence_id"] for f in facts]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate evidence identity")
    if not dependencies or any(not name or not data for name, data in dependencies.items()):
        raise ValueError("Approval requires its frozen source dependencies")
    pos = 0
    for unit in sorted(value.units, key=lambda u: u.start):
        if unit.start < pos or text[unit.start : unit.end] != unit.text:
            raise ValueError("Review span differs from approved article")
        if text[pos : unit.start].strip():
            raise ValueError("Unreviewed article text")
        if not set(unit.evidence_ids) <= set(ids):
            raise ValueError("Unknown review evidence")
        if unit.kind == "source_fact" and not unit.evidence_ids:
            raise ValueError("Factual unit needs source evidence")
        pos = unit.end
    if text[pos:].strip():
        raise ValueError("Unreviewed article text")
    return {
        "schema_version": VERSION,
        "status": "agent_editorial_approved",
        "scope": "local_editorial_use",
        "article_sha256": sha256(article),
        "evidence_sha256": sha256(evidence),
        "review_sha256": sha256(json_bytes(value.model_dump())),
        "dependencies_sha256": {name: sha256(data) for name, data in sorted(dependencies.items())},
        "actor_type": "agent",
        "actor": value.actor,
        "authorization": value.authorization,
        "reviewed_at": value.reviewed_at,
        "human_approval": False,
        "published": False,
    }


def verify_approval(approval, article, evidence, review, dependencies):
    """Any changed article, review, packet or dependency requires a fresh approval."""
    current = approve_article(article, evidence, review, dependencies)
    if approval != current:
        raise ValueError("Approval no longer matches the reviewed version")
    return current
