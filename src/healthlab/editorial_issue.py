"""Turn a completed editorial run into a version-bound issue for human approval and email.

A run with a draft becomes a newsletter issue. A fully finished run in which no fact passed
the release gates becomes a "no qualifying news" digest listing the relevant articles and
why each was held back; nothing is invented to fill the issue.

The issue pins the writer's draft, the exact evidence packet it was given and the source
dataset. Every claim must cite pinned evidence whose quote still matches the frozen body.
Approval binds one issue version; any revision clears it. Email reuses the Signal delivery
ledger, so a rerun never resends an accepted, pending or unknown receipt.

Usage (repo root):
    python -m healthlab.editorial_issue --store STORE --source-store DATA create RUN_ID
    python -m healthlab.editorial_issue --store STORE --source-store DATA preview ISSUE_ID
    python -m healthlab.editorial_issue ... approve ISSUE_ID --expected-version V --reviewer R --rationale T
    python -m healthlab.editorial_issue ... revise ISSUE_ID --expected-version V --content draft.json
    python -m healthlab.editorial_issue ... email ISSUE_ID [--send]
"""

import argparse
import hashlib
import html
import json
import os
import sys
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path

from dotenv import load_dotenv

from healthlab.data.datasets import read_dataset
from healthlab.data.pilot import runner_lock
from healthlab.editorial.contracts import EditorialDraft
from healthlab.editorial.packet import evidence_packet, requires_scope_assessment
from healthlab.editorial.runs import jobs, load
from healthlab.models import IngestionError
from healthlab.pubmed import utc_now
from healthlab.signal.newsletter import deliver, mailbox
from healthlab.store import RunStore, json_bytes
from healthlab.synthesis import write_preview

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


def _check(draft, evidence, records):
    by_id = {e["evidence_id"]: e for e in evidence}
    if len(by_id) != len(evidence) or not evidence:
        raise ValueError("Issue needs a nonempty evidence packet with unique IDs")
    for e in evidence:
        row = records.get(e["document_id"])
        if row is None or row["body"][e["start"] : e["end"]] != e["quote"]:
            raise ValueError("Evidence quote differs from the frozen article body")
    for section in draft.sections:
        for claim in section.facts:
            if not set(claim.evidence_ids) <= by_id.keys():
                raise ValueError("Claim cites evidence outside the pinned packet")
    return by_id


def _sources(source_store, dataset_id):
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


def _no_release_digest(store, run, records, calls):
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
    watchlist = []
    for a in packet["articles"]:
        if "relevant" not in {a["proposal"]["relevance"], (a["review"] or {}).get("relevance")}:
            continue
        reasons = list(dict.fromkeys(a["review_gate"]["reasons"]))
        # "Not a candidate" verdicts are consequences; show them only without a root cause.
        reasons = [r for r in reasons if r not in DERIVED_REASONS] or reasons
        watchlist.append(
            {
                "document_id": a["document_id"],
                "title": a["title"],
                "url": a["url"],
                "reasons": reasons,
            }
        )
    return {
        "kind": NO_RELEASE,
        "articles": len(records),
        "classified": classified,
        "relevant": sum(a["proposal"]["relevance"] == "relevant" for a in packet["articles"]),
        "reviewed": sum(j["role"] == "review" for j in calls),
        "scope_assessed": sum(j["role"] == "scope" for j in calls),
        "watchlist": watchlist,
    }


def create_issue(store, source_store, run_id):
    run, records = load(store, run_id)
    calls = jobs(store, run)
    drafts = [j for j in calls if j["role"] == "draft" and j["status"] == "completed"]
    if len(drafts) > 1:
        raise ValueError("Editorial run has more than one completed draft")
    if drafts:
        job = drafts[0]
        draft = EditorialDraft.model_validate_json(store.read(job["result_sha256"]))
        evidence = json.loads(store.read(job["evidence_sha256"]))
        _check(draft, evidence, records)
        pinned = dict(
            kind="draft",
            draft_job_id=job["run_id"],
            original_draft_sha256=job["result_sha256"],
            evidence_sha256=job["evidence_sha256"],
            issue_sha256=job["result_sha256"],
        )
    else:
        digest = store.put(json_bytes(_no_release_digest(store, run, records, calls)))
        pinned = dict(kind=NO_RELEASE, issue_sha256=digest)
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


