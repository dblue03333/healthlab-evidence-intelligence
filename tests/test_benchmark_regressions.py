"""Synthetic reproductions of observed v0.1 failure shapes, not clinical gold."""

import json

import pytest

from healthlab.evidence import parse_content, verify_passages
from healthlab.models import IngestionError
from healthlab.pubmed import parse_records
from tests.test_extraction import evidence
from tests.test_ingestion import XML


@pytest.mark.parametrize(
    "source,quote",
    [
        ("Health and well-being improved.", "Health and well‑being improved."),
        (
            "Participants (n\u00a0=\u00a05) completed follow-up.",
            "Participants (n = 5) completed follow-up.",
        ),
        (
            "Benefits were uncertain, and the optimal program remains unclear.",
            "The optimal program remains unclear.",
        ),
        ("Training is a sarcopenia countermeasure.", "Training is a sarcopenic countermeasure."),
    ],
)
def test_quote_mutations_remain_observable_not_silently_repaired(source, quote):
    document = parse_records(XML)[0][0]
    document.abstract_sections[1].text = source
    candidate = evidence(sample_sizes=[], main_finding="Reported result", supporting_passage=quote)
    with pytest.raises(IngestionError) as err:
        verify_passages(parse_content(json.dumps(candidate)), document, "a" * 64)
    assert err.value.code == "passage_not_found"


def test_finding_without_supporting_quote_is_not_a_valid_null_case():
    with pytest.raises(IngestionError) as err:
        parse_content(json.dumps(evidence(supporting_passage=None)))
    assert err.value.code == "schema_invalid"
