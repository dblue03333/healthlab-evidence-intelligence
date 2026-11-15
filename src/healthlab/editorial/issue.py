"""Turn a completed editorial run into a version-bound issue for human approval and email.

A run with a draft becomes a newsletter issue. A fully finished run in which no fact passed
the release gates becomes a "no qualifying news" digest listing the relevant articles and
why each was held back; nothing is invented to fill the issue.

The approval preview of a draft also lists near misses: articles judged on topic but held
back by a gate. The approver may promote one into a "side news" section. Its facts are the
classifier's source-bound statements with literal quotes and reported status; no model call
is made, and promotion is a revision that clears any approval.

When a second-model recall check has run (editorial/recall.py), the preview also lists the
articles it flags as possibly missed. One without classifier facts is promoted with a
sentence the approver writes (--text, --status), bound to the passages the check cited.
The approval records which flags were promoted, so these decisions become labels.

The issue pins the writer's draft, the exact evidence packet it was given and the source
dataset. Every claim must cite pinned evidence whose quote still matches the frozen body.
Approval binds one issue version; any revision clears it. Email goes through the delivery
ledger in editorial/mail.py, so a rerun never resends an accepted, pending or unknown receipt.

Usage (repo root):
    python -m healthlab.editorial.issue --store STORE --source-store DATA create RUN_ID
    python -m healthlab.editorial.issue --store STORE --source-store DATA preview ISSUE_ID
    python -m healthlab.editorial.issue ... approve ISSUE_ID --expected-version V --reviewer R --rationale T
    python -m healthlab.editorial.issue ... revise ISSUE_ID --expected-version V --content draft.json
    python -m healthlab.editorial.issue ... promote ISSUE_ID --expected-version V --document-id D [--facts 0,1]
    python -m healthlab.editorial.issue ... promote ISSUE_ID --expected-version V --document-id D --text T --status S
    python -m healthlab.editorial.issue ... email ISSUE_ID [--send]
"""

import argparse
import hashlib
import html
import json
import os
import sys
from collections.abc import Collection, Iterator, Sequence
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv
from pydantic import Field

from healthlab.common import JsonObject, configure_logging, utc_now, write_text_atomic
from healthlab.data.datasets import read_dataset
from healthlab.data.pilot import runner_lock
from healthlab.editorial.contracts import DraftClaim, EditorialDraft
from healthlab.editorial.mail import deliver, mailbox
from healthlab.editorial.packet import evidence_packet, requires_scope_assessment
from healthlab.editorial.recall import summary as recall_summary
from healthlab.editorial.review_policy import fact_time_basis, relative_time_words
from healthlab.editorial.runs import jobs, load
from healthlab.models import Contract, IngestionError
from healthlab.store import RunStore, json_bytes

VERSION = "editorial-issue-1"
MODE = "editorial_issue"
NO_RELEASE = "no_release"
NO_RELEASE_TITLE = "Không có tin đạt chuẩn phát hành trong kỳ"
DERIVED_REASONS = {"primary_not_newsletter_candidate", "review_not_newsletter_eligible"}
SOURCE_STATUS = {"completed": "hoàn tất", "partial": "thiếu một phần", "failed": "lỗi"}
# Plain-language gate reasons for the editor; unknown codes are shown as-is.
REASONS = {
    "primary_not_newsletter_candidate": "Bước phân loại không xếp bài là ứng viên bản tin",
    "review_not_newsletter_eligible": "Reviewer không xếp bài là ứng viên bản tin",
    "supported_in_window_development_missing": (
        "Không có sự kiện có ngày đầy đủ (ngày/tháng/năm) nằm trong kỳ"
    ),
    "scope_not_substantive": "Bài chỉ nhắc đến người cao tuổi/già hoá, chưa có nội dung cụ thể",
    "scope_requires_outside_knowledge": "Cần kiến thức ngoài bài mới thấy liên hệ với già hoá",
    "context_support_incomplete": "Dữ kiện thiếu ngữ cảnh (chủ thể hoặc đối tượng) được nguồn hỗ trợ",
    "topic_unresolved_or_disagreed": "Reviewer không đồng ý bài thuộc chủ đề già hoá/tuổi thọ",
    "blocking_findings": "Reviewer nêu lỗi chặn trong dữ kiện",
    "article_level_finding": "Reviewer nêu lỗi ở cấp cả bài (chủ đề hoặc nguồn gốc bài)",
    "topic_passage_missing": "Reviewer không chỉ ra đoạn nào chứng minh bài đúng chủ đề",
    "topic_not_relevant": "AI phân loại cho rằng bài không thuộc chủ đề già hoá/tuổi thọ",
    "current_development_missing": (
        "Mọi dữ kiện đều có ngày trước kỳ, hoặc bài không được đăng trong kỳ"
    ),
}
# How the writer must date a fact under publication-window-1; shown to the approver.
TIME_BASIS = {
    "event_date_in_window": "ngày sự kiện {event_date}, trong kỳ",
    "event_date_before_window": "ngày sự kiện {event_date}, trước kỳ — không viết như tin trong kỳ",
    "event_date_after_window": "dự kiến ngày {event_date}, sau kỳ — không viết như đã làm",
    "publication_date": (
        "nguồn không ghi ngày sự kiện — viết theo bài đăng ngày {publication_date}"
    ),
}
# Reviewer flags on released facts (fact-flags-1); shown only in the approval preview.
FLAGS = {
    "reviewer_not_confirmed": "Reviewer chưa xác nhận dữ kiện này khớp với nguồn",
    "citation_incomplete": "Reviewer cho rằng trích dẫn chưa đủ cho toàn bộ câu",
    "context_missing:actor": "Thiếu chủ thể (ai thực hiện) trong đoạn nguồn",
    "context_missing:document": "Thiếu tên văn bản/chương trình trong đoạn nguồn",
    "context_missing:population": "Thiếu đối tượng hưởng lợi trong đoạn nguồn",
    "context_missing:status": "Thiếu trạng thái (đã làm/đang/dự kiến) trong đoạn nguồn",
}
# Reported status of a side-news fact, as the classifier read it from the source.
STATUS = {
    "proposed": "đề xuất",
    "announced": "đã công bố, chưa triển khai",
    "adopted": "đã thông qua",
    "effective": "có hiệu lực",
    "pilot": "thí điểm",
    "operational": "đang triển khai",
    "published_report": "báo cáo đã công bố",
}
SIDE_NEWS_TITLE = "Tin bên lề / Đang chuẩn bị"


