"""Opt-in durable triage budgets and explicit, bounded recovery between invocations.

No sleeping, scheduling, fallback, or retries of unknown outcomes. All methods run
under the existing collection state lock. This is a per-run budget, not an account
quota coordinator. Start a new run to change the pinned policy or inference inputs.
"""

import copy
import hashlib
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from healthlab.models import IngestionError
from healthlab.signal import provider_failures
from healthlab.store import json_bytes


class Recovery:
    def __init__(self, storage, run, provider, version, total_budget, max_attempts):
        self.storage, self.run = storage, run
        identity = dict(
            version="triage-recovery-1",
            input_sha256=run["input_sha256"],
            provider=provider.identity()
            if provider
            else run.get("recovery", {}).get("identity", {}).get("provider"),
            prompt_version=version,
            runtime_sha256={
                p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(Path(__file__).parent.glob("triage*.py"))
            },
        )
        if "recovery" not in run:
            if run.get("model_attempts"):
                raise ValueError(
                    "Start a new run for resumable triage; legacy attempts remain immutable"
                )
            if total_budget is None or not 1 <= total_budget <= 510:
                raise ValueError(
                    "First resumable invocation requires total_call_budget between 1 and 510"
                )
            if not 1 <= max_attempts <= 5:
                raise ValueError("max_attempts must be between 1 and 5")
            run["recovery"] = dict(
                identity=identity,
                total_call_budget=total_budget,
                max_attempts=max_attempts,
                calls=[],
                state="ready",
                pause=None,
            )
        self.state = run["recovery"]
        if (
            self.state["identity"] != identity
            or total_budget not in {None, self.state["total_call_budget"]}
            or max_attempts != self.state["max_attempts"]
        ):
            raise ValueError(
                "Incompatible recovery input/provider/prompt/policy; prepare a new run"
            )
        self.used_before = len(self.state["calls"])
        # A process may have died after dispatch, audit, or response persistence.
        # A response checkpoint is reusable; a started call without one is unknown.
        for call in self.state["calls"]:
            if call["status"] in {"started", "audited"}:
                call.update(
                    status="unknown_outcome", failure=dict(kind="unknown_outcome", retryable=False)
                )
                self.state.update(
                    state="blocked", pause=dict(kind="unknown_outcome", retryable=False)
                )
        storage.save(run)

    @property
    def remaining(self):
        return self.state["total_call_budget"] - len(self.state["calls"])

    @property
    def new_calls(self):
        return len(self.state["calls"]) - self.used_before

    def ready(self):
        pause = self.state.get("pause")
        if pause:
            if not pause["retryable"] or not pause.get("not_before"):
                return False
            if provider_failures.now() < datetime.fromisoformat(pause["not_before"]):
                return False
        self.state.update(state="ready", pause=None)
        self.storage.save(self.run)
        return True

    def defer_from_evidence(self, event, raw):
        """Carry a known provider cooldown into a fresh run without making a call.

        The caller must establish that the error belongs to the same project and
        model. This stores the original HTTP evidence and does not invent a new
        model attempt or count the historical request against this run's budget.
        """
        if self.state["calls"] or self.run.get("model_attempts"):
            raise ValueError("Only a fresh managed run can import prior cooldown evidence")
        failure = provider_failures.failure_details(event, raw)
        if not failure or not failure["retryable"]:
            raise ValueError("Saved evidence does not establish a retryable cooldown")
        received = datetime.fromisoformat(event["finished_at"])
        if received.tzinfo is None:
            raise ValueError("Cooldown evidence requires a timezone")
        delay = max(
            failure.get("retry_delay_seconds", 0),
            86400
            if failure["kind"] == "daily_quota" and not failure.get("retry_delay_seconds")
            else 30,
        )
        failure["not_before"] = (received + timedelta(seconds=delay)).isoformat()
        failure["evidence_sha256"] = self.storage.put(
            json_bytes(dict(event=event, raw_sha256=self.storage.put(raw)))
        )
        self.state.update(state="paused", pause=failure)
        self.storage.save(self.run)

    def previous(self, sid):
        attempts = [a for a in self.run.get("model_attempts", []) if a["snapshot_id"] == sid]
        if not attempts:
            return None
        last = attempts[-1]
        if last["status"] == "validated_proposal":
            return None
        if len(attempts) >= self.state["max_attempts"]:
            self.state.update(state="blocked", pause=dict(kind="attempt_budget", retryable=False))
            self.storage.save(self.run)
            return False
        # A crash after the final response checkpoint is safe to replay locally.
        if last["status"] == "started" and all(
            s["status"] == "completed" for s in last.get("steps", [])
        ):
            return last
        if not last.get("recovery_failure", {}).get("retryable"):
            return False
        return last

    def begin(self, attempt, previous):
        attempt["attempt_id"] = uuid.uuid4().hex
        if previous:
            attempt["retry_of"] = previous["attempt_id"]
            for field in ("provider", "prompt_version", "coverage_plan_sha256"):
                if attempt.get(field) != previous.get(field):
                    raise ValueError("Incompatible recovery attempt/coverage plan")

    def reuse(self, attempt, previous, prompt, stage, schema, mode):
        if not previous:
            return None
        index = len(attempt.get("steps", []))
        steps = previous.get("steps", [])
        if index >= len(steps) or steps[index]["status"] != "completed":
            return None
        step = steps[index]
        if (
            json.loads(self.storage.read(step["prompt_sha256"])) != prompt
            or step["stage"] != stage
            or step.get("response_format", "text") != mode
            or (
                schema is not None
                and json.loads(self.storage.read(step["response_schema_sha256"])) != schema
            )
        ):
            raise ValueError("Incompatible recovery checkpoint prompt/schema")
        response = json.loads(self.storage.read(step["model_response_sha256"]))
        if (
            response["finish_reason"] != "stop"
            or response["actual_model"] != attempt["provider"]["model"]
        ):
            raise ValueError("Incompatible recovery response model/completion")
        call = next((c for c in self.state["calls"] if c["call_id"] == step.get("call_id")), None)
        if not call or call.get("response_sha256") != step["model_response_sha256"]:
            raise ValueError("Recovery checkpoint has no matching durable call record")
        saved = copy.deepcopy(step)
        saved["reused_from_attempt"] = previous["attempt_id"]
        attempt.setdefault("steps", []).append(saved)
        self.storage.save(self.run)
        return response

    def reserve(self, attempt, step):
        if self.remaining <= 0:
            raise IngestionError("triage_call_budget", "Durable call budget exhausted")
        call = dict(
            call_id=uuid.uuid4().hex,
            attempt_id=attempt["attempt_id"],
            snapshot_id=attempt["snapshot_id"],
            status="started",
            prompt_sha256=step["prompt_sha256"],
            started_at=provider_failures.now().isoformat(),
        )
        step["call_id"] = call["call_id"]
        self.state["calls"].append(call)
        self.storage.save(self.run)  # Count before dispatch, including ambiguous crashes.
        return call

    def audit(self, call, writer):
        def record(event, raw):
            event = dict(event, call_id=call["call_id"], attempt_id=call["attempt_id"])
            failure = provider_failures.failure_details(event, raw)
            if failure:
                event["failure"] = failure
                call["failure"] = failure
            call.update(status="audited", audit=event)
            writer(event, raw)

        return record

    def completed(self, call, step):
        call.update(status="completed", response_sha256=step["model_response_sha256"])
        self.storage.save(self.run)

    def failed(self, attempt, exc):
        call = next(
            (c for c in reversed(self.state["calls"]) if c["attempt_id"] == attempt["attempt_id"]),
            None,
        )
        failure = (call or {}).get("failure")
        if call and call["status"] in {"started", "unknown_outcome"}:
            failure = dict(kind="unknown_outcome", retryable=False)
        if not failure and getattr(exc, "code", None) == "triage_output_token_limit":
            failure = dict(kind="output_token_limit", retryable=False)
        if not failure:
            failure = dict(kind="invalid_output_or_contract", retryable=False)
        failure = dict(failure)
        if failure["retryable"]:
            delay = max(
                failure.get("retry_delay_seconds", 0),
                86400
                if failure["kind"] == "daily_quota" and not failure.get("retry_delay_seconds")
                else 30
                * 2
                ** min(
                    4,
                    len(
                        [
                            a
                            for a in self.run["model_attempts"]
                            if a["snapshot_id"] == attempt["snapshot_id"]
                        ]
                    )
                    - 1,
                ),
            )
            failure["not_before"] = (provider_failures.now() + timedelta(seconds=delay)).isoformat()
        attempt["recovery_failure"] = failure
        attempt["provider_error_code"] = getattr(exc, "code", type(exc).__name__)
        if call and call["status"] != "completed":
            call["status"] = "failed" if failure["kind"] != "unknown_outcome" else "unknown_outcome"
        self.state.update(state="paused" if failure["retryable"] else "blocked", pause=failure)
        self.storage.save(self.run)

    def summary(self):
        return dict(
            state=self.state["state"],
            pause=self.state["pause"],
            total_calls=len(self.state["calls"]),
            total_call_budget=self.state["total_call_budget"],
            calls_remaining=self.remaining,
        )
