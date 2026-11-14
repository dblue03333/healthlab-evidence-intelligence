"""Opt-in GCS artifacts and PostgreSQL collection state; no provisioning.

Use a direct/session PostgreSQL connection, never transaction pooling: writer
serialization uses a session advisory lock. Losing that connection aborts work.
"""

import hashlib
import json
import re
import uuid
from contextlib import contextmanager

from healthlab.common import utc_now
from healthlab.models import MANIFEST_VERSION, IngestionError
from healthlab.signal.storage import CollectionStorage
from healthlab.store import json_bytes

DDL = (
    "CREATE TABLE IF NOT EXISTS healthlab_signal_storage ("
    "namespace text PRIMARY KEY, schema_version integer NOT NULL, object_location text NOT NULL)",
    "CREATE TABLE IF NOT EXISTS healthlab_signal_runs ("
    "namespace text NOT NULL, run_id text NOT NULL CHECK (run_id ~ '^[a-f0-9]{32}$'), "
    "manifest jsonb NOT NULL, PRIMARY KEY(namespace, run_id), "
    "FOREIGN KEY(namespace) REFERENCES healthlab_signal_storage(namespace))",
    "CREATE TABLE IF NOT EXISTS healthlab_signal_documents ("
    "namespace text NOT NULL, document_key text NOT NULL, reference jsonb NOT NULL, "
    "PRIMARY KEY(namespace, document_key), "
    "FOREIGN KEY(namespace) REFERENCES healthlab_signal_storage(namespace))",
)


class GCSObjectStore:
    def __init__(self, bucket, prefix="healthlab/signal"):
        # Import only for the optional cloud backend.
        from google.api_core.exceptions import NotFound, PreconditionFailed

        if not prefix.strip("/") or not re.fullmatch(r"[a-zA-Z0-9_/-]+", prefix) or ".." in prefix:
            raise ValueError("Invalid object prefix")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.not_found = NotFound
        self.precondition_failed = PreconditionFailed

    def _blob(self, digest):
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise IngestionError("invalid_reference", "Invalid object digest")
        return self.bucket.blob(f"{self.prefix}/objects/{digest}")

    def put(self, content):
        digest = hashlib.sha256(content).hexdigest()
        try:
            self._blob(digest).upload_from_string(
                content,
                content_type="application/octet-stream",
                if_generation_match=0,
                timeout=30,
                retry=None,
            )
        except self.precondition_failed:
            if self.read(digest) != content:
                raise IngestionError("cache_corrupt", "Existing cloud object differs") from None
        except Exception as exc:
            raise IngestionError("cloud_object_io", "Cloud object upload failed") from exc
        return digest

    def read(self, digest):
        blob = self._blob(digest)
        try:
            content = blob.download_as_bytes(timeout=30, retry=None)
        except self.not_found as exc:
            raise IngestionError("cache_missing", "Referenced cloud object is missing") from exc
        except Exception as exc:
            raise IngestionError("cloud_object_io", "Cloud object download failed") from exc
        if hashlib.sha256(content).hexdigest() != digest:
            raise IngestionError("cache_corrupt", "Cloud object hash mismatch")
        return content


