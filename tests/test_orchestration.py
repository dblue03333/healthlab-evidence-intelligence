import json

import httpx
import pytest

from healthlab.models import IngestionError, Question
from healthlab.orchestration import run_workflow, workflow_lock
from healthlab.provider import Completion, FakeProvider
from healthlab.store import RunStore
from tests.test_extraction import completion, second_completion
from tests.test_ingestion import connector, normal_handler, search_bytes


def empty_brief():
    return Completion(
        json.dumps(
            {"claims": [], "short_answer_claim_ids": [], "evidence_sufficiency": "insufficient"}
        ),
        "fake-test",
        "stop",
        {"total_tokens": 100},
    )


def start(tmp_path, provider, handler=normal_handler):
    store = RunStore(tmp_path)
    run = run_workflow(
        store,
        provider,
        question=Question(raw_question="Question", query="fixture"),
        client_factory=lambda: connector(handler)[0],
    )
    return store, run


def no_client():
    pytest.fail("Resume must not call PubMed")


def test_end_to_end_and_completed_resume(tmp_path):
    provider = FakeProvider([completion(), second_completion(), empty_brief()])
    store, run = start(tmp_path, provider)
    assert run["status"] == "completed"
    assert run["brief"] and provider.calls == 3
    assert run["metrics"]["api_calls"] == 5
    before = run["stages"]
    resumed = run_workflow(
        store, FakeProvider([]), resume_id=run["run_id"], client_factory=no_client
    )
    assert resumed["status"] == "completed" and resumed["stages"] == before
    assert resumed["metrics"] == run["metrics"]


def test_synthesis_timeout_resumes_only_synthesis(tmp_path):
    provider = FakeProvider(
        [completion(), second_completion(), IngestionError("provider_timeout", "Timeout")]
    )
    store, run = start(tmp_path, provider)
    assert run["status"] == "partial" and run["brief"] is None
    assert run["errors"][0]["stage"] == "synthesis"
    ingestion_id = run["stages"]["ingestion"][0]["run_id"]
    documents = store.load(ingestion_id)["documents"]
    provider = FakeProvider([empty_brief()])
    resumed = run_workflow(store, provider, resume_id=run["run_id"], client_factory=no_client)
    assert resumed["status"] == "completed" and provider.calls == 1
    assert store.load(ingestion_id)["documents"] == documents
    assert [len(v) for v in resumed["stages"].values()] == [1, 1, 2]


def test_extraction_timeout_cache_and_new_brief(tmp_path):
    provider = FakeProvider(
        [completion(), IngestionError("provider_timeout", "Timeout"), empty_brief()]
    )
    store, run = start(tmp_path, provider)
    assert run["status"] == "partial" and run["brief"]
    assert "extraction: partial" in run["warnings"]
    provider = FakeProvider([second_completion(), empty_brief()])
    resumed = run_workflow(store, provider, resume_id=run["run_id"], client_factory=no_client)
    assert resumed["status"] == "completed" and provider.calls == 2
    assert resumed["stages"]["extraction"][-1]["cache_hits"] == 1
    assert len(resumed["stages"]["synthesis"]) == 2
    assert run["brief"] != resumed["brief"]


def test_no_results_needs_no_provider(tmp_path):
    store, run = start(
        tmp_path, FakeProvider([]), lambda r: httpx.Response(200, content=search_bytes([], 0))
    )
    assert run["status"] == "completed" and run["metrics"]["api_calls"] == 1
    assert run["metrics"]["reported_usage"] == {}


def test_ingestion_failure_and_retry(tmp_path):
    store, run = start(tmp_path, FakeProvider([]), lambda r: httpx.Response(400))
    assert run["status"] == "failed" and not run["stages"]["extraction"]
    provider = FakeProvider([completion(), second_completion(), empty_brief()])
    resumed = run_workflow(
        store,
        provider,
        resume_id=run["run_id"],
        client_factory=lambda: connector(normal_handler)[0],
    )
    assert resumed["status"] == "completed"
    assert len(resumed["stages"]["ingestion"]) == 2


def test_changed_model_rejected_before_network(tmp_path):
    store, run = start(tmp_path, FakeProvider([]), lambda r: httpx.Response(400))
    provider = FakeProvider([])
    provider.identity = lambda: {"model": "different"}
    with pytest.raises(IngestionError, match="same workflow code"):
        run_workflow(store, provider, resume_id=run["run_id"], client_factory=no_client)


def test_modified_child_rejected(tmp_path):
    store, run = start(tmp_path, FakeProvider([completion(), second_completion(), empty_brief()]))
    child = store.load(run["stages"]["ingestion"][0]["run_id"])
    child["question"]["query"] = "changed"
    store.save(child)
    with pytest.raises(IngestionError, match="manifest changed"):
        run_workflow(store, FakeProvider([]), resume_id=run["run_id"], client_factory=no_client)


def test_corrupt_snapshot_rejected(tmp_path):
    store, run = start(tmp_path, FakeProvider([completion(), second_completion(), empty_brief()]))
    child = store.load(run["stages"]["ingestion"][0]["run_id"])
    digest = child["documents"][0]["snapshot_id"]
    (store.root / "objects" / digest).write_text("corrupt")
    with pytest.raises(IngestionError, match="hash"):
        run_workflow(store, FakeProvider([]), resume_id=run["run_id"], client_factory=no_client)


def test_concurrent_resume_rejected(tmp_path):
    store, run = start(tmp_path, FakeProvider([]), lambda r: httpx.Response(400))
    with (
        workflow_lock(store, run["run_id"]),
        pytest.raises(IngestionError, match="already running"),
    ):
        run_workflow(store, FakeProvider([]), resume_id=run["run_id"], client_factory=no_client)