def load_issue(store, issue_id):
    issue = store.load(issue_id)
    if issue.get("mode") != MODE or issue.get("version") != VERSION:
        raise ValueError("Expected an editorial issue")
    if issue.get("kind") == NO_RELEASE:
        return issue, json.loads(store.read(issue["issue_sha256"])), None
    _, records = load(store, issue["editorial_run_id"])
    draft = EditorialDraft.model_validate_json(store.read(issue["issue_sha256"]))
    evidence = json.loads(store.read(issue["evidence_sha256"]))
    by_id = _check(draft, evidence, records)
    return issue, draft, by_id


def title(issue, content):
    return NO_RELEASE_TITLE if issue.get("kind") == NO_RELEASE else content.title


def revise(store, issue_id, content, *, expected_version):
    with runner_lock(store.root):
        issue, _, _ = load_issue(store, issue_id)
        if issue.get("kind") == NO_RELEASE:
            raise ValueError("A no-release digest is generated from the run and cannot be revised")
        if expected_version != issue["issue_sha256"]:
            raise ValueError("Stale issue version; reload before editing")
        draft = EditorialDraft.model_validate(content)
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


def approve(store, issue_id, *, expected_version, reviewer, rationale):
    if not reviewer.strip() or not rationale.strip():
        raise ValueError("Reviewer and rationale are required")
    with runner_lock(store.root):
        issue, _, _ = load_issue(store, issue_id)
        if expected_version != issue["issue_sha256"]:
            raise ValueError("Stale issue version; reload before approving")
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
        issue["approval_sha256"] = store.put(json_bytes(event))
        issue.setdefault("approval_history", []).append(issue["approval_sha256"])
        store.save(issue)
    return issue


def approval(store, issue):
    if not issue.get("approval_sha256"):
        raise ValueError("This issue version has no human approval")
    event = json.loads(store.read(issue["approval_sha256"]))
    if event["issue_sha256"] != issue["issue_sha256"] or event["issue_id"] != issue["run_id"]:
        raise ValueError("Approval does not match the current issue version")
    return event


def _source_numbers(draft, by_id):
    numbers = {}
    for section in draft.sections:
        for claim in section.facts:
            for eid in claim.evidence_ids:
                numbers.setdefault(by_id[eid]["url"], (len(numbers) + 1, by_id[eid]))
    return numbers