class PostgresCollectionState:
    def __init__(self, dsn, namespace, object_location):
        import psycopg

        if not re.fullmatch(r"[a-z0-9_-]{1,64}", namespace):
            raise ValueError("Invalid storage namespace")
        self.dsn = dsn
        self.namespace = namespace
        self.object_location = object_location
        self.driver = psycopg
        self.connection = None
        self.lock_key = int.from_bytes(
            hashlib.sha256(namespace.encode()).digest()[:8], "big", signed=True
        )

    @contextmanager
    def _safe_database(self):
        try:
            yield
        except self.driver.Error as exc:
            raise IngestionError("cloud_state_io", "PostgreSQL operation failed") from exc

    def _connect(self):
        return self.driver.connect(self.dsn, autocommit=True, connect_timeout=10)

    def list_runs(self, mode):
        with self._safe_database(), self._connect() as connection:
            self._check_schema(connection)
            return [
                row[0]
                for row in connection.execute(
                    "SELECT run_id FROM healthlab_signal_runs "
                    "WHERE namespace=%s AND manifest->>'mode'=%s ORDER BY run_id",
                    (self.namespace, mode),
                )
            ]

    def _check_schema(self, connection):
        row = connection.execute(
            "SELECT schema_version, object_location FROM healthlab_signal_storage WHERE namespace=%s",
            (self.namespace,),
        ).fetchone()
        if row != (1, self.object_location):
            raise IngestionError("cloud_schema", "Initialize the matching storage namespace first")

    @contextmanager
    def lock(self):
        if self.connection is not None:
            raise ValueError("Storage instances cannot share nested/concurrent writer sessions")
        with self._safe_database(), self._connect() as connection:
            acquired = connection.execute(
                "SELECT pg_try_advisory_lock(%s)", (self.lock_key,)
            ).fetchone()[0]
            if not acquired:
                raise IngestionError("collection_busy", "Another collector holds this namespace")
            self.connection = connection
            try:
                self._check_schema(connection)
                yield
            finally:
                # Closing the session releases the lock, even after connection failure.
                self.connection = None

    def initialize(self):
        """Explicit, additive schema bootstrap. Never called by collect()."""
        with self._safe_database(), self._connect() as connection:
            with connection.transaction():
                # Serialize DDL across all namespaces as well as namespace setup.
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (7183051274001,))
                for statement in DDL:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO healthlab_signal_storage VALUES (%s, 1, %s) ON CONFLICT DO NOTHING",
                    (self.namespace, self.object_location),
                )
                self._check_schema(connection)

    def _writer(self):
        if self.connection is None:
            raise ValueError("State mutations require the writer lock")
        return self.connection

    def new(self, mode, question):
        run = {
            "manifest_version": MANIFEST_VERSION,
            "parser_version": "pending",
            "run_id": uuid.uuid4().hex,
            "mode": mode,
            "question": question,
            "created_at": utc_now(),
            "status": "running",
            "stage": "created",
            "requests": [],
            "documents": [],
            "errors": [],
            "warnings": [],
        }
        with self._safe_database():
            self._writer().execute(
                "INSERT INTO healthlab_signal_runs VALUES (%s, %s, %s::jsonb)",
                (self.namespace, run["run_id"], json_bytes(run).decode()),
            )
        return run

    def load(self, run_id):
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise IngestionError("invalid_run_id", "Invalid run ID")
        with self._safe_database():
            if self.connection is not None:
                row = self.connection.execute(
                    "SELECT manifest FROM healthlab_signal_runs WHERE namespace=%s AND run_id=%s",
                    (self.namespace, run_id),
                ).fetchone()
            else:
                with self._connect() as connection:
                    self._check_schema(connection)
                    row = connection.execute(
                        "SELECT manifest FROM healthlab_signal_runs WHERE namespace=%s AND run_id=%s",
                        (self.namespace, run_id),
                    ).fetchone()
        if row is None:
            raise IngestionError("invalid_run", "Run not found")
        run = row[0]
        if run.get("run_id") != run_id or run.get("manifest_version") != MANIFEST_VERSION:
            raise IngestionError("invalid_run", "Run identity/version mismatch")
        return run

    def save(self, run):
        with self._safe_database():
            result = self._writer().execute(
                "UPDATE healthlab_signal_runs SET manifest=%s::jsonb WHERE namespace=%s AND run_id=%s",
                (json.dumps(run, ensure_ascii=False), self.namespace, run["run_id"]),
            )
            if result.rowcount != 1:
                raise IngestionError("invalid_run", "Cannot update missing run")

    def load_index(self):
        with self._safe_database():
            rows = (
                self._writer()
                .execute(
                    "SELECT document_key, reference FROM healthlab_signal_documents WHERE namespace=%s",
                    (self.namespace,),
                )
                .fetchall()
            )
            return dict(rows)

    def commit(self, run, index):
        with self._safe_database(), self._writer().transaction():
            self.save(run)
            with self._writer().cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO healthlab_signal_documents VALUES (%s, %s, %s::jsonb) "
                    "ON CONFLICT(namespace, document_key) DO UPDATE SET reference=EXCLUDED.reference",
                    [
                        (self.namespace, key, json.dumps(value, ensure_ascii=False))
                        for key, value in index.items()
                    ],
                )

    def install(self, run, index):
        """Import an exact run and merge its index in one transaction, without overwrite."""
        with self._safe_database(), self._writer().transaction():
            found = (
                self._writer()
                .execute(
                    "SELECT manifest FROM healthlab_signal_runs WHERE namespace=%s AND run_id=%s",
                    (self.namespace, run["run_id"]),
                )
                .fetchone()
            )
            if found and found[0] != run:
                raise IngestionError("migration_conflict", "Destination run differs")
            if not found:
                self._writer().execute(
                    "INSERT INTO healthlab_signal_runs VALUES (%s, %s, %s::jsonb)",
                    (self.namespace, run["run_id"], json.dumps(run, ensure_ascii=False)),
                )
            current = self.load_index()
            for key, reference in index.items():
                previous = current.get(key)
                if not previous or previous.get("collected_at", "") < reference["collected_at"]:
                    current[key] = reference
            self.commit(run, current)


