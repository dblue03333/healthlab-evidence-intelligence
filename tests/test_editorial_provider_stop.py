"""A provider protocol failure stops unattempted work instead of draining budget."""

import json

from healthlab import editorial as ed
from healthlab.models import IngestionError
from tests.test_editorial import setup_run


def test_provider_envelope_failure_stops_the_wave_without_retrying(tmp_path, monkeypatch):
    store, run, provider = setup_run(
        tmp_path, monkeypatch, [IngestionError("provider_envelope", "Invalid provider envelope")]
    )
    article = json.loads(store.read(run["input_sha256"]))[0]
    rows = [article]
    for identifier in ("second", "third"):
        rows.append(dict(article, document_id=identifier))
        job = store.new("editorial_call", run["question"])
        job.update(role="classify", document_id=identifier, status="prepared")
        store.save(job)
        run["jobs"].append(job["run_id"])
    run.update(input_sha256=store.put(ed.json_bytes(rows)), call_budget=10)
    store.save(run)
    ed.run_calls(store, run["run_id"], provider, workers=1)
    jobs = ed.jobs(store, store.load(run["run_id"]))
    assert provider.calls == 1
    assert jobs[0]["status"] == "failed" and jobs[0]["error_code"] == "provider_envelope"
    assert all(j["status"] == "prepared" and not j.get("charged") for j in jobs[1:])
