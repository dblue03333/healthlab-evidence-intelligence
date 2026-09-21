import json

from healthlab.evidence import build_messages, parse_content
from healthlab.provider import Completion, FakeProvider
from healthlab.pubmed import parse_records
from healthlab.sample_size_experiment import (
    CASES,
    SAMPLE_RULE,
    experiment_messages,
    inspect_samples,
    run_one,
)
from healthlab.store import RunStore
from tests.test_extraction import evidence
from tests.test_ingestion import XML


def example(text, samples):
    document = parse_records(XML)[0][0]
    document.abstract_sections[0].text = text
    candidate = evidence(
        sample_sizes=samples, main_finding=None, supporting_passage=None, section_index=None
    )
    return document, parse_content(json.dumps(candidate))


def sample(value, text, role="unspecified", group=None):
    return {
        "value": value,
        "role": role,
        "group": group,
        "supporting_passage": text,
        "section_index": 0,
    }


def test_article_counts_are_observed_separately_from_quote_validity():
    source = "Of 507 articles, nine papers met the inclusion criteria."
    doc, wrong = example(source, [sample(507, source), sample(9, source)])
    result = inspect_samples(wrong, doc, "a" * 64, CASES[0])
    assert result["sample_quote_valid"] is True
    assert result["forbidden_values_present"] == [9, 507]
    assert result["empty_as_expected"] is False
    _, corrected = example(source, [])
    assert inspect_samples(corrected, doc, "a" * 64, CASES[0])["empty_as_expected"] is True


def test_explicit_human_stages_must_not_be_lost():
    source = "We recruited 96 individuals; 60 were randomized and 55 completed follow-up."
    doc, candidate = example(
        source,
        [
            sample(96, source, "recruited"),
            sample(60, source, "randomized"),
            sample(55, source, "completed"),
        ],
    )
    assert inspect_samples(candidate, doc, "a" * 64, CASES[3])["missing_expected_roles"] == []
    _, empty = example(source, [])
    assert len(inspect_samples(empty, doc, "a" * 64, CASES[3])["missing_expected_roles"]) == 3


def test_expert_counts_need_groups_and_unstated_total_is_flagged():
    source = "Phase 1 involved 29 Task Force members. Experts from Australia (n=62) and New Zealand (n=5) joined Phase 2. Forty-seven experts joined Phase 3."
    doc, candidate = example(
        source,
        [
            sample(v, source, group=g)
            for v, g in [
                (29, "Task Force phase 1"),
                (62, "Experts Australia phase 2"),
                (5, "Experts New Zealand phase 2"),
                (47, "Experts phase 3"),
            ]
        ],
    )
    diag = inspect_samples(candidate, doc, "a" * 64, CASES[1])
    assert diag["expert_groups_present"] and not diag["missing_expected_values"]
    _, wrong = example(source, [sample(67, source, "recruited", "phase 2")])
    diag = inspect_samples(wrong, doc, "a" * 64, CASES[1])
    assert diag["forbidden_values_present"] == [67] and not diag["expert_groups_present"]


def test_missing_sample_and_ambiguous_completion_are_distinct():
    doc, empty = example("A consensus on exercise is presented.", [])
    assert inspect_samples(empty, doc, "a" * 64, CASES[4])["empty_as_expected"]
    source = "19 participants enrolled; 7 did not complete. Remaining 12 were randomized."
    doc, candidate = example(
        source,
        [
            sample(19, source, "recruited"),
            sample(12, source, "randomized"),
            sample(12, source, "completed"),
        ],
    )
    diag = inspect_samples(candidate, doc, "a" * 64, CASES[2])
    assert diag["ambiguous_roles_present"] == [[12, "completed"]]
    assert diag["missing_expected_roles"] == []


def test_one_factor_prompt_change_keeps_source_schema_and_production_unchanged():
    doc, _ = example("Participants were enrolled.", [])
    original = build_messages(doc)
    candidate = experiment_messages(doc, "candidate")
    assert candidate[1:] == original[1:]
    assert candidate[0]["content"] == original[0]["content"] + " " + SAMPLE_RULE
    assert build_messages(doc) == original
    assert experiment_messages(doc, "control") == original


def test_outputs_audited_and_diagnostics_survive_unrelated_quote_failure(tmp_path):
    source = "Ninety-six participants were recruited."
    doc, parsed = example(source, [sample(96, source, "recruited")])
    payload = parsed.model_dump()
    payload.update(main_finding="Result", supporting_passage="Invented result", section_index=0)
    provider = FakeProvider([Completion(json.dumps(payload), "fake", "stop", {"total_tokens": 30})])
    store = RunStore(tmp_path)
    run = run_one(store, provider, doc, "a" * 64, CASES[3], "candidate")
    assert run["status"] == "failed" and run["schema_valid"]
    assert run["sample_diagnostics"]["sample_quote_valid"]
    assert run["usage"]["total_tokens"] == 30
    assert store.read(run["content_sha256"])
    assert len(run["requests"]) == 1