class SideItem(Contract):
    text: str = Field(min_length=1, max_length=1200)
    reported_status: str = Field(min_length=1, max_length=40)
    evidence_ids: list[str] = Field(min_length=1, max_length=5)


class IssueDraft(EditorialDraft):
    """The writer's draft plus side news the approver promoted; older issues have none."""

    side_news: list[SideItem] = Field(default_factory=list, max_length=10)


def _claims(draft: EditorialDraft | IssueDraft) -> Iterator[DraftClaim | SideItem]:
    yield from (claim for section in draft.sections for claim in section.facts)
    yield from getattr(draft, "side_news", ())


def time_word_findings(draft: EditorialDraft | IssueDraft) -> list[Any]:
    """Relative time words in a draft whose evidence is dated by publication-window-1.

    Older drafts were written without a time basis per fact and are not checked.
    """
    found = []
    texts = [("Tiêu đề", draft.title)]
    for section in draft.sections:
        texts.append((f"Mục «{section.title}»", section.title))
        texts += [(f"Mục «{section.title}»", c.text) for c in section.facts]
        texts.append((f"Mục «{section.title}» — Ý nghĩa", section.interpretation))
        texts.append((f"Mục «{section.title}» — Giới hạn", section.uncertainty))
    texts += [(SIDE_NEWS_TITLE, item.text) for item in getattr(draft, "side_news", ())]
    for where, text in texts:
        found += [(where, word) for word in relative_time_words(text)]
    return found


def _time_dated(by_id: JsonObject) -> bool:
    return any(e.get("time_basis") for e in by_id.values())


def _check(draft: IssueDraft, evidence: list[JsonObject], records: JsonObject) -> JsonObject:
    by_id = {e["evidence_id"]: e for e in evidence}
    if len(by_id) != len(evidence) or not evidence:
        raise ValueError("Issue needs a nonempty evidence packet with unique IDs")
    for e in evidence:
        row = records.get(e["document_id"])
        if row is None or row["body"][e["start"] : e["end"]] != e["quote"]:
            raise ValueError("Evidence quote differs from the frozen article body")
    for claim in _claims(draft):
        if not set(claim.evidence_ids) <= by_id.keys():
            raise ValueError("Claim cites evidence outside the pinned packet")
    return by_id


def _sources(source_store: RunStore, dataset_id: str) -> tuple[str, list[JsonObject]]:
    _, _, silver, gold = read_dataset(source_store, dataset_id)
    return gold.status, [
        {
            "source_id": s.source_id,
            "organization": s.organization,
            "required": s.required,
            "status": s.status,
            "coverage_complete": s.coverage_complete,
            "candidates": s.candidates,
            "in_window": sum(d.startswith(f"{s.source_id}:") for d in gold.document_ids),
        }
        for s in silver.sources
    ]


def _near_misses(packet: JsonObject, exclude: Collection[str] = ()) -> list[JsonObject]:
    """Articles judged on topic but held back by a gate, with their source-bound facts."""
    misses = []
    for a in packet["articles"]:
        if a["eligible_for_unapproved_draft"] or a["document_id"] in exclude:
            continue
        if "relevant" not in {a["proposal"]["relevance"], (a["review"] or {}).get("relevance")}:
            continue
        reasons = list(dict.fromkeys(a["review_gate"]["reasons"]))
        # "Not a candidate" verdicts are consequences; show them only without a root cause.
        reasons = [r for r in reasons if r not in DERIVED_REASONS] or reasons
        misses.append(
            {
                "document_id": a["document_id"],
                "title": a["title"],
                "url": a["url"],
                "reasons": reasons,
                "facts": a["proposal"].get("resolved_facts") or [],
            }
        )
    return misses


def _no_release_digest(
    store: RunStore, run: JsonObject, records: JsonObject, calls: list[JsonObject]
) -> JsonObject:
    """Summarize a fully finished run in which no fact passed the release gates.

    Only a complete run may claim "no qualifying news"; a failed or unfinished call
    would make that claim untrue, so it is refused instead.
    """
    if not calls or any(j["status"] != "completed" for j in calls):
        raise ValueError("Editorial run is unfinished or has failed calls; no issue can be made")
    classified = sum(j["role"] == "classify" for j in calls)
    roles = ["review", *(["scope"] if requires_scope_assessment(run) else [])]
    if classified != len(records) or any(
        r not in run.get("prepared_review_roles", []) for r in roles
    ):
        raise ValueError("Editorial run has not finished classify, review and scope")
    packet = evidence_packet(store, run["run_id"])
    if packet["draft_evidence"]:
        raise ValueError("Editorial run has draft evidence but no completed draft; run draft")
    watchlist = [
        {k: a[k] for k in ("document_id", "title", "url", "reasons")} for a in _near_misses(packet)
    ]
    return {
        "kind": NO_RELEASE,
        "articles": len(records),
        "classified": classified,
        "relevant": sum(a["proposal"]["relevance"] == "relevant" for a in packet["articles"]),
        "reviewed": sum(j["role"] == "review" for j in calls),
        "scope_assessed": sum(j["role"] == "scope" for j in calls),
        "watchlist": watchlist,
    }


