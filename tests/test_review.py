import json

import httpx
import pytest

from healthlab.models import IngestionError
from healthlab.provider import FakeProvider
from healthlab.review import export_marketing, history, main, packet, submit
from healthlab.synthesis import load_bundle, synthesize_run
from tests.test_synthesis import extracted_source, response, synthesis_output


def source(tmp_path):
    store, _, extraction = extracted_source(tmp_path)
    items = load_bundle(store, extraction["run_id"])[2]
    run = synthesize_run(
        store, extraction["run_id"], FakeProvider([response(synthesis_output(items))])
    )
    return store, extraction, items, run


def decide(store, run, action="accept", head=None, **kwargs):
    return submit(
        store,
        run["run_id"],
        expected_head=head,
        action=action,
        reviewer="Test reviewer",
        rationale="Fixture-only decision",
        **kwargs,
    )


def test_offline_edit_accept_export_and_reject(tmp_path, monkeypatch):
    store, _, _, run = source(tmp_path)
    monkeypatch.setattr(httpx.Client, "__init__", lambda *a, **k: pytest.fail("Network called"))
    original = store.read(run["brief_sha256"])
    assert packet(store, run["run_id"])["brief"]["evidence_items"]
    with pytest.raises(ValueError, match="approve"):
        export_marketing(store, run["run_id"])
    edit = decide(store, run, "edit", edits={"C1": "Nghiên cứu báo cáo 60 người được phân nhóm."})
    with pytest.raises(ValueError, match="approve"):
        export_marketing(store, run["run_id"])
    accepted = decide(store, run, head=edit["event_sha256"])
    result = export_marketing(store, run["run_id"])
    text = store.read(result["sha256"]).decode()
    assert "Nghiên cứu" in text and "Source passage" in text
    assert accepted["event_sha256"] in text
    assert store.read(run["brief_sha256"]) == original
    assert len(history(store, run["run_id"])["events"]) == 2
    decide(store, run, "reject", head=accepted["event_sha256"])
    with pytest.raises(ValueError, match="approve"):
        export_marketing(store, run["run_id"])
    # Export is an immutable historical handoff, not an automatically revoked file.
    assert store.read(result["sha256"]).decode() == text


def test_stale_writer_and_edit_invalidate_approval(tmp_path):
    store, _, _, run = source(tmp_path)
    approved = decide(store, run)
    with pytest.raises(ValueError, match="Stale"):
        decide(store, run, "reject")
    assert history(store, run["run_id"])["head"] == approved["event_sha256"]
    decide(store, run, "edit", head=approved["event_sha256"], edits={"C1": "Revised text"})
    with pytest.raises(ValueError, match="approve"):
        export_marketing(store, run["run_id"])


def test_regeneration_does_not_inherit_review(tmp_path):
    store, extraction, items, first = source(tmp_path)
    approved = decide(store, first)
    second = synthesize_run(
        store, extraction["run_id"], FakeProvider([response(synthesis_output(items))])
    )
    assert first["brief_version_id"] != second["brief_version_id"]
    assert history(store, second["run_id"])["events"] == []
    with pytest.raises(ValueError, match="approve"):
        export_marketing(store, second["run_id"])
    # Copying the pointer across versions cannot approve another run.
    (store.root / "runs" / second["run_id"] / "review-head.json").write_text(
        json.dumps(approved["event_sha256"])
    )
    with pytest.raises(ValueError, match="another brief"):
        history(store, second["run_id"])


@pytest.mark.parametrize("edits", [{}, {"unknown": "text"}, {"C1": ""}, {"C1": 12}, []])
def test_invalid_edits_rejected(tmp_path, edits):
    store, _, _, run = source(tmp_path)
    with pytest.raises(ValueError, match="Edits"):
        decide(store, run, "edit", edits=edits)
    assert history(store, run["run_id"])["events"] == []


def test_corrupt_review_event_and_cli(tmp_path, capsys):
    store, _, _, run = source(tmp_path)
    assert main(["--store", str(tmp_path), "packet", run["run_id"]]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "--store",
                str(tmp_path),
                "submit",
                run["run_id"],
                "--expected-head",
                "none",
                "--action",
                "accept",
                "--reviewer",
                "Test reviewer",
                "--rationale",
                "Fixture",
            ]
        )
        == 0
    )
    event = json.loads(capsys.readouterr().out)
    (store.root / "objects" / event["event_sha256"]).write_text("{}")
    with pytest.raises(IngestionError, match="hash"):
        export_marketing(store, run["run_id"])


@pytest.mark.parametrize("field", ["reviewer", "rationale"])
def test_missing_review_identity_or_reason(tmp_path, field):
    store, _, _, run = source(tmp_path)
    values = {"reviewer": "Test reviewer", "rationale": "Fixture only"}
    values[field] = "   "
    with pytest.raises(ValueError):
        submit(store, run["run_id"], expected_head=None, action="accept", **values)
    assert history(store, run["run_id"])["head"] is None


def test_source_corruption_blocks_existing_approval(tmp_path):
    store, _, items, run = source(tmp_path)
    decide(store, run)
    (store.root / "objects" / items[0]["snapshot_id"]).write_text("{}")
    with pytest.raises(IngestionError):
        export_marketing(store, run["run_id"])
