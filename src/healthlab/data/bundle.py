"""Portable dataset export and a readable local review report."""

import html
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import cast

from healthlab.common import JsonObject
from healthlab.data.contracts import Bronze, Digest, Gold, NewsletterObservation, Silver
from healthlab.data.datasets import read_dataset, summary, verify_objects
from healthlab.models import Contract
from healthlab.signal.backup import _write
from healthlab.signal.storage import collection_storage
from healthlab.store import RunStore, json_bytes, sync_directory


class Bundle(Contract):
    schema_version: str
    dataset_sha256: Digest
    objects: list[Digest]


def render_report(dataset_id: str, bronze: Bronze, silver: Silver, gold: Gold) -> str:
    newsletter = gold.window.basis == "configured_source_calendar_date"

    def esc(value):
        return html.escape(str(value), quote=True)

    rows = []
    for record in silver.records:
        if record.outcome == "outside_window":
            continue
        if newsletter:  # A newsletter dataset stores newsletter observations.
            shown = cast(NewsletterObservation, record)
            day, basis = shown.selection_date, f"<br>{esc(shown.selection_date_basis)}"
        else:
            day, basis = record.publication_date, ""
        rows.append(
            f"<tr><td>{esc(record.source_id)}</td><td>{esc(record.outcome)}</td>"
            f"<td>{esc(day or 'unknown')}" + basis + "</td>"
            f'<td><a href="{esc(record.url)}">{esc(record.title or record.url)}</a>'
            f"<details><summary>Saved text and provenance</summary><pre>{esc(record.body or 'No readable text')}</pre>"
            f"<pre>{esc(json.dumps(record.model_dump(mode='json', exclude={'body'}), ensure_ascii=False, indent=2))}</pre>"
            "</details></td></tr>"
        )
    qualities = "".join(
        f"<tr><td>{esc(q.organization)}</td><td>{esc(q.coverage_complete)}</td>"
        f"<td>{q.candidates}</td><td>{esc(json.dumps(q.outcomes, ensure_ascii=False))}</td>"
        f"<td>{q.recovered_from_history}</td><td><details><summary>{len(q.issues)} issues</summary>"
        f"<pre>{esc(json.dumps(q.issues, ensure_ascii=False, indent=2))}</pre></details></td></tr>"
        for q in silver.sources
    )
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        "<title>HealthLab data release</title><style>"
        "body{font:16px system-ui;max-width:1300px;margin:32px auto;padding:0 20px;color:#172b35;background:#f8fafb}"
        "table{width:100%;border-collapse:collapse;background:white}th,td{padding:12px;border:1px solid #dce3e7;text-align:left;vertical-align:top}"
        "pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}a{color:#006c70}details{margin-top:12px}code{overflow-wrap:anywhere}"
        "</style><h1>HealthLab · Data release</h1>"
        f"<p><strong>{esc(gold.status.upper())}</strong> · {gold.window.mindate} → {gold.window.maxdate} inclusive · "
        f"{len(gold.document_ids)} readable articles · {esc(bronze.capture_kind)}</p>"
        f"<p>Dataset: <code>{esc(dataset_id)}</code><br>Collection: <code>{esc(bronze.collection_id)}</code></p>"
        + (
            "<p>Selection dates follow each source policy: HCMC uses its displayed date (listing, then article update, then publication). Original publication remains separate. Coverage concerns the configured captured inventories; "
            if newsletter
            else "<p>Publication dates as supplied by publishers. Coverage concerns the configured captured inventories; "
        )
        + "independent recall and editorial relevance have not been evaluated. Media-only pages are outside the text scope.</p>"
        "<h2>Source coverage</h2><table><tr><th>Source</th><th>Complete scope</th><th>Candidates</th><th>Outcomes</th>"
        f"<th>Recovered links</th><th>Issues</th></tr>{qualities}</table>"
        "<h2>Articles and unresolved cases</h2><p>Outside-window observations remain in Silver and are omitted from this table.</p>"
        f"<table><tr><th>Source</th><th>Outcome</th><th>{'Selection date / basis' if newsletter else 'Publication date'}</th><th>Article</th></tr>{''.join(rows)}</table></html>"
    )


def export_dataset(store: RunStore, dataset_id: str, destination: Path) -> JsonObject:
    storage = collection_storage(store)
    _, bronze, silver, gold = read_dataset(store, dataset_id)
    destination = Path(destination).resolve()
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError("Choose a new export directory under an existing parent")
    temporary = Path(tempfile.mkdtemp(prefix=".dataset-export-", dir=destination.parent))
    try:
        objects = verify_objects(storage, dataset_id)
        (temporary / "objects").mkdir()
        (temporary / "runs").mkdir()
        for ref in sorted(objects):
            _write(temporary / "objects" / ref, storage.read(ref))
        inventory = Bundle(
            schema_version="dataset-bundle-1", dataset_sha256=dataset_id, objects=sorted(objects)
        )
        _write(temporary / "dataset-bundle.json", json_bytes(inventory.model_dump()))
        _write(temporary / "report.html", render_report(dataset_id, bronze, silver, gold).encode())
        _write(temporary / "summary.json", json_bytes(summary(dataset_id, bronze, silver, gold)))
        # The explicit status prevents an exported partial list from appearing
        # to be an accepted full-window input to an unsuspecting consumer.
        selected = set(gold.document_ids)
        _write(
            temporary / "gold.json",
            json_bytes(
                {
                    "dataset_id": dataset_id,
                    "status": gold.status,
                    "window": gold.window.model_dump(mode="json"),
                    "documents": [
                        r.model_dump(mode="json")
                        for r in silver.records
                        if r.document_id in selected
                    ],
                }
            ),
        )
        verify_export(temporary)
        for folder in (temporary / "objects", temporary / "runs", temporary):
            sync_directory(folder)
        os.rename(temporary, destination)
        sync_directory(destination.parent)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return {
        "dataset_id": dataset_id,
        "status": gold.status,
        "bundle": str(destination),
        "objects": len(objects),
        "verified": True,
    }


def verify_export(directory: Path) -> JsonObject:
    directory = Path(directory)
    inventory = Bundle.model_validate_json((directory / "dataset-bundle.json").read_bytes())
    if inventory.schema_version != "dataset-bundle-1":
        raise ValueError("Unsupported dataset bundle version")
    # Read-only adapter: verification must not create or repair any directory.
    store = object.__new__(RunStore)
    store.root = directory
    storage = collection_storage(store)
    objects = verify_objects(storage, inventory.dataset_sha256)
    if objects != set(inventory.objects) or len(objects) != len(inventory.objects):
        raise ValueError("Dataset object inventory is incomplete or duplicated")
    _, bronze, silver, gold = read_dataset(store, inventory.dataset_sha256)
    expected_summary = summary(inventory.dataset_sha256, bronze, silver, gold)
    selected = set(gold.document_ids)
    expected_gold = {
        "dataset_id": inventory.dataset_sha256,
        "status": gold.status,
        "window": gold.window.model_dump(mode="json"),
        "documents": [
            r.model_dump(mode="json") for r in silver.records if r.document_id in selected
        ],
    }
    if (
        (directory / "summary.json").read_bytes() != json_bytes(expected_summary)
        or (directory / "gold.json").read_bytes() != json_bytes(expected_gold)
        or (directory / "report.html").read_text()
        != render_report(inventory.dataset_sha256, bronze, silver, gold)
    ):
        raise ValueError("Derived export differs from its verified dataset")
    return {
        "dataset_id": inventory.dataset_sha256,
        "status": gold.status,
        "objects": len(objects),
        "verified": True,
    }