def create_issue(store: RunStore, source_store: RunStore, run_id: str) -> JsonObject:
    run, records = load(store, run_id)
    calls = jobs(store, run)
    drafts = [j for j in calls if j["role"] == "draft" and j["status"] == "completed"]
    if len(drafts) > 1:
        raise ValueError("Editorial run has more than one completed draft")
    if drafts:
        job = drafts[0]
        draft = IssueDraft.model_validate_json(store.read(job["result_sha256"]))
        evidence = json.loads(store.read(job["evidence_sha256"]))
        _check(draft, evidence, records)
        pinned = {
            "kind": "draft",
            "draft_job_id": job["run_id"],
            "original_draft_sha256": job["result_sha256"],
            "evidence_sha256": job["evidence_sha256"],
            "issue_sha256": job["result_sha256"],
        }
    else:
        digest = store.put(json_bytes(_no_release_digest(store, run, records, calls)))
        pinned = {"kind": NO_RELEASE, "issue_sha256": digest}
    dataset_status, sources = _sources(source_store, run["dataset_id"])
    issue = store.new(MODE, run["question"])
    issue.update(
        version=VERSION,
        editorial_run_id=run_id,
        editorial_version=run["version"],
        dataset_id=run["dataset_id"],
        dataset_status=dataset_status,
        sources=sources,
        **pinned,
        status="completed",
        stage="draft_ready",
        human_approval=False,
        finished_at=utc_now(),
    )
    store.save(issue)
    render_preview(store, issue["run_id"])
    return issue


def load_issue(store: RunStore, issue_id: str) -> Any:
    issue = store.load(issue_id)
    if issue.get("mode") != MODE or issue.get("version") != VERSION:
        raise ValueError("Expected an editorial issue")
    if issue.get("kind") == NO_RELEASE:
        return issue, json.loads(store.read(issue["issue_sha256"])), None
    _, records = load(store, issue["editorial_run_id"])
    draft = IssueDraft.model_validate_json(store.read(issue["issue_sha256"]))
    evidence = json.loads(store.read(issue["evidence_sha256"]))
    by_id = _check(draft, evidence, records)
    return issue, draft, by_id


def title(issue: JsonObject, content: IssueDraft | JsonObject) -> str:
    # The issue kind decides the content: a no-release digest is a plain object.
    return NO_RELEASE_TITLE if issue.get("kind") == NO_RELEASE else cast(IssueDraft, content).title


def revise(store: RunStore, issue_id: str, content: JsonObject, *, expected_version) -> JsonObject:
    with runner_lock(store.root):
        issue, _, _ = load_issue(store, issue_id)
        if issue.get("kind") == NO_RELEASE:
            raise ValueError("A no-release digest is generated from the run and cannot be revised")
        if expected_version != issue["issue_sha256"]:
            raise ValueError("Stale issue version; reload before editing")
        draft = IssueDraft.model_validate(content)
        _, records = load(store, issue["editorial_run_id"])
        _check(draft, json.loads(store.read(issue["evidence_sha256"])), records)
        previous = issue["issue_sha256"]
        issue["issue_sha256"] = store.put(json_bytes(draft.model_dump()))
        issue.setdefault("revisions", []).append(
            {"previous": previous, "next": issue["issue_sha256"], "created_at": utc_now()}
        )
        # The old approval stays in history but cannot approve this edit.
        issue.pop("approval_sha256", None)
        issue["stage"] = "draft_ready"
        store.save(issue)
    render_preview(store, issue_id)
    return issue


def promote(
    store: RunStore,
    issue_id: str,
    document_id: str,
    *,
    expected_version,
    fact_indexes=None,
    text=None,
    status=None,
) -> JsonObject:
    """Add a held-back article's source-bound facts to the side-news section.

    The facts are the classifier's statements with literal quotes; nothing is rewritten by
    a model. An article flagged by the recall check instead takes the approver's own
    sentence (text, status), cited to the passages the check named. Like a revision, this
    creates a new version and clears any approval.
    """
    if (text is None) != (status is None):
        raise ValueError("Give --text and --status together")
    if text is not None:
        if fact_indexes is not None:
            raise ValueError("Choose either classifier facts or your own text, not both")
        return _promote_recalled(
            store, issue_id, document_id, text, status, expected_version=expected_version
        )
    with runner_lock(store.root):
        issue, draft, by_id = load_issue(store, issue_id)
        if issue.get("kind") == NO_RELEASE:
            raise ValueError("Side news can only be added to an issue with a draft")
        if expected_version != issue["issue_sha256"]:
            raise ValueError("Stale issue version; reload before editing")
        if any(e["document_id"] == document_id for e in by_id.values()):
            raise ValueError("This article is already in the issue")
        packet = evidence_packet(store, issue["editorial_run_id"])
        article = next((a for a in packet["articles"] if a["document_id"] == document_id), None)
        if article is None:
            raise ValueError("Article is not in this editorial run")
        if article["eligible_for_unapproved_draft"]:
            raise ValueError("Article passed the gates; its facts are already draft evidence")
        facts = article["proposal"].get("resolved_facts") or []
        if not facts:
            raise ValueError(
                "Article has no classifier facts; if the recall check flagged it, "
                "write your own sentence with --text and --status"
            )
        indexes = list(range(len(facts))) if fact_indexes is None else list(fact_indexes)
        if not indexes or len(set(indexes)) != len(indexes):
            raise ValueError("Choose at least one fact, each once")
        if not all(0 <= i < len(facts) for i in indexes):
            raise ValueError(f"Article has facts 0..{len(facts) - 1}")
        _, records = load(store, issue["editorial_run_id"])
        row = records[document_id]
        evidence = json.loads(store.read(issue["evidence_sha256"]))
        side = sum(e["evidence_id"].startswith("S") for e in evidence)
        content = draft.model_dump()
        for n, index in enumerate(indexes, side + 1):
            f = facts[index]
            evidence.append(
                dict(
                    evidence_id=f"S{n}",
                    document_id=document_id,
                    snapshot_id=row["snapshot_id"],
                    url=row["url"],
                    title=row["title"],
                    raw_sha256=row["raw_sha256"],
                    promoted_fact_index=index,
                    **f,
                )
            )
            content["side_news"].append(
                {
                    "text": f["statement"],
                    "reported_status": f["reported_status"],
                    "evidence_ids": [f"S{n}"],
                }
            )
        new = IssueDraft.model_validate(content)
        _check(new, evidence, records)
        previous = issue["issue_sha256"]
        issue["evidence_sha256"] = store.put(json_bytes(evidence))
        issue["issue_sha256"] = store.put(json_bytes(new.model_dump()))
        issue.setdefault("revisions", []).append(
            {
                "previous": previous,
                "next": issue["issue_sha256"],
                "created_at": utc_now(),
                "promoted": {"document_id": document_id, "fact_indexes": indexes},
            }
        )
        issue.pop("approval_sha256", None)
        issue["stage"] = "draft_ready"
        store.save(issue)
    render_preview(store, issue_id)
    return issue


