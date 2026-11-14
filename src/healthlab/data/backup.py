"""Verified external snapshots. Keep every snapshot; never copy credentials."""

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from healthlab.common import configure_logging
from healthlab.data.datasets import build_dataset, read_dataset
from healthlab.data.pilot import runner_lock
from healthlab.data.power import awake_for_cli
from healthlab.signal.backup import export_collection
from healthlab.signal.storage import collection_storage
from healthlab.store import RunStore, json_bytes, sync_directory

EXCLUDED = {".env", ".git", ".venv", "__pycache__", ".DS_Store"}


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def verify_snapshot(path):
    path = Path(path).resolve()
    manifest = json.loads((path / "snapshot.json").read_bytes())
    if any(p.is_symlink() for p in path.rglob("*")):
        raise ValueError("Snapshot refuses symlinks")
    expected = set()
    for row in manifest["files"]:
        relative = Path(row["path"])
        target = path / relative
        if relative.is_absolute() or ".." in relative.parts or target.is_symlink():
            raise ValueError("Unsafe snapshot path")
        if not target.resolve().is_relative_to(path):
            raise ValueError("Snapshot path escapes root")
        if target.stat().st_size != row["bytes"] or digest(target) != row["sha256"]:
            raise ValueError("Snapshot integrity mismatch")
        expected.add(row["path"])
    actual = {str(p.relative_to(path)) for p in path.rglob("*") if p.is_file()}
    if actual != expected | {"snapshot.json"}:
        raise ValueError("Snapshot file inventory differs")
    return {"verified": True, "files": len(expected), "datasets": manifest["datasets"]}


def create_snapshot(store, parent, *, project=None, require_independent=True):
    root = store.root.resolve()
    parent = Path(parent).resolve()
    # Refuse a missing volume/path rather than accidentally backing up on root disk.
    if not parent.is_dir() or parent.is_relative_to(root) or root.is_relative_to(parent):
        raise ValueError("Choose an existing external backup directory outside the store")
    if require_independent and parent.stat().st_dev == root.stat().st_dev:
        raise ValueError("Backup must be on an independent mounted device")
    estimated = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    if shutil.disk_usage(parent).free < max(10 * 1024**3, 2 * estimated):
        raise ValueError("Insufficient external capacity; snapshots are never auto-deleted")
    stage = Path(tempfile.mkdtemp(prefix=".incomplete-", dir=parent))
    destination = parent / (
        "snapshot-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    )
    inventory = []

    def copy_tree(source, target):
        for source_file in sorted(source.rglob("*")):
            relative = source_file.relative_to(source)
            if any(p in EXCLUDED or p.startswith(".env.") for p in relative.parts):
                continue
            if source_file.is_symlink():
                raise ValueError("Snapshot refuses symlinks")
            if not source_file.is_file():
                continue
            output = target / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            before = digest(source_file)
            shutil.copy2(source_file, output)
            if digest(output) != before or digest(source_file) != before:
                raise ValueError("File changed during snapshot")
            with output.open("rb") as handle:
                os.fsync(handle.fileno())
            inventory.append(
                {
                    "path": str(output.relative_to(stage)),
                    "bytes": output.stat().st_size,
                    "sha256": before,
                }
            )

    storage = collection_storage(store)
    with runner_lock(root), storage.state.lock():
        pending = [
            p
            for p in (root / "pilot").glob("*/*/*/journal.json")
            if json.loads(p.read_bytes())["status"] != "completed"
        ]
        if pending:
            raise ValueError("Finish the pending pilot before snapshotting")
        copy_tree(root, stage / "store")
    if project:
        project = Path(project).resolve()
        for name in ("src", "config", "tests", "scripts", "reports"):
            copy_tree(project / name, stage / "project" / name)
        metadata = stage / ".metadata"
        metadata.mkdir()
        for name in (
            "pyproject.toml",
            "uv.lock",
            "README.md",
            "DATA_PIPELINE.md",
            "HANDOFF_DATA_PIPELINE.md",
            ".gitignore",
            ".python-version",
        ):
            if (project / name).is_file():
                shutil.copy2(project / name, metadata / name)
        copy_tree(metadata, stage / "project")
        shutil.rmtree(metadata)
    # Read only the copied store for release checks and a portable restore/rebuild.
    copied = RunStore(stage / "store")
    datasets = {}
    for path in (copied.root / "runs").glob("*/manifest.json"):
        row = json.loads(path.read_bytes())
        if row.get("mode") == "signal_dataset" and row["status"] == "completed":
            datasets[row["dataset_sha256"]] = row["collection_id"]
    verified = []
    for identifier, collection in sorted(datasets.items()):
        _, _, _, gold = read_dataset(copied, identifier)
        verified.append(
            {
                "dataset_id": identifier,
                "collection_id": collection,
                "articles": len(gold.document_ids),
                "status": gold.status,
            }
        )
    journals = [
        json.loads(p.read_bytes()) for p in (copied.root / "pilot").glob("*/*/*/journal.json")
    ]
    latest = max(journals, key=lambda r: r["created_at"], default=None)
    restored = None
    if latest:
        with tempfile.TemporaryDirectory(prefix="healthlab-restore-") as temporary:
            destination_store = Path(temporary) / "collection"
            run = copied.load(latest.get("repair_collection_id") or latest["collection_id"])
            export_collection(copied, run["run_id"], destination_store)
            restored = build_dataset(RunStore(destination_store), run["run_id"])
            if restored["dataset_id"] != latest["dataset_id"]:
                raise ValueError("Restored dataset identity differs")
    proof = {
        "version": "data-snapshot-1",
        "created_at": datetime.now(UTC).isoformat(),
        "files": inventory,
        "datasets": verified,
        "latest_restored": restored,
        "independent_device": parent.stat().st_dev != root.stat().st_dev,
        "retention": "keep_all_no_automatic_deletion",
        "credentials_copied": False,
    }
    with (stage / "snapshot.json").open("xb") as handle:
        handle.write(json_bytes(proof))
        handle.flush()
        os.fsync(handle.fileno())
    verification = verify_snapshot(stage)
    # Flush directory entries before publishing the completed snapshot name.
    for directory in sorted(
        (p for p in stage.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True
    ):
        sync_directory(directory)
    sync_directory(stage)
    os.rename(stage, destination)
    sync_directory(parent)
    return dict(verification, destination=str(destination), latest_restored=restored)


def main(argv=None):
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=Path("outputs/data_20261026"))
    parser.add_argument("--parent", type=Path)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args(argv)
    if not args.verify and not args.parent:
        parser.error("Choose --parent or --verify")
    with awake_for_cli():
        result = (
            verify_snapshot(args.verify)
            if args.verify
            else create_snapshot(RunStore(args.store), args.parent, project=args.project)
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
