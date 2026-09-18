"""Content-addressed immutable objects plus one manifest per invocation."""

import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path

from healthlab.models import MANIFEST_VERSION, PARSER_VERSION, IngestionError
from healthlab.pubmed import utc_now


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


class RunStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        (self.root / "objects").mkdir(parents=True, exist_ok=True)
        (self.root / "runs").mkdir(exist_ok=True)

    def put(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        path = self.root / "objects" / digest
        if path.exists():
            if path.read_bytes() != content:
                raise IngestionError("cache_corrupt", "Existing object failed integrity check")
            return digest
        fd, tmp = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp, path)  # Atomic create; never overwrite a shared object.
            except FileExistsError:
                if path.read_bytes() != content:
                    raise IngestionError("cache_corrupt", "Concurrent object mismatch")
        finally:
            os.unlink(tmp)
        return digest

    def read(self, digest: str) -> bytes:
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise IngestionError("invalid_reference", "Invalid object digest")
        try:
            content = (self.root / "objects" / digest).read_bytes()
        except FileNotFoundError as exc:
            raise IngestionError("cache_missing", "Referenced object is missing") from exc
        if hashlib.sha256(content).hexdigest() != digest:
            raise IngestionError("cache_corrupt", "Object hash does not match content")
        return content

    def new(self, mode: str, question: dict) -> dict:
        run_id = uuid.uuid4().hex
        (self.root / "runs" / run_id).mkdir()
        run = {
            "manifest_version": MANIFEST_VERSION,
            "parser_version": PARSER_VERSION,
            "run_id": run_id,
            "mode": mode,
            "created_at": utc_now(),
            "status": "running",
            "question": question,
            "stage": "created",
            "requests": [],
            "documents": [],
            "errors": [],
            "warnings": [],
        }
        self.save(run)
        return run

    def save(self, run: dict):
        directory = self.root / "runs" / run["run_id"]
        fd, tmp = tempfile.mkstemp(dir=directory)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(json_bytes(run))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, directory / "manifest.json")
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def load(self, run_id: str) -> dict:
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise IngestionError("invalid_run_id", "Expected a 32-character run ID")
        try:
            result = json.loads((self.root / "runs" / run_id / "manifest.json").read_bytes())
        except (FileNotFoundError, ValueError) as exc:
            raise IngestionError("invalid_run", "Run manifest missing or invalid") from exc
        if not isinstance(result, dict):
            raise IngestionError("invalid_run", "Run manifest must be an object")
        if result.get("manifest_version") != MANIFEST_VERSION:
            raise IngestionError("manifest_version", "Unsupported manifest version")
        if (
            result.get("run_id") != run_id
            or not isinstance(result.get("question"), dict)
            or not isinstance(result.get("requests"), list)
        ):
            raise IngestionError("invalid_run", "Run manifest lacks required identity or fields")
        return result