def _promote_recalled(
    store: RunStore, issue_id: str, document_id: str, text: str, status: str, *, expected_version
) -> JsonObject:
    if status not in STATUS:
        raise ValueError(f"Status must be one of: {', '.join(STATUS)}")
    with runner_lock(store.root):
        issue, draft, by_id = load_issue(store, issue_id)
        if issue.get("kind") == NO_RELEASE:
            raise ValueError("Side news can only be added to an issue with a draft")
        if expected_version != issue["issue_sha256"]:
            raise ValueError("Stale issue version; reload before editing")
        if any(e["document_id"] == document_id for e in by_id.values()):
            raise ValueError("This article is already in the issue")
        recall = recall_summary(store, issue["editorial_run_id"]) or {"flags": []}
        flag = next((f for f in recall["flags"] if f["document_id"] == document_id), None)
        if flag is None:
            raise ValueError("Article is not flagged by the recall check; use its facts instead")
        run, records = load(store, issue["editorial_run_id"])
        row = records[document_id]
        evidence = json.loads(store.read(issue["evidence_sha256"]))
        side = sum(e["evidence_id"].startswith("S") for e in evidence)
        # A side item cites at most five passages; the check lists the strongest first.
        passages = flag["passages"][:5]
        basis = fact_time_basis(None, False, row.get("publication_date"), run["question"])
        ids = []
        for n, p in enumerate(passages, side + 1):
            ids.append(f"S{n}")
            evidence.append(
                {
                    "evidence_id": f"S{n}",
                    "document_id": document_id,
                    "snapshot_id": row["snapshot_id"],
                    "url": row["url"],
                    "title": row["title"],
                    "raw_sha256": row["raw_sha256"],
                    "passage_id": p["passage_id"],
                    "start": p["start"],
                    "end": p["end"],
                    "quote": p["quote"],
                    "publication_date": row.get("publication_date"),
                    "time_basis": basis,
                    "recall_check_id": recall["recall_check_id"],
                }
            )
        content = draft.model_dump()
        content["side_news"].append({"text": text, "reported_status": status, "evidence_ids": ids})
        new = IssueDraft.model_validate(content)
        _check(new, evidence, records)
        previous = issue["issue_sha256"]
        issue["evidence_sha256"] = store.put(json_bytes(evidence))
        issue["issue_sha256"] = store.put(json_bytes(new.model_dump()))
        issue.setdefault("revisions", []).append(
            {
                "previous": previous,
                "next": issue["issue_sha256"],
                "created_at": utc_now(),
                "promoted": {
                    "document_id": document_id,
                    "recall_check_id": recall["recall_check_id"],
                    "passage_ids": [p["passage_id"] for p in passages],
                    "text_by": "approver",
                },
            }
        )
        issue.pop("approval_sha256", None)
        issue["stage"] = "draft_ready"
        store.save(issue)
    render_preview(store, issue_id)
    return issue


def _recall_outcomes(
    store: RunStore, issue: JsonObject, by_id: JsonObject | None
) -> JsonObject | None:
    """Each recall flag and whether the approver put it in this version: implicit labels."""
    recall = recall_summary(store, issue["editorial_run_id"])
    if recall is None:
        return None
    included = {e["document_id"] for e in (by_id or {}).values()}
    return {
        "recall_check_id": recall["recall_check_id"],
        "model": recall["model"],
        "completed": recall["completed"],
        "targets": recall["targets"],
        "flags": [
            {"document_id": f["document_id"], "included": f["document_id"] in included}
            for f in recall["flags"]
        ],
    }


def approve(store: RunStore, issue_id: str, *, expected_version, reviewer, rationale) -> JsonObject:
    if not reviewer.strip() or not rationale.strip():
        raise ValueError("Reviewer and rationale are required")
    with runner_lock(store.root):
        issue, draft, by_id = load_issue(store, issue_id)
        if expected_version != issue["issue_sha256"]:
            raise ValueError("Stale issue version; reload before approving")
        if issue.get("kind") != NO_RELEASE and _time_dated(by_id):
            found = time_word_findings(draft)
            if found:
                listed = "; ".join(f"{where}: «{word}»" for where, word in found)
                raise ValueError(
                    f"Draft uses relative time words ({listed}); fix them with revise first"
                )
        if issue["dataset_status"] != "ready" or any(
            s["required"] and s["status"] != "completed" for s in issue["sources"]
        ):
            raise ValueError("Required-source coverage is incomplete; issue cannot be approved")
        event = {
            "issue_id": issue_id,
            "issue_sha256": issue["issue_sha256"],
            "reviewer": reviewer.strip(),
            "rationale": rationale.strip(),
            "created_at": utc_now(),
            "identity": "self_declared_local_operator",
        }
        outcomes = _recall_outcomes(store, issue, by_id)
        if outcomes is not None:
            event["recall_outcomes"] = outcomes
        issue["approval_sha256"] = store.put(json_bytes(event))
        issue.setdefault("approval_history", []).append(issue["approval_sha256"])
        store.save(issue)
    return issue


