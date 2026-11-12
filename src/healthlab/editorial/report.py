"""Write the review artifacts for a loaded editorial run and evidence packet."""

import html
import json
from collections import Counter

from healthlab.editorial.contracts import passage_inputs
from healthlab.editorial.packet import evidence_packet
from healthlab.editorial.runs import is_current_run, jobs, load
from healthlab.editorial_dates import (
    DATE_POLICY,
    date_role,
    editorial_literal_dates,
    incomplete_date_mentions,
)
from healthlab.store import json_bytes

DATE_LABELS = {
    "yearless": "Thiếu năm",
    "relative_date": "Mốc tương đối, chưa có ngày cụ thể",
    "as_of_without_date": "Số liệu hoặc trạng thái chưa có ngày chốt",
    "as_of": "Mốc chốt số liệu hoặc trạng thái",
    "deadline": "Hạn thực hiện",
    "range_endpoint": "Đầu hoặc cuối khoảng thời gian",
    "unverified": "Ngày cần đối chiếu với sự kiện",
}


def date_review_details(row, article, window, version):
    reasons = article["review_gate"]["reasons"]
    if "supported_in_window_development_missing" not in reasons or "relevant" not in {
        article["proposal"]["relevance"],
        (article["review"] or {}).get("relevance"),
    }:
        return None
    units, dates = passage_inputs(row, version=version)
    detected = {(d["passage_id"], d["start"], d["end"], d["date"]) for d in dates}
    mentions = []
    for unit in units:
        pid, quote = unit["passage_id"], unit["quote"]
        for item in editorial_literal_dates(quote, policy=DATE_POLICY):
            mentions.append(
                dict(
                    item,
                    passage_id=pid,
                    kind=date_role(quote, item),
                    inside_window=window["mindate"] <= item["date"] <= window["maxdate"],
                    detected_in_run=(pid, item["start"], item["end"], item["date"]) in detected,
                )
            )
        mentions.extend(dict(item, passage_id=pid) for item in incomplete_date_mentions(quote))
    return {
        "document_id": article["document_id"],
        "scope": (article.get("scope_assessment") or {}).get("verdict"),
        "gate_reasons": reasons,
        "mentions": mentions,
    }


def _date_review_html(detail):
    esc = html.escape
    lines = []
    for item in detail["mentions"]:
        label = DATE_LABELS[item["kind"]]
        if "date" in item:
            label += " · " + ("trong kỳ" if item["inside_window"] else "ngoài kỳ")
            if not item["detected_in_run"]:
                label += " · bộ đọc của lượt gốc chưa nhận dạng"
        lines.append(
            f"<li>{esc(label)} · {esc(item['passage_id'])} · <q>{esc(item['quote'])}</q></li>"
        )
    items = "<ul>" + "".join(lines) + "</ul>" if lines else "<p>Chưa tìm thấy mốc ngày.</p>"
    return (
        "<details><summary>Đối chiếu lý do giữ bài về ngày</summary>"
        "<p>Chưa có ngày sự kiện trong kỳ được xác nhận. Các mốc dưới đây cần "
        "được đối chiếu với từng dữ kiện.</p>" + items + "</details>"
    )


