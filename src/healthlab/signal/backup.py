"""Portable, verified collection bundles. No credentials or unrelated runs copied."""

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from healthlab.models import IngestionError
from healthlab.signal.storage import collection_storage
from healthlab.store import json_bytes, sync_directory

REFERENCE_KEYS = {
    "raw_sha256",
    "diagnostic_sha256",
    "raw_record_sha256",
    "extraction_sha256",
    "rules_sha256",
    "snapshot_id",
    "previous_snapshot_id",
    "checkpoint_sha256",
    "document_sha256",
    "collection_manifest_sha256",
    "replay_of_manifest_sha256",
    "repair_of_manifest_sha256",
    "bronze_sha256",
    "silver_sha256",
    "gold_sha256",
    "dataset_sha256",
}


def references(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in REFERENCE_KEYS and item is not None:
                if not isinstance(item, str) or not re.fullmatch(r"[a-f0-9]{64}", item):
                    raise ValueError("Invalid artifact reference")
                yield item
            else:
                yield from references(item)
    elif isinstance(value, list):
        for item in value:
            yield from references(item)


def object_references(content):
    try:
        return list(references(json.loads(content)))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []


def _write(path, content):
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def export_collection(store, run_id, destination):
    """Export one HTML collection, including all referenced objects, under a lock.

    The resulting directory is itself a usable local store. Bundle inventory
    detects accidental damage; its hash is not a signature/authentication proof.
    """
    storage = collection_storage(store)
    destination = Path(destination).resolve()
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError("Choose a new bundle directory under an existing parent")
    temporary = Path(tempfile.mkdtemp(prefix=".collection-export-", dir=destination.parent))
    try:
        with storage.state.lock():
            run = storage.load(run_id)
            if run.get("mode") != "signal_collection" or any(
                s["enabled"] and s["kind"] != "html" for s in run["registry"]["sources"]
            ):
                raise ValueError("Portable export currently supports HTML collection only")
            pending, objects = list(references(run)), set()
            (temporary / "objects").mkdir()
            while pending:
                digest = pending.pop()
                if digest in objects:
                    continue
                content = storage.read(digest)
                _write(temporary / "objects" / digest, content)
                objects.add(digest)
                pending.extend(object_references(content))
            directory = temporary / "runs" / run_id
            directory.mkdir(parents=True)
            manifest = json_bytes(run)
            _write(directory / "manifest.json", manifest)
            index = {
                ref["source_id"] + ":" + ref["document_id"]: ref
                | {
                    "collected_at": run.get("observed_at", run["created_at"]),
                    "run_id": run_id,
                }
                for ref in run["documents"]
            }
            index_content = json_bytes(index)
            _write(temporary / "signal-index.json", index_content)
            inventory = {
                "schema_version": "collection-bundle-1",
                "run_id": run_id,
                "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
                "index_sha256": hashlib.sha256(index_content).hexdigest(),
                "objects": sorted(objects),
            }
            _write(temporary / "bundle.json", json_bytes(inventory))
            verify_bundle(temporary)
            for folder in (directory, directory.parent, temporary / "objects", temporary):
                sync_directory(folder)
            os.rename(temporary, destination)
            sync_directory(destination.parent)
        return {
            "bundle": str(destination),
            "run_id": run_id,
            "objects": len(objects),
            "verified": True,
        }
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def verify_bundle(directory):
    """Offline hash and reference-closure verification, without mutating the bundle."""
    root = Path(directory)
    inventory = json.loads((root / "bundle.json").read_bytes())
    run_id = inventory["run_id"]
    if inventory.get("schema_version") != "collection-bundle-1" or not re.fullmatch(
        r"[a-f0-9]{32}", run_id
    ):
        raise ValueError("Unsupported bundle")
    manifest = (root / "runs" / run_id / "manifest.json").read_bytes()
    index = (root / "signal-index.json").read_bytes()
    if (
        hashlib.sha256(manifest).hexdigest() != inventory["manifest_sha256"]
        or hashlib.sha256(index).hexdigest() != inventory["index_sha256"]
    ):
        raise IngestionError("bundle_corrupt", "Manifest or index hash mismatch")
    run = json.loads(manifest)
    if run.get("run_id") != run_id or run.get("mode") != "signal_collection":
        raise ValueError("Invalid collection identity")
    objects = set(inventory["objects"])
    needed = set(references(run)) | set(references(json.loads(index)))
    for digest in objects:
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid bundle object identity")
        content = (root / "objects" / digest).read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise IngestionError("bundle_corrupt", "Object hash mismatch")
        needed.update(object_references(content))
    if not needed <= objects:
        raise IngestionError("bundle_incomplete", "Bundle lacks referenced objects")
    return {"run_id": run_id, "objects": len(objects), "verified": True}
