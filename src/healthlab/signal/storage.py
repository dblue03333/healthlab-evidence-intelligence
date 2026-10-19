"""Collector storage boundary and recoverable local state.

Cloud implementations must supply create-only hash objects, transactional state
and a shared lock/lease. A mounted object bucket is not a local-state adapter.
"""

import fcntl
import json
import os
import tempfile
from contextlib import AbstractContextManager, contextmanager
from typing import Protocol

from healthlab.models import IngestionError
from healthlab.store import RunStore, json_bytes, sync_directory


class ObjectStore(Protocol):
    def put(self, content: bytes) -> str:
        """Create immutable SHA-256 object; verify existing content on collision."""
        ...

    def read(self, digest: str) -> bytes:
        """Read and verify SHA-256; fail on missing or corrupt content."""
        ...


class CollectionState(Protocol):
    def new(self, mode: str, question: dict) -> dict: ...
    def load(self, run_id: str) -> dict: ...
    def save(self, run: dict) -> None: ...
    def load_index(self) -> dict: ...
    def commit(self, run: dict, index: dict) -> None:
        """Publish run and document index together, under lock()."""
        ...

    def lock(self) -> AbstractContextManager[None]:
        """Serialize state writers; recover any interrupted commit before yielding."""
        ...


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class LocalCollectionState:
    """Existing file layout, with a write-ahead journal for run/index commits.

    All methods are called under lock(). An interrupted commit rolls forward
    before any collector or newsletter acquires that same lock.
    """

    def __init__(self, store: RunStore):
        self.store = store
        self.root = store.root

    def new(self, mode, question):
        return self.store.new(mode, question)

    def load(self, run_id):
        return self.store.load(run_id)

    def save(self, run):
        self.store.save(run)

    def load_index(self):
        path = self.root / "signal-index.json"
        try:
            index = json.loads(path.read_bytes()) if path.exists() else {}
        except ValueError as exc:
            raise IngestionError("collection_index_invalid", "Invalid collection index") from exc
        if not isinstance(index, dict):
            raise IngestionError("collection_index_invalid", "Invalid collection index")
        return index

    def commit(self, run, index):
        # Objects are durable before this pointer is published. Recovery verifies
        # the bundle's hash before replaying the two mutable file writes.
        digest = self.store.put(json_bytes({"run": run, "index": index}))
        atomic_json(self.root / "collection-pending.json", {"bundle_sha256": digest})
        self._recover()

    def _recover(self):
        path = self.root / "collection-pending.json"
        if not path.exists():
            return
        try:
            pointer = json.loads(path.read_bytes())
            bundle = json.loads(self.store.read(pointer["bundle_sha256"]))
            run, index = bundle["run"], bundle["index"]
            # Validate run identity using the existing manifest before any write.
            self.store.load(run["run_id"])
            if not isinstance(index, dict) or run.get("mode") != "signal_collection":
                raise ValueError("Invalid commit")
        except (KeyError, TypeError, ValueError) as exc:
            raise IngestionError("collection_journal_invalid", "Invalid pending commit") from exc
        self.save(run)
        atomic_json(self.root / "signal-index.json", index)
        path.unlink()
        sync_directory(self.root)

    @contextmanager
    def lock(self):
        with (self.root / "signal.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self._recover()
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


class CollectionStorage:
    """Compose independent artifact and state adapters; no filesystem required."""

    def __init__(self, objects: ObjectStore, state: CollectionState):
        self.objects = objects
        self.state = state

    def put(self, content):
        return self.objects.put(content)

    def read(self, digest):
        return self.objects.read(digest)

    def new(self, mode, question):
        return self.state.new(mode, question)

    def save(self, run):
        self.state.save(run)

    def load(self, run_id):
        return self.state.load(run_id)


def collection_storage(store):
    if isinstance(store, CollectionStorage):
        return store
    if isinstance(store, RunStore):
        return CollectionStorage(store, LocalCollectionState(store))
    raise TypeError("Expected RunStore or CollectionStorage")