def write_report(store, run, records, calls, packet, directory):
    run_id = run["run_id"]
    counts = Counter(a["proposal"]["relevance"] for a in packet["articles"])
    result = {
        "run_id": run_id,
        "dataset_id": run["dataset_id"],
        "window": run["question"],
        "articles": len(records),
        "classified": len(packet["articles"]),
        "relevance": dict(counts),
        "call_states": dict(Counter(j["status"] for j in calls)),
        "calls_dispatched": run.get("prior_call_count", 0)
        + sum(j.get("charged", False) for j in calls),
        "reused_validated_articles": sum(bool(j.get("reused_from")) for j in calls),
        "review_scope": run.get("review_scope"),
        "call_budget": run["call_budget"],
        "reviewed": sum(j["role"] == "review" and j["status"] == "completed" for j in calls),
        "scope_assessed": sum(j["role"] == "scope" and j["status"] == "completed" for j in calls),
        "draft_eligible_articles": sum(
            a["eligible_for_unapproved_draft"] for a in packet["articles"]
        ),
        "draft_evidence_facts": len(packet["draft_evidence"]),
        "usage": {
            k: sum(j.get("usage", {}).get(k, 0) or 0 for j in calls)
            for k in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
        "errors": [
            {"document_id": j.get("document_id"), "role": j["role"], "code": j.get("error_code")}
            for j in calls
            if j["status"] in {"failed", "started"}
        ],
        "keyword_prefilter": False,
        "human_approval": False,
    }
    if "batch_canaries" in run:
        result["batch_canaries"] = run["batch_canaries"]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "knowledge.json").write_bytes(json_bytes(packet))
    (directory / "summary.json").write_bytes(json_bytes(result))
    esc = html.escape
    table = []
    date_reviews = []
    by_id = {a["document_id"]: a for a in packet["articles"]}
    for identifier, row in records.items():
        a = by_id.get(identifier)
        status = a["proposal"]["relevance"] if a else "unprocessed_or_failed"
        detail = (
            json.dumps(a, ensure_ascii=False, indent=2)
            if a
            else "No valid AI decision; not an exclusion."
        )
        date_review = date_review_details(row, a, run["question"], run["version"]) if a else None
        date_panel = ""
        if date_review is not None:
            date_reviews.append(date_review)
            date_panel = _date_review_html(date_review)
        table.append(
            f'<tr><td>{esc(status)}</td><td><a href="{esc(row["url"], quote=True)}">{esc(row["title"])}</a>{date_panel}<details><summary>AI decision/evidence</summary><pre>{esc(detail)}</pre></details><details><summary>Entire frozen article</summary><pre>{esc(row["body"])}</pre></details></td></tr>'
        )
    (directory / "date-review.json").write_bytes(json_bytes(date_reviews))
    (directory / "report.html").write_text(
        '<!doctype html><html lang="vi"><meta charset="utf-8"><title>HealthLab AI review</title><style>body{font:16px system-ui;max-width:1150px;margin:32px auto}td{padding:12px;border:1px solid #ccc;vertical-align:top}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><h1>HealthLab · AI evidence candidates</h1><p>Unapproved. Exact citations and AI agreement do not certify semantic truth. No keyword prefilter.</p><pre>'
        + esc(json.dumps(result, ensure_ascii=False, indent=2))
        + "</pre><table>"
        + "".join(table)
        + "</table></html>"
    )
    for job in calls:
        if job["role"] == "draft" and job["status"] == "completed":
            draft = json.loads(store.read(job["result_sha256"]))
            lines = [
                f"# {draft['title']}",
                "",
                f"Kỳ {run['question']['mindate']}–{run['question']['maxdate']}. BẢN NHÁP CHƯA DUYỆT.",
                "",
            ]
            # Evidence IDs are positional. Later reviews can renumber the current packet,
            # so resolve them against the exact evidence the writer was given.
            evidence = (
                json.loads(store.read(job["evidence_sha256"]))
                if job.get("evidence_sha256")
                else packet["draft_evidence"]
            )
            by_eid = {e["evidence_id"]: e for e in evidence}
            for section in draft["sections"]:
                lines += [f"## {section['title']}", ""]
                for claim in section["facts"]:
                    links = " ".join(
                        f"[{eid}]({by_eid[eid]['url']})" for eid in claim["evidence_ids"]
                    )
                    lines += [claim["text"] + " " + links, ""]
                lines += [
                    "**Ý nghĩa:** " + section["interpretation"],
                    "",
                    "**Giới hạn:** " + section["uncertainty"],
                    "",
                ]
            lines += ["## Đoạn bằng chứng đã lưu", ""]
            for e in evidence:
                lines += [
                    f"**{e['evidence_id']} — {e['title']}**",
                    "",
                    "> "
                    + "\n\n".join(
                        s["quote"] for s in e.get("supporting_passages", [{"quote": e["quote"]}])
                    ).replace("\n", "\n> "),
                    "",
                    f"Snapshot: `{e['snapshot_id']}`; body offsets {e['start']}–{e['end']}.",
                    "",
                ]
            (directory / "draft.md").write_text("\n".join(lines))
    return result


def export_report(store, run_id):
    run, records = load(store, run_id)
    calls = jobs(store, run)
    packet = evidence_packet(store, run_id)
    directory = store.root / ("runs" if is_current_run(run) else "inspections") / run_id
    return write_report(store, run, records, calls, packet, directory)