def _draft_body(draft, by_id, evidence_quotes):
    esc = html.escape
    numbers = _source_numbers(draft, by_id)
    lines, parts = [], []
    for section in draft.sections:
        lines += ["", section.title.upper()]
        parts.append(f'<h2 style="font-size:19px;margin-top:28px">{esc(section.title)}</h2>')
        for claim in section.facts:
            refs = sorted({numbers[by_id[e]["url"]][0] for e in claim.evidence_ids})
            lines.append("- " + claim.text + " " + "".join(f"[{n}]" for n in refs))
            links = "".join(
                f'<a href="{esc(url, quote=True)}" style="text-decoration:none">[{n}]</a>'
                for url, (n, _) in numbers.items()
                if n in refs
            )
            parts.append(f"<p>{esc(claim.text)} <sup>{links}</sup></p>")
            if evidence_quotes:
                for eid in claim.evidence_ids:
                    e = by_id[eid]
                    quotes = [(eid, s["quote"]) for s in e.get("supporting_passages") or [e]]
                    # A date taken from another passage is shown so the approver can check it.
                    if (e.get("date_evidence") or {}).get("quote"):
                        quotes.append((f"{eid} (ngày)", e["date_evidence"]["quote"]))
                    for label, quote in quotes:
                        parts.append(
                            '<blockquote style="color:#555;border-left:3px solid #ccc;'
                            f'margin:4px 0 8px;padding-left:10px">{esc(label)}: {esc(quote)}</blockquote>'
                        )
                    for flag in e.get("review_flags") or []:
                        parts.append(
                            '<p style="color:#8a4b00;font-size:14px;margin:0 0 6px 13px">'
                            f"⚠ Cần xem ({esc(eid)}): {esc(FLAGS.get(flag, flag))}</p>"
                        )
        lines += ["Ý nghĩa: " + section.interpretation, "Giới hạn: " + section.uncertainty]
        parts.append(f"<p><b>Ý nghĩa:</b> {esc(section.interpretation)}</p>")
        parts.append(f'<p style="color:#555"><b>Giới hạn:</b> {esc(section.uncertainty)}</p>')
    lines += ["", "NGUỒN"]
    parts.append('<h2 style="font-size:17px;margin-top:28px">Nguồn</h2><ol>')
    for url, (n, e) in numbers.items():
        lines.append(f"[{n}] {e['title']} — {url}")
        parts.append(f'<li><a href="{esc(url, quote=True)}">{esc(e["title"])}</a></li>')
    parts.append("</ol>")
    return lines, parts, "AI hỗ trợ trích dẫn và soạn thảo; mọi dữ kiện dẫn về đoạn nguồn đã lưu."


def _no_release_body(digest):
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


def render(issue, content, by_id, *, approved=None, evidence_quotes=False):
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
    body_lines, body_parts, footer = (
        _no_release_body(content)
        if issue.get("kind") == NO_RELEASE
        else _draft_body(content, by_id, evidence_quotes)
    )
    lines += body_lines
    parts += body_parts
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


def render_preview(store, issue_id):
    issue, draft, by_id = load_issue(store, issue_id)
    directory = store.root / "runs" / issue_id
    text, _ = render(issue, draft, by_id)
    _, markup = render(issue, draft, by_id, evidence_quotes=True)
    write_preview(directory / "newsletter.txt", text)
    write_preview(directory / "newsletter.html", markup)
    content = draft if issue.get("kind") == NO_RELEASE else draft.model_dump()
    write_preview(
        directory / ("digest.json" if issue.get("kind") == NO_RELEASE else "draft.json"),
        json.dumps(content, ensure_ascii=False, indent=2),
    )
    return directory / "newsletter.html"


def prepare_email(store, issue_id, sender, recipient):
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
    message["Subject"] = Header(
        f"HealthLab Signal · {title(issue, draft)} · {issue['question']['maxdate']}", "utf-8"
    )
    message["Date"] = formatdate(localtime=False, usegmt=True)
    message["Message-ID"] = f"<{key}@{sender.rsplit('@', 1)[1]}>"
    message.attach(MIMEText(text, "plain", "utf-8"))
    message.attach(MIMEText(markup, "html", "utf-8"))
    return message, key


def export_email(store, issue_id, sender, recipients):
    if not recipients:
        raise ValueError("At least one recipient is required")
    paths = []
    for recipient in sorted(set(recipients)):
        message, key = prepare_email(store, issue_id, sender, recipient)
        path = store.root / "runs" / issue_id / f"email-{key}.eml"
        path.write_bytes(message.as_bytes())
        paths.append(str(path))
    return paths


def send_gmail(store, issue_id, sender, recipients, app_password, *, smtp_factory=None):
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
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--source-store", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("create").add_argument("run_id")
    for name in ("preview", "status", "approve", "revise", "email"):
        command = commands.add_parser(name)
        command.add_argument("issue_id")
        if name in {"approve", "revise"}:
            command.add_argument("--expected-version", required=True)
        if name == "approve":
            command.add_argument("--reviewer", required=True)
            command.add_argument("--rationale", required=True)
        if name == "revise":
            command.add_argument("--content", required=True, type=Path)
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
