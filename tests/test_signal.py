"""Synthetic Signal cases, not HealthLab evidence or human factual evaluation."""

import json
import smtplib
from datetime import date

import httpx
import pytest

from healthlab.provider import Completion, FakeProvider
from healthlab.signal.collection import collect, load_documents
from healthlab.signal.contracts import Registry, Source
from healthlab.signal.newsletter import (
    approve,
    draft_issue,
    export_email,
    prepare_email,
    render_preview,
    revise,
    send_gmail,
)
from healthlab.signal.triage import prepare_triage, submit_triage
from healthlab.store import RunStore

FEED = b"""<rss version="2.0"><channel><title>Synthetic news</title><item>
<title>Ageing report</title><link>https://example.org/ageing</link>
<description><![CDATA[<p>The report describes older adults.</p><script>ignore me</script>]]></description>
<pubDate>Fri, 09 Oct 2026 00:00:00 GMT</pubDate></item></channel></rss>"""


def registry(required=True):
    return Registry(
        sources=[
            Source(
                source_id="news",
                organization="Synthetic source",
                kind="rss",
                url="https://example.org/feed",
                usage_policy="Synthetic fixture only",
                required=required,
            )
        ]
    )


def collected(tmp_path, *, status=200, feed=FEED):
    store = RunStore(tmp_path)
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, content=feed))
    ) as client:
        run = collect(
            store,
            registry(),
            mindate=date(2026, 10, 2),
            maxdate=date(2026, 10, 9),
            rss_client=client,
        )
    return store, run


def content(snapshot_id, passage="The report describes older adults."):
    return {
        "overview": "Một tài liệu cần team kiểm tra.",
        "signals": [
            {
                "snapshot_id": snapshot_id,
                "topic": "aging_society",
                "headline": "Tài liệu về người cao tuổi",
                "what_happened": "Nguồn đề cập người cao tuổi.",
                "supporting_passage": passage,
                "vietnam_relevance": "Khả năng áp dụng tại Việt Nam cần review.",
                "uncertainty": "Chỉ đọc RSS summary; chưa có full text.",
            }
        ],
    }


def triaged(store, collection, *, include=True):
    triage = prepare_triage(store, collection["run_id"])
    if include:
        snapshot_id = collection["documents"][0]["snapshot_id"]
        document = load_documents(store, collection)[snapshot_id]
        quote = "The report describes older adults."
        start = document.text.index(quote)
        submit_triage(
            store,
            triage["run_id"],
            [
                {
                    "snapshot_id": snapshot_id,
                    "decision": "include",
                    "topic": "aging_society",
                    "freshness": "current_update",
                    "reported_status": "published_report",
                    "vietnam_relevance": "direct",
                    "jurisdiction": "vietnam",
                    "source_role": "original",
                    "provenance_note": "Synthetic editorial interpretation for tests only",
                    "event_key": "synthetic-ageing-report",
                    "reason_codes": ["eligible"],
                    "rationale": "Synthetic relevance decision, not real human judgement",
                    "passages": [
                        {
                            "start": start,
                            "end": start + len(quote),
                            "quote": quote,
                            "purposes": ["topic", "development", "status", "vietnam_relevance"],
                        }
                    ],
                }
            ],
            expected_head=None,
            reviewer="Synthetic test reviewer",
            rationale="Fixture only",
        )
    return triage["run_id"]


def issue(tmp_path):
    store, collection = collected(tmp_path)
    value = content(collection["documents"][0]["snapshot_id"])
    provider = FakeProvider(
        [Completion(json.dumps(value), "fake-test", "stop", {"total_tokens": 10})]
    )
    run = draft_issue(store, collection["run_id"], provider, triage_id=triaged(store, collection))
    return store, run, value


def human_accept(store, run):
    return approve(
        store,
        run["run_id"],
        expected_version=run["issue_sha256"],
        reviewer="Synthetic test reviewer",
        rationale="Fixture only, not real review",
    )


def test_collect_preserves_raw_and_deduplicates_even_when_feed_changes(tmp_path):
    store, first = collected(tmp_path)
    assert first["status"] == "completed"
    docs = load_documents(store, first)
    assert "ignore me" not in next(iter(docs.values())).text
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=FEED.replace(b"Synthetic news", b"Different channel")
            )
        )
    ) as client:
        second = collect(
            store,
            registry(),
            mindate=date(2026, 10, 2),
            maxdate=date(2026, 10, 9),
            rss_client=client,
        )
    assert second["documents"][0]["change"] == "unchanged"
    assert second["documents"][0]["snapshot_id"] == first["documents"][0]["snapshot_id"]
    assert store.read(first["source_checks"][0]["raw_sha256"]) == FEED


def test_feed_failure_is_visible_and_blocks_approval(tmp_path):
    store, run, _ = issue(tmp_path)
    collection = store.load(run["collection_run_id"])
    collection["source_checks"][0]["status"] = "failed"
    store.save(collection)
    with pytest.raises(ValueError, match="coverage"):
        human_accept(store, run)


def test_no_active_sources_is_not_a_completed_monitoring_run(tmp_path):
    store = RunStore(tmp_path)
    config = registry()
    config.sources[0].enabled = False
    run = collect(store, config, mindate=date(2026, 10, 2), maxdate=date(2026, 10, 9))
    assert run["status"] == "failed" and not run["documents"]


@pytest.mark.parametrize("feed", [b"not XML", b"<!DOCTYPE rss><rss><channel/></rss>", b"<feed/>"])
def test_invalid_source_does_not_disappear(tmp_path, feed):
    _, run = collected(tmp_path, feed=feed)
    assert run["status"] == "failed"
    assert run["source_checks"][0]["status"] == "failed"