def import_bundle(storage, directory):
    """Verify locally, upload/verify all objects, then atomically publish PG state."""
    from pathlib import Path

    from healthlab.signal.backup import verify_bundle

    if not isinstance(storage.state, PostgresCollectionState):
        raise ValueError("Import requires the PostgreSQL state adapter")
    directory = Path(directory)
    inventory = verify_bundle(directory)
    bundle = json.loads((directory / "bundle.json").read_bytes())
    run = json.loads((directory / "runs" / inventory["run_id"] / "manifest.json").read_bytes())
    index = json.loads((directory / "signal-index.json").read_bytes())
    with storage.state.lock():
        for digest in bundle["objects"]:
            content = (directory / "objects" / digest).read_bytes()
            if storage.put(content) != digest or storage.read(digest) != content:
                raise IngestionError("migration_integrity", "Transferred object differs")
        storage.state.install(run, index)
    return {"run_id": run["run_id"], "objects": len(bundle["objects"]), "verified": True}


def cloud_collection_storage(dsn, bucket_name, *, prefix="healthlab/signal", namespace="signal"):
    try:
        import psycopg  # noqa: F401
        from google.cloud import storage
    except ImportError as exc:
        raise IngestionError("cloud_dependencies", "Install the cloud extra first") from exc

    try:
        bucket = storage.Client().bucket(bucket_name)
    except Exception as exc:
        raise IngestionError("cloud_credentials", "Cloud credentials are unavailable") from exc
    objects = GCSObjectStore(bucket, prefix)
    state = PostgresCollectionState(dsn, namespace, f"gs://{bucket_name}/{objects.prefix}")
    return CollectionStorage(objects, state)


def storage_from_config(path):
    import os

    from pydantic import Field

    from healthlab.models import Contract

    class Config(Contract):
        backend: str = Field(pattern="^gcs_postgres$")
        bucket: str = Field(min_length=3, max_length=222, pattern=r"^[a-z0-9][a-z0-9._-]+[a-z0-9]$")
        prefix: str = "healthlab/signal"
        namespace: str = "signal"
        database_url_env: str = Field(
            default="HEALTHLAB_SIGNAL_DATABASE_URL", pattern=r"^[A-Z][A-Z0-9_]*$"
        )

    config = Config.model_validate_json(path.read_bytes())
    dsn = os.environ.get(config.database_url_env)
    if not dsn:
        raise IngestionError("cloud_configuration", "Database environment variable is absent")
    return cloud_collection_storage(
        dsn, config.bucket, prefix=config.prefix, namespace=config.namespace
    )