def approval(store: RunStore, issue: JsonObject) -> dict[str, str]:
    if not issue.get("approval_sha256"):
        raise ValueError("This issue version has no human approval")
    event = json.loads(store.read(issue["approval_sha256"]))
    if event["issue_sha256"] != issue["issue_sha256"] or event["issue_id"] != issue["run_id"]:
        raise ValueError("Approval does not match the current issue version")
    return event


def _source_numbers(
    draft: EditorialDraft | IssueDraft, by_id: JsonObject
) -> dict[str, tuple[int, JsonObject]]:
    numbers: dict[str, tuple[int, JsonObject]] = {}
    for claim in _claims(draft):
        for eid in claim.evidence_ids:
            numbers.setdefault(by_id[eid]["url"], (len(numbers) + 1, by_id[eid]))
    return numbers


def _quotes(e: JsonObject, label: str) -> list[str]:
    """Every source passage behind one piece of evidence, for the approval preview."""
    quotes = [(label, s["quote"]) for s in e.get("supporting_passages") or [e]]
    # A date taken from another passage is shown so the approver can check it.
    date_quote = (e.get("date_evidence") or {}).get("quote")
    if date_quote and date_quote not in {q for _, q in quotes}:
        quotes.append((f"{label} (ngày)", date_quote))
    parts = [
        '<blockquote style="color:#555;border-left:3px solid #ccc;'
        f'margin:4px 0 8px;padding-left:10px">{html.escape(k)}: {html.escape(q)}</blockquote>'
        for k, q in quotes
    ]
    basis = TIME_BASIS.get(e.get("time_basis") or "")
    if basis:
        parts.append(
            '<p style="color:#555;font-size:14px;margin:0 0 6px 13px">'
            f"🕒 Mốc thời gian ({html.escape(label)}): "
            f"{html.escape(basis.format(event_date=e.get('event_date'), publication_date=e.get('publication_date')))}"
            "</p>"
        )
    return parts


def _draft_body(
    draft: EditorialDraft | IssueDraft, by_id: JsonObject, evidence_quotes: bool
) -> tuple[list[str], list[str], str]:
    esc = html.escape
    numbers = _source_numbers(draft, by_id)
    lines, parts = [], []

    def claim_lines(text, evidence_ids):
        refs = sorted({numbers[by_id[e]["url"]][0] for e in evidence_ids})
        lines.append("- " + text + " " + "".join(f"[{n}]" for n in refs))
        links = "".join(
            f'<a href="{esc(url, quote=True)}" style="text-decoration:none">[{n}]</a>'
            for url, (n, _) in numbers.items()
            if n in refs
        )
        parts.append(f"<p>{esc(text)} <sup>{links}</sup></p>")

    for section in draft.sections:
        lines += ["", section.title.upper()]
        parts.append(f'<h2 style="font-size:19px;margin-top:28px">{esc(section.title)}</h2>')
        for claim in section.facts:
            claim_lines(claim.text, claim.evidence_ids)
            if evidence_quotes:
                for eid in claim.evidence_ids:
                    e = by_id[eid]
                    parts += _quotes(e, eid)
                    for flag in e.get("review_flags") or []:
                        parts.append(
                            '<p style="color:#8a4b00;font-size:14px;margin:0 0 6px 13px">'
                            f"⚠ Cần xem ({esc(eid)}): {esc(FLAGS.get(flag, flag))}</p>"
                        )
        lines += ["Ý nghĩa: " + section.interpretation, "Giới hạn: " + section.uncertainty]
        parts.append(f"<p><b>Ý nghĩa:</b> {esc(section.interpretation)}</p>")
        parts.append(f'<p style="color:#555"><b>Giới hạn:</b> {esc(section.uncertainty)}</p>')
    side_news = getattr(draft, "side_news", ())
    if side_news:
        lines += ["", SIDE_NEWS_TITLE.upper()]
        parts.append(f'<h2 style="font-size:19px;margin-top:28px">{esc(SIDE_NEWS_TITLE)}</h2>')
        for item in side_news:
            status = STATUS.get(item.reported_status)
            claim_lines(
                item.text + (f" (Trạng thái theo nguồn: {status}.)" if status else ""),
                item.evidence_ids,
            )
            if evidence_quotes:
                for eid in item.evidence_ids:
                    parts += _quotes(by_id[eid], eid)
    lines += ["", "NGUỒN"]
    parts.append('<h2 style="font-size:17px;margin-top:28px">Nguồn</h2><ol>')
    for url, (n, e) in numbers.items():
        lines.append(f"[{n}] {e['title']} — {url}")
        parts.append(f'<li><a href="{esc(url, quote=True)}">{esc(e["title"])}</a></li>')
    parts.append("</ol>")
    return lines, parts, "AI hỗ trợ trích dẫn và soạn thảo; mọi dữ kiện dẫn về đoạn nguồn đã lưu."