def test_wrong_quote_fails_draft_but_retains_source_and_raw_response(tmp_path):
    store, run = collected(tmp_path)
    provider = FakeProvider(
        [
            Completion(
                json.dumps(content(run["documents"][0]["snapshot_id"], "invented text")),
                "fake-test",
                "stop",
                {},
            )
        ]
    )
    draft = draft_issue(store, run["run_id"], provider, triage_id=triaged(store, run))
    assert draft["status"] == "failed" and draft["requests"]
    assert "issue_sha256" not in draft
    assert load_documents(store, run)


def test_edit_invalidates_approval_and_stale_approval_is_rejected(tmp_path):
    store, run, value = issue(tmp_path)
    human_accept(store, run)
    value["overview"] = "Nội dung biên tập mới."
    edited = revise(store, run["run_id"], value, expected_version=run["issue_sha256"])
    with pytest.raises(ValueError, match="approval"):
        prepare_email(store, run["run_id"], "sender@gmail.com", "reader@example.org")
    with pytest.raises(ValueError, match="Stale"):
        human_accept(store, run)
    assert edited["issue_sha256"] != run["issue_sha256"]


def test_preview_escapes_markup_and_email_contains_portable_source_links(tmp_path):
    store, run, value = issue(tmp_path)
    value["signals"][0]["headline"] = '<script>alert("x")</script>'
    run = revise(store, run["run_id"], value, expected_version=run["issue_sha256"])
    preview = render_preview(store, run["run_id"]).read_text()
    assert "<script>" not in preview and "&lt;script&gt;" in preview
    human_accept(store, run)
    msg, _ = prepare_email(store, run["run_id"], "sender@gmail.com", "reader@example.org")
    assert "https://example.org/ageing" in msg.get_body(preferencelist=("plain",)).get_content()
    assert "outputs/objects" not in msg.get_body(preferencelist=("html",)).get_content()


def test_email_export_never_sends_and_requires_approval(tmp_path):
    store, run, _ = issue(tmp_path)
    with pytest.raises(ValueError, match="approval"):
        export_email(store, run["run_id"], "sender@gmail.com", ["reader@example.org"])
    human_accept(store, run)
    paths = export_email(store, run["run_id"], "sender@gmail.com", ["reader@example.org"])
    assert len(paths) == 1
    assert not (store.root / "runs" / run["run_id"] / "delivery.json").exists()


class MailServer:
    def __init__(self, sent, fail=False):
        self.sent, self.fail = sent, fail

    def login(self, user, password):
        pass

    def send_message(self, message, **kwargs):
        self.sent.append(kwargs["to_addrs"])
        if self.fail:
            raise smtplib.SMTPServerDisconnected("synthetic ambiguous outcome")
        return {}

    def close(self):
        pass


def test_delivery_replay_skips_accepted_recipients(tmp_path):
    store, run, _ = issue(tmp_path)
    human_accept(store, run)
    sent = []

    def factory():
        return MailServer(sent)

    first = send_gmail(
        store,
        run["run_id"],
        "sender@gmail.com",
        ["a@example.org", "b@example.org"],
        "fixture",
        smtp_factory=factory,
    )
    second = send_gmail(
        store,
        run["run_id"],
        "sender@gmail.com",
        ["a@example.org", "b@example.org"],
        "fixture",
        smtp_factory=factory,
    )
    assert len(sent) == 2 and len(first) == 2
    assert all(o["skipped"] for o in second)
    assert sent == [["a@example.org"], ["b@example.org"]]


def test_unknown_smtp_outcome_is_not_retried(tmp_path):
    store, run, _ = issue(tmp_path)
    human_accept(store, run)
    sent = []
    first = send_gmail(
        store,
        run["run_id"],
        "sender@gmail.com",
        ["a@example.org"],
        "fixture",
        smtp_factory=lambda: MailServer(sent, True),
    )
    second = send_gmail(
        store,
        run["run_id"],
        "sender@gmail.com",
        ["a@example.org"],
        "fixture",
        smtp_factory=lambda: MailServer(sent),
    )
    assert first[0]["status"] == "unknown"
    assert second[0]["skipped"] and len(sent) == 1


def test_reapproving_unchanged_issue_does_not_resend(tmp_path):
    store, run, _ = issue(tmp_path)
    human_accept(store, run)
    sent = []
    send_gmail(
        store,
        run["run_id"],
        "sender@gmail.com",
        ["a@example.org"],
        "fixture",
        smtp_factory=lambda: MailServer(sent),
    )
    human_accept(store, run)
    result = send_gmail(
        store,
        run["run_id"],
        "sender@gmail.com",
        ["a@example.org"],
        "fixture",
        smtp_factory=lambda: MailServer(sent),
    )
    assert result[0]["skipped"] and len(sent) == 1


def test_empty_or_invalid_recipient_list_never_connects(tmp_path):
    store, run, _ = issue(tmp_path)
    human_accept(store, run)

    def forbidden():
        pytest.fail("No connection expected")

    with pytest.raises(ValueError):
        send_gmail(store, run["run_id"], "sender@gmail.com", [], "fixture", smtp_factory=forbidden)
    with pytest.raises(ValueError):
        send_gmail(
            store,
            run["run_id"],
            "sender@gmail.com",
            ["ok@example.org", "bad\naddress"],
            "fixture",
            smtp_factory=forbidden,
        )