def test_process_interrupt_has_child_link_and_recovers(tmp_path):
    class InterruptingProvider(FakeProvider):
        def complete(self, messages, audit):
            if self.calls == 1:
                raise KeyboardInterrupt
            return super().complete(messages, audit)

    provider = InterruptingProvider([completion()])
    with pytest.raises(KeyboardInterrupt):
        start(tmp_path, provider)
    store = RunStore(tmp_path)
    runs = [store.load(p.name) for p in (tmp_path / "runs").iterdir()]
    run = next(r for r in runs if r["mode"] == "workflow")
    assert run["status"] == "running"
    interrupted = run["stages"]["extraction"][-1]
    assert interrupted["run_id"] and interrupted["status"] == "running"
    resumed = run_workflow(
        store,
        FakeProvider([second_completion(), empty_brief()]),
        resume_id=run["run_id"],
        client_factory=no_client,
    )
    assert resumed["status"] == "completed"
    assert resumed["stages"]["extraction"][0]["status"] == "interrupted"
    assert resumed["stages"]["extraction"][-1]["cache_hits"] == 1


def test_partial_ingestion_keeps_same_snapshot_boundary(tmp_path):
    from tests.test_ingestion import XML

    def handler(request):
        raw = (
            search_bytes(["101", "102", "103", "999"])
            if request.url.path.endswith("esearch.fcgi")
            else XML
        )
        return httpx.Response(200, content=raw)

    store, run = start(
        tmp_path, FakeProvider([completion(), second_completion(), empty_brief()]), handler
    )
    assert run["status"] == "partial" and run["brief"]
    # Partial inherited from ingestion is not a retryable extraction failure.
    resumed = run_workflow(
        store, FakeProvider([]), resume_id=run["run_id"], client_factory=no_client
    )
    assert resumed["status"] == "partial"
    assert len(resumed["stages"]["ingestion"]) == 1
    assert resumed["stages"] == run["stages"]


def test_all_extractions_fail_brief_discloses_failure(tmp_path):
    from pathlib import Path

    store, run = start(
        tmp_path,
        FakeProvider(
            [
                IngestionError("provider_timeout", "Timeout"),
                IngestionError("provider_timeout", "Timeout"),
            ]
        ),
    )
    assert run["status"] == "partial" and run["brief"]
    assert run["errors"][0]["stage"] == "extraction"
    content = Path(run["brief"]).read_text()
    assert "Excluded PMID 101: failed" in content
    assert "Excluded PMID 103: failed" in content
    assert "insufficient" in content.lower()


def test_usage_excludes_cache_hits(tmp_path):
    first = completion()
    second = second_completion()
    first.usage = {"total_tokens": 12}
    second.usage = {"total_tokens": 24}
    store, run = start(
        tmp_path,
        FakeProvider([first, IngestionError("provider_timeout", "Timeout"), empty_brief()]),
    )
    assert run["metrics"]["reported_usage"]["total_tokens"] == 112
    resumed = run_workflow(
        store,
        FakeProvider([second, empty_brief()]),
        resume_id=run["run_id"],
        client_factory=no_client,
    )
    assert resumed["metrics"]["reported_usage"]["total_tokens"] == 236


def test_changed_implementation_rejected(tmp_path, monkeypatch):
    from healthlab import orchestration

    store, run = start(tmp_path, FakeProvider([]), lambda r: httpx.Response(400))
    original = orchestration.contract

    def changed(provider):
        result = original(provider)
        result["implementation"]["evidence"] = "changed"
        return result

    monkeypatch.setattr(orchestration, "contract", changed)
    with pytest.raises(IngestionError, match="same workflow code"):
        run_workflow(store, FakeProvider([]), resume_id=run["run_id"], client_factory=no_client)


def test_cli_run_and_resume(tmp_path, monkeypatch, capsys):
    from healthlab import __main__ as cli

    provider = FakeProvider([completion(), second_completion(), empty_brief()])
    provider.close = lambda: None
    monkeypatch.setattr(cli, "FPTProvider", lambda *a, **k: provider)
    monkeypatch.setattr(cli, "PubMedClient", lambda *a: connector(normal_handler)[0])
    assert cli.main(["--store", str(tmp_path), "run", "--query", "fixture"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["brief"] and output["stages"] and output["metrics"]["api_calls"] == 5
    monkeypatch.setattr(cli, "PubMedClient", lambda *a: no_client())
    assert cli.main(["--store", str(tmp_path), "resume", output["run_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == output["run_id"]


def test_actionable_configuration_error_is_preserved(tmp_path):
    from healthlab.config import Settings

    def invalid_client():
        Settings(email="your_email@example.com").validate_online()

    store = RunStore(tmp_path)
    run = run_workflow(
        store,
        FakeProvider([]),
        question=Question(raw_question="q", query="q"),
        client_factory=invalid_client,
    )
    assert run["status"] == "failed"
    assert run["errors"][0]["code"] == "config_ncbi_email"
    assert "NCBI_EMAIL" in run["errors"][0]["message"]
    assert "your_email@example.com" not in json.dumps(run)
    assert run["counts"] == {"ingestion": None}


def test_root_coverage_and_provider_error_are_visible(tmp_path):
    from healthlab.models import ConfigurationError

    store, run = start(
        tmp_path,
        FakeProvider(
            [
                ConfigurationError("config_fpt_api_key", "Set FPT_API_KEY before extraction"),
                ConfigurationError("config_fpt_api_key", "Set FPT_API_KEY before extraction"),
            ]
        ),
    )
    assert run["counts"]["ingestion"]["stored"] == 3
    assert run["counts"]["extraction"]["failed"] == 2
    assert run["counts"]["synthesis"]["evidence_items"] == 0
    assert run["errors"][0]["code"] == "config_fpt_api_key"
    assert run["status"] == "partial"