def _no_release_body(digest: JsonObject) -> tuple[list[str], list[str], str]:
    esc = html.escape
    scan = (
        f"Đã quét {digest['articles']} bài, phân loại {digest['classified']}, "
        f"{digest['relevant']} bài được đánh giá liên quan; reviewer kiểm {digest['reviewed']} bài, "
        f"đánh giá phạm vi {digest['scope_assessed']} bài. Không có dữ kiện nào qua đủ các cổng "
        "phát hành, nên kỳ này không có tin. Hệ thống không tự tạo tin để lấp chỗ trống."
    )
    lines, parts = ["", scan], [f"<p>{esc(scan)}</p>"]
    if digest["watchlist"]:
        heading = "Bài liên quan nhưng chưa đạt chuẩn (để biên tập viên xem lại)"
        lines += ["", heading.upper()]
        parts.append(f'<h2 style="font-size:19px;margin-top:28px">{esc(heading)}</h2><ol>')
        for a in digest["watchlist"]:
            reasons = [REASONS.get(r, r) for r in a["reasons"]]
            lines.append(f"- {a['title']} — {a['url']}")
            lines += [f"    · {r}" for r in reasons]
            items = "".join(f"<li>{esc(r)}</li>" for r in reasons)
            parts.append(
                f'<li style="margin-bottom:12px"><a href="{esc(a["url"], quote=True)}">'
                f'{esc(a["title"])}</a><ul style="color:#555;font-size:14px">{items}</ul></li>'
            )
        parts.append("</ol>")
    return lines, parts, "AI hỗ trợ phân loại và kiểm tra; không có dữ kiện nào được phát hành."


def _promote_command(issue: JsonObject, document_id: str) -> str:
    return (
        f"python -m healthlab.editorial.issue --store STORE --source-store DATA promote "
        f"{issue['run_id']} --expected-version {issue['issue_sha256']} "
        f"--document-id {document_id}"
    )


def _recall_body(
    issue: JsonObject, recall: JsonObject, flags: list[JsonObject], *, can_promote
) -> list[str]:
    """Preview-only: articles a second model thinks were missed; never in the sent email."""
    esc = html.escape
    progress = f"{recall['completed']}/{recall['targets']} bài bị loại đã được rà"
    if recall["failed"]:
        progress += f", {recall['failed']} bài lỗi chưa rà được"
    parts = [
        f'<hr><h2 style="font-size:19px;margin-top:28px;color:#8a4b00">'
        f"Có thể bị sót (AI rà soát thứ 2: {esc(recall['model'])}, chỉ người duyệt thấy)</h2>",
        f'<p style="color:#555;font-size:14px">{esc(progress)}. AI này đọc lại các bài pipeline '
        "đã loại, theo đúng tiêu chí phạm vi, và cho rằng các bài dưới đây có nội dung cụ thể "
        "về người cao tuổi. Đây chỉ là gợi ý: bỏ qua nếu không đồng ý."
        + (
            " Nếu muốn đưa vào mục "
            f"«{esc(SIDE_NEWS_TITLE)}», tự viết một câu theo nguồn và chạy lệnh dưới bài, "
            "thêm <code>--text &quot;…&quot; --status operational</code> (hoặc trạng thái khác: "
            f"{esc(', '.join(STATUS))})."
            if can_promote
            else ""
        )
        + "</p>",
    ]
    if not flags:
        parts.append('<p style="color:#555;font-size:14px">Không có bài nào bị gắn cờ.</p>')
        return parts
    parts.append("<ol>")
    for f in flags:
        quotes = "".join(
            '<blockquote style="color:#555;border-left:3px solid #ccc;margin:4px 0 8px;'
            f'padding-left:10px">{esc(p["passage_id"])}: {esc(p["quote"])}</blockquote>'
            for p in f["passages"]
        )
        why = "".join(
            f"<li>{esc(label)}: {esc(value)}</li>"
            for label, value in (
                ("Bối cảnh già hoá", f["ageing_context"]),
                ("Nội dung cụ thể", f["substantive_detail"]),
                ("Lý do", f["rationale"]),
            )
            if value
        )
        command = _promote_command(issue, f["document_id"]) + ' --text "…" --status …'
        parts.append(
            f'<li style="margin-bottom:18px"><a href="{esc(f["url"], quote=True)}">'
            f'{esc(f["title"])}</a><ul style="color:#555;font-size:14px">{why}</ul>{quotes}'
            + (f'<p style="font-size:12px"><code>{esc(command)}</code></p>' if can_promote else "")
            + "</li>"
        )
    parts.append("</ol>")
    return parts


def _near_miss_body(
    issue: JsonObject, misses: list[JsonObject], recalled: Collection[str] = frozenset()
) -> list[str]:
    """Preview-only list the approver can promote from; never part of the sent email."""
    esc = html.escape
    heading = "Bài đúng chủ đề nhưng bị chặn (chỉ người duyệt thấy)"
    parts = [
        f'<hr><h2 style="font-size:19px;margin-top:28px;color:#8a4b00">{esc(heading)}</h2>',
        '<p style="color:#555;font-size:14px">Nếu muốn đưa một bài vào mục '
        f"«{esc(SIDE_NEWS_TITLE)}», chạy lệnh <code>promote</code> ghi dưới bài. "
        "Thêm <code>--facts 0,2</code> để chỉ chọn một số dữ kiện.</p><ol>",
    ]
    for a in misses:
        reasons = "".join(f"<li>{esc(REASONS.get(r, r))}</li>" for r in a["reasons"])
        facts = []
        for i, f in enumerate(a["facts"]):
            status = STATUS.get(f.get("reported_status"))
            facts.append(
                f"<li><b>Dữ kiện {i}</b>"
                + (f" · {esc(status)}" if status else "")
                + (f" · ngày {esc(f['event_date'])}" if f.get("event_date") else "")
                + f": {esc(f['statement'])}"
                + "".join(_quotes(f, f"Nguồn {i}"))
                + "</li>"
            )
        command = _promote_command(issue, a["document_id"])
        second = (
            '<p style="color:#8a4b00;font-size:14px;margin:4px 0">AI rà soát thứ 2 cũng cho '
            "rằng bài có nội dung cụ thể về người cao tuổi.</p>"
            if a["document_id"] in recalled
            else ""
        )
        parts.append(
            f'<li style="margin-bottom:18px"><a href="{esc(a["url"], quote=True)}">'
            f'{esc(a["title"])}</a>{second}<ul style="color:#555;font-size:14px">{reasons}</ul>'
            + (f'<ul style="font-size:14px">{"".join(facts)}</ul>' if facts else "")
            + f'<p style="font-size:12px"><code>{esc(command)}</code></p></li>'
        )
    parts.append("</ol>")
    return parts


