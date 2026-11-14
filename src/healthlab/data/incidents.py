"""Local supervision ledger: immutable states, one atomic pointer, auditable human actions.

All writers use the collector lock, including backups. No network or notification
transport is hidden here. Acknowledging a finding never changes the captured data.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from healthlab.common import JsonObject
from healthlab.signal.storage import atomic_json, collection_storage
from healthlab.store import RunStore, json_bytes

VERSION = "healthlab-incidents-1"


def digest(value: JsonObject) -> str:
    return hashlib.sha256(json_bytes(value)).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


def incident_id(finding: JsonObject) -> str:
    # Rules/budgets/windows may change during a repair. They must not make an
    # unresolved problem disappear. The full signature is retained in evidence.
    return "INC-" + digest({k: finding[k] for k in ("source_id", "code", "entity")})


def _pointer(store: RunStore) -> Path:
    if not isinstance(store, RunStore):
        raise ValueError("Incident supervision currently requires a local RunStore")
    return store.root / "supervision" / "current.json"


def _load(store: RunStore) -> JsonObject:
    path = _pointer(store)
    if not path.exists():
        return {"schema_version": VERSION, "incidents": {}, "observations": [], "events": []}
    pointer = json.loads(path.read_bytes())
    if pointer["schema_version"] != VERSION:
        raise ValueError("Unsupported incident pointer")
    state = json.loads(store.read(pointer["state_sha256"]))
    if state["schema_version"] != VERSION:
        raise ValueError("Unsupported incident ledger")
    return state


def _commit(store: RunStore, state: JsonObject) -> None:
    path = _pointer(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = json.loads(path.read_bytes())["state_sha256"] if path.exists() else None
    state["previous_state_sha256"] = previous
    ref = store.put(json_bytes(state))
    atomic_json(path, {"schema_version": VERSION, "state_sha256": ref})


def snapshot(store: RunStore) -> JsonObject:
    """Return a verified consistent snapshot; corruption fails loudly."""
    with collection_storage(store).state.lock():
        return _load(store)


def record_assessment(store: RunStore, assessment: JsonObject) -> Any:
    """Idempotently record observations; a distinct recurrence reopens an incident."""
    with collection_storage(store).state.lock():
        state = _load(store)
        ref = store.put(json_bytes(assessment))
        seen = set(state["observations"])
        changes = []
        for finding in assessment["findings"]:
            identifier = incident_id(finding)
            observation = digest({"assessment_sha256": ref, "incident_id": identifier})
            if observation in seen:
                continue
            at = now()
            row = state["incidents"].get(identifier)
            action = "observed"
            if row is None:
                row = {
                    "incident_id": identifier,
                    "first_seen": at,
                    "occurrences": 0,
                    "windows": [],
                    "status": "open",
                }
                state["incidents"][identifier] = row
                action = "opened"
            elif row["status"] == "resolved":
                row["status"] = "open"
                action = "reopened"
            row.update(
                finding=finding,
                last_seen=at,
                latest_assessment_sha256=ref,
                occurrences=row["occurrences"] + 1,
            )
            if assessment.get("window") not in row["windows"]:
                row["windows"].append(assessment.get("window"))
            event = {
                "incident_id": identifier,
                "at": at,
                "action": action,
                "assessment_sha256": ref,
                "observation_id": observation,
            }
            state["events"].append(event)
            state["observations"].append(observation)
            seen.add(observation)
            changes.append(event)
        if changes or state.get("latest_assessment_sha256") != ref:
            state["latest_assessment_sha256"] = ref
            _commit(store, state)
        return ref, state, changes


def transition(
    store: RunStore, identifier: str, action: str, *, actor, note, evidence=None
) -> JsonObject:
    if action not in {"acknowledge", "resolve"}:
        raise ValueError("Expected acknowledge or resolve")
    if (
        not actor.strip()
        or not note.strip()
        or (action == "resolve" and not (evidence or "").strip())
    ):
        raise ValueError("Actor, review note and resolution evidence are required")
    with collection_storage(store).state.lock():
        state = _load(store)
        if identifier not in state["incidents"]:
            raise ValueError("Unknown incident")
        row = state["incidents"][identifier]
        target = "acknowledged" if action == "acknowledge" else "resolved"
        if row["status"] == "resolved" and action == "acknowledge":
            raise ValueError("Resolved incidents cannot be acknowledged")
        event = {
            "incident_id": identifier,
            "at": now(),
            "action": action,
            "actor": actor.strip(),
            "note": note.strip(),
            "evidence": evidence,
        }
        row["status"] = target
        row["last_review"] = event
        state["events"].append(event)
        _commit(store, state)
        return row


def relevant_open(state: JsonObject, source_ids: list[Any], window: JsonObject | None) -> list[Any]:
    def overlaps(row):
        return any(
            w is None
            or window is None
            or (w["mindate"] <= window["maxdate"] and w["maxdate"] >= window["mindate"])
            for w in row["windows"]
        )

    return [
        row
        for row in state["incidents"].values()
        if row["status"] != "resolved"
        and (row["finding"]["source_id"] in source_ids or row["finding"]["source_id"] == "pipeline")
        and overlaps(row)
    ]
