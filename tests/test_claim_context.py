import pytest

from healthlab.brief import Synthesis, validate_claims
from healthlab.claim_context import context_requirements
from healthlab.models import IngestionError


def scoped_item():
    texts = [
        "Ninety participants were allocated to exercise or waitlist control.",
        "A subset of 40 participants completed in-person assessments.",
        "Strength improved in the cohort. Mobility improved in lab subset participants compared with control.",
    ]
    return {
        "evidence_item_id": "a" * 64,
        "document": {
            "document_id": "pubmed:101",
            "abstract_sections": [{"text": t} for t in texts],
        },
        "evidence": {"study_type": "randomized controlled trial"},
        "passages": [
            {
                "passage_id": str(i + 1) * 64,
                "section_index": i,
                "text": text,
                "field": "main_finding" if i == 2 else f"source_context.{i}",
            }
            for i, text in enumerate(texts)
        ],
    }


def payload(text, refs=(0, 1, 2)):
    item = scoped_item()
    return Synthesis.model_validate(
        {
            "claims": [
                {
                    "claim_id": "C1",
                    "kind": "finding",
                    "inference": "reported_result",
                    "text": text,
                    "references": [
                        {
                            "evidence_item_id": item["evidence_item_id"],
                            "passage_id": item["passages"][i]["passage_id"],
                        }
                        for i in refs
                    ],
                }
            ],
            "short_answer_claim_ids": ["C1"],
            "evidence_sufficiency": "limited",
        }
    )


def test_subgroup_lost_rejected_even_when_quote_is_real():
    with pytest.raises(IngestionError) as err:
        validate_claims(
            payload(
                "Exercise improved strength and mobility in participants compared with waitlist control."
            ),
            [scoped_item()],
        )
    assert err.value.code == "missing_subgroup_scope"


@pytest.mark.parametrize(
    "text",
    [
        "Strength improved in the cohort; mobility improved in the lab subset compared with waitlist control.",
        "Sức mạnh cải thiện trong nhóm nghiên cứu; vận động cải thiện ở nhóm con so với nhóm đối chứng danh sách chờ.",
    ],
)
def test_scoped_claim_with_correct_comparator_citations(text):
    result = validate_claims(payload(text), [scoped_item()])
    assert result["semantic_support"] == "not_evaluated"
    assert result["context_guardrails"] == "lexical_checks_only"


def test_specific_comparator_requires_specific_passage():
    with pytest.raises(IngestionError) as err:
        validate_claims(
            payload(
                "Mobility improved in the lab subset compared with wait-list control.", refs=(1, 2)
            ),
            [scoped_item()],
        )
    assert err.value.code == "missing_comparator_citation"


def test_scope_citation_missing_even_with_qualifier_in_claim():
    item = scoped_item()
    item["passages"][2]["text"] = "Strength and mobility improved compared with control."
    with pytest.raises(IngestionError) as err:
        validate_claims(
            payload("Mobility improved in the lab subset compared with control.", refs=(2,)), [item]
        )
    assert err.value.code == "missing_scope_citation"


def test_legacy_validation_is_explicit_not_silently_upgraded():
    claim = payload("Exercise improved mobility compared with waitlist control.", refs=(2,))
    result = validate_claims(claim, [scoped_item()], validator_version="brief-refs-1")
    assert result["context_guardrails"] == "legacy_not_checked"
    with pytest.raises(IngestionError):
        validate_claims(claim, [scoped_item()], validator_version="unknown")


def test_no_subgroup_does_not_require_invented_qualifier():
    item = scoped_item()
    item["document"]["abstract_sections"] = [{"text": "Strength improved compared with control."}]
    item["passages"][2]["text"] = "Strength improved compared with control."
    assert not context_requirements(item)["scope_section_indexes"]
    validate_claims(payload("Strength improved compared with control.", refs=(2,)), [item])


def test_generic_comparator_without_any_cited_comparison_is_rejected():
    item = scoped_item()
    item["document"]["abstract_sections"] = [{"text": "Strength improved."}]
    item["passages"][2]["text"] = "Strength improved."
    with pytest.raises(IngestionError) as err:
        validate_claims(payload("Strength improved compared with control.", refs=(2,)), [item])
    assert err.value.code == "missing_comparator_citation"


def test_comparator_can_be_described_as_allocation_between_named_arms():
    item = scoped_item()
    item["document"]["abstract_sections"] = [
        {"text": "Participants were allocated to either exercise or a fitness regimen."}
    ]
    item["passages"][0]["text"] = (
        "Participants were allocated to either exercise or a fitness regimen."
    )
    item["passages"][2]["text"] = "Strength improved."
    validate_claims(
        payload("Strength improved compared with a fitness regimen.", refs=(0, 2)), [item]
    )