def _time_word_body(found: list[tuple[str, str]]) -> list[str]:
    """Preview-only warning; approval is refused until a revision removes these words."""
    esc = html.escape
    items = "".join(f"<li>{esc(where)}: «{esc(word)}»</li>" for where, word in found)
    return [
        '<div style="border:2px solid #b00020;padding:8px 14px;margin:16px 0">'
        '<p style="color:#b00020;margin:4px 0"><b>Chưa duyệt được: bản nháp dùng từ chỉ '
        "thời gian tương đối.</b> Hãy sửa bằng lệnh <code>revise</code>, ghi ngày tuyệt đối "
        f"hoặc «theo bài đăng ngày …».</p><ul>{items}</ul></div>"
    ]


def render(
    issue: JsonObject,
    content: IssueDraft | JsonObject,
    by_id: JsonObject | None,
    *,
    approved=None,
    evidence_quotes=False,
    near_misses=(),
    time_words=(),
    recall=None,
) -> tuple[str, str]:
    esc = html.escape
    period = issue["question"]
    label = "Đã duyệt nội bộ" if approved else "BẢN NHÁP AI — CHƯA ĐƯỢC DUYỆT"
    heading = title(issue, content)
    lines = [
        "HealthLab Signal — Vietnam Aging & Longevity Monitor",
        label,
        f"Kỳ: {period['mindate']} — {period['maxdate']}",
        "",
        heading,
    ]
    parts = [
        '<!doctype html><html lang="vi"><head><meta charset="utf-8"></head>',
        '<body style="font-family:Arial,sans-serif;max-width:720px;margin:24px auto;'
        'padding:16px;line-height:1.55;color:#1a1a1a">',
        '<p style="color:#666;font-size:13px;margin:0">HealthLab Signal — Vietnam Aging &amp; '
        "Longevity Monitor</p>",
        f'<p style="font-size:13px;margin:4px 0"><b>{esc(label)}</b> · Kỳ {esc(period["mindate"])}'
        f" — {esc(period['maxdate'])}</p>",
        f'<h1 style="font-size:24px">{esc(heading)}</h1>',
    ]
    if time_words:
        parts += _time_word_body(time_words)
    if issue.get("kind") == NO_RELEASE:
        body_lines, body_parts, footer = _no_release_body(cast(JsonObject, content))
    else:
        assert by_id is not None  # A release draft always comes with its evidence.
        body_lines, body_parts, footer = _draft_body(
            cast(IssueDraft, content), by_id, evidence_quotes
        )
    lines += body_lines
    parts += body_parts
    recalled = {f["document_id"] for f in (recall or {}).get("flags", ())}
    if near_misses:
        parts += _near_miss_body(issue, near_misses, recalled)
    if recall is not None:
        shown = {a["document_id"] for a in near_misses} | {
            e["document_id"] for e in (by_id or {}).values()
        }
        if issue.get("kind") == NO_RELEASE:
            shown |= {a["document_id"] for a in cast(JsonObject, content)["watchlist"]}
        flags = [f for f in recall["flags"] if f["document_id"] not in shown]
        parts += _recall_body(issue, recall, flags, can_promote=issue.get("kind") != NO_RELEASE)
    lines += [
        "",
        "Phạm vi nguồn đã kiểm tra (theo kỳ ngày đăng; không khẳng định đầy đủ toàn website):",
    ]
    parts.append(
        '<p style="color:#666;font-size:13px">Phạm vi nguồn đã kiểm tra (theo kỳ ngày đăng; '
        "không khẳng định đầy đủ toàn website):<br>"
    )
    for s in issue["sources"]:
        line = (
            f"{s['organization']}: {SOURCE_STATUS.get(s['status'], s['status'])}, "
            f"{s['in_window']} bài trong kỳ ({s['candidates']} mục đã liệt kê khi thu thập)"
        )
        lines.append(line)
        parts.append(esc(line) + "<br>")
    if approved:
        footer += f" Người duyệt: {approved['reviewer']}, {approved['created_at']}."
    footer += f" Issue {issue['run_id']} · version {issue['issue_sha256'][:12]}"
    lines += ["", footer]
    parts += [f'</p><p style="color:#888;font-size:12px">{esc(footer)}</p>', "</body></html>"]
    return "\n".join(lines), "\n".join(parts)


def render_preview(store: RunStore, issue_id: str) -> Path:
    issue, draft, by_id = load_issue(store, issue_id)
    directory = store.root / "runs" / issue_id
    text, _ = render(issue, draft, by_id)
    misses: Sequence[JsonObject] = ()
    found: Sequence[Any] = ()
    if issue.get("kind") != NO_RELEASE:
        packet = evidence_packet(store, issue["editorial_run_id"])
        misses = _near_misses(packet, {e["document_id"] for e in by_id.values()})
        if _time_dated(by_id):
            found = time_word_findings(draft)
    _, markup = render(
        issue,
        draft,
        by_id,
        evidence_quotes=True,
        near_misses=misses,
        time_words=found,
        recall=recall_summary(store, issue["editorial_run_id"]),
    )
    write_text_atomic(directory / "newsletter.txt", text)
    write_text_atomic(directory / "newsletter.html", markup)
    content = draft if issue.get("kind") == NO_RELEASE else draft.model_dump()
    write_text_atomic(
        directory / ("digest.json" if issue.get("kind") == NO_RELEASE else "draft.json"),
        json.dumps(content, ensure_ascii=False, indent=2),
    )
    return directory / "newsletter.html"


def prepare_email(
    store: RunStore, issue_id: str, sender: str, recipient: str
) -> tuple[MIMEMultipart, str]:
    issue, draft, by_id = load_issue(store, issue_id)
    event = approval(store, issue)
    mailbox(sender)
    mailbox(recipient)
    text, markup = render(issue, draft, by_id, approved=event)
    key = hashlib.sha256(
        json_bytes(
            {
                "issue_id": issue_id,
                "version": issue["issue_sha256"],
                "sender": sender,
                "recipient": recipient,
            }
        )
    ).hexdigest()
    # compat32 MIME keeps the space between folded encoded words; EmailMessage drops it
    # in long Vietnamese subjects ("hỗ trợ phục" -> "hỗ trợphục").
    message = MIMEMultipart("alternative")
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = Header(  # type: ignore[assignment]  # compat32 accepts a Header.
        f"HealthLab Signal · {title(issue, draft)} · {issue['question']['maxdate']}", "utf-8"
    )
    message["Date"] = formatdate(localtime=False, usegmt=True)
    message["Message-ID"] = f"<{key}@{sender.rsplit('@', 1)[1]}>"
    message.attach(MIMEText(text, "plain", "utf-8"))
    message.attach(MIMEText(markup, "html", "utf-8"))
    return message, key


def export_email(store: RunStore, issue_id: str, sender: str, recipients: list[str]) -> list[str]:
    if not recipients:
        raise ValueError("At least one recipient is required")
    paths = []
    for recipient in sorted(set(recipients)):
        message, key = prepare_email(store, issue_id, sender, recipient)
        path = store.root / "runs" / issue_id / f"email-{key}.eml"
        path.write_bytes(message.as_bytes())
        paths.append(str(path))
    return paths


def send_gmail(
    store: RunStore,
    issue_id: str,
    sender: str,
    recipients: list[str],
    app_password: str,
    *,
    smtp_factory=None,
) -> list[JsonObject]:
    if not recipients or not app_password:
        raise ValueError("Sender, recipients and Gmail app password are required")
    if not sender.lower().endswith("@gmail.com"):
        raise ValueError("This adapter is for personal @gmail.com sender accounts")
    with runner_lock(store.root):
        # Validate approval and every recipient before any connection or partial delivery.
        prepared = [prepare_email(store, issue_id, sender, r) for r in sorted(set(recipients))]
        issue = store.load(issue_id)
        ledger_path = store.root / "runs" / issue_id / "delivery.json"
        ledger, outcomes = deliver(ledger_path, prepared, sender, app_password, smtp_factory)
        issue["delivery_ledger_sha256"] = store.put(json_bytes(ledger))
        if all(o["status"] == "accepted" for o in outcomes):
            issue["stage"] = "sent"
        store.save(issue)
    return outcomes


def main(argv=None):
    load_dotenv()
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--source-store", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("create").add_argument("run_id")
    for name in ("preview", "status", "approve", "revise", "promote", "email"):
        command = commands.add_parser(name)
        command.add_argument("issue_id")
        if name in {"approve", "revise", "promote"}:
            command.add_argument("--expected-version", required=True)
        if name == "approve":
            command.add_argument("--reviewer", required=True)
            command.add_argument("--rationale", required=True)
        if name == "revise":
            command.add_argument("--content", required=True, type=Path)
        if name == "promote":
            command.add_argument("--document-id", required=True)
            command.add_argument(
                "--facts",
                type=lambda v: [int(i) for i in v.split(",")],
                help="Comma-separated fact numbers from the preview (default: all)",
            )
            command.add_argument("--text", help="Approver's sentence for a recall-flagged article")
            command.add_argument("--status", choices=sorted(STATUS), help="Status with --text")
        if name == "email":
            command.add_argument("--send", action="store_true", help="Actually send via Gmail SMTP")
    args = parser.parse_args(argv)
    store, source = RunStore(args.store), RunStore(args.source_store)
    try:
        if args.command == "create":
            issue = create_issue(store, source, args.run_id)
            result = {"issue_id": issue["run_id"], "version": issue["issue_sha256"]}
            result["preview"] = str(store.root / "runs" / issue["run_id"] / "newsletter.html")
        elif args.command in {"preview", "status"}:
            issue, draft, _ = load_issue(store, args.issue_id)
            result = {
                "issue_id": issue["run_id"],
                "version": issue["issue_sha256"],
                "stage": issue["stage"],
                "approved": bool(issue.get("approval_sha256")),
                "kind": issue.get("kind", "draft"),
                "title": title(issue, draft),
            }
            if args.command == "preview":
                result["preview"] = str(render_preview(store, args.issue_id))
        elif args.command == "approve":
            issue = approve(
                store,
                args.issue_id,
                expected_version=args.expected_version,
                reviewer=args.reviewer,
                rationale=args.rationale,
            )
            result = {"issue_id": issue["run_id"], "approval": issue["approval_sha256"]}
        elif args.command == "revise":
            issue = revise(
                store,
                args.issue_id,
                json.loads(args.content.read_bytes()),
                expected_version=args.expected_version,
            )
            result = {"issue_id": issue["run_id"], "version": issue["issue_sha256"]}
        elif args.command == "promote":
            issue = promote(
                store,
                args.issue_id,
                args.document_id,
                expected_version=args.expected_version,
                fact_indexes=args.facts,
                text=args.text,
                status=args.status,
            )
            result = {"issue_id": issue["run_id"], "version": issue["issue_sha256"]}
            result["preview"] = str(store.root / "runs" / issue["run_id"] / "newsletter.html")
        else:
            sender = os.environ.get("HEALTHLAB_GMAIL_USER", "")
            recipients = [
                r.strip() for r in os.environ.get("HEALTHLAB_EMAIL_TO", "").split(",") if r.strip()
            ]
            if args.send:
                result = {
                    "deliveries": send_gmail(
                        store,
                        args.issue_id,
                        sender,
                        recipients,
                        os.environ.get("HEALTHLAB_GMAIL_APP_PASSWORD", ""),
                    )
                }
            else:
                result = {"exported": export_email(store, args.issue_id, sender, recipients)}
    except (IngestionError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
