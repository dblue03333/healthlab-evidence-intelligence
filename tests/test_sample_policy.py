import json

import pytest

from healthlab.evidence import parse_content
from healthlab.models import IngestionError
from healthlab.sample_policy import verify_sample_counts
from tests.test_extraction import evidence


def extracted(value, quote, role="unspecified"):
    return parse_content(
        json.dumps(
            evidence(
                main_finding=None,
                supporting_passage=None,
                section_index=None,
                sample_sizes=[
                    {
                        "value": value,
                        "role": role,
                        "group": None,
                        "supporting_passage": quote,
                        "section_index": 0,
                    }
                ],
            )
        )
    )


@pytest.mark.parametrize(
    "quote",
    [
        "We identified 14 systematic reviews or meta-analyses.",
        "Fourteen articles met the criteria.",
        "We used 14 measurements.",
    ],
)
def test_nonhuman_count_rejected_even_with_exact_quote(quote):
    with pytest.raises(IngestionError) as err:
        verify_sample_counts(extracted(14, quote))
    assert err.value.code == "sample_nonhuman_count"


def test_derived_total_is_not_explicit():
    with pytest.raises(IngestionError) as err:
        verify_sample_counts(extracted(67, "Australia (n = 62) and New Zealand (n = 5)."))
    assert err.value.code == "sample_count_not_explicit"


@pytest.mark.parametrize("role", ["randomized", "completed", "analyzed"])
def test_recruitment_does_not_support_other_roles(role):
    with pytest.raises(IngestionError) as err:
        verify_sample_counts(extracted(232, "232 participants were recruited.", role))
    assert err.value.code == "sample_role_unsupported"


@pytest.mark.parametrize(
    "value,quote,role",
    [
        (14, "Fourteen older adults participated.", "unspecified"),
        (47, "Forty-seven experts participated in Phase 3.", "unspecified"),
        (60, "Sixty participants were randomized.", "randomized"),
        (55, "55 participants completed follow-up.", "completed"),
        (101, "One hundred and one patients were enrolled.", "recruited"),
        (1200, "1,200 participants were analysed.", "analyzed"),
        (14, "We reviewed 14 studies and enrolled 14 participants.", "recruited"),
    ],
)
def test_explicit_human_counts_preserved(value, quote, role):
    verify_sample_counts(extracted(value, quote, role))


@pytest.mark.parametrize(
    "quote",
    [
        "114 participants",
        "14.5 percent",
        "14% of patients",
        "14,000 adults",
        "14-year-old participants",
    ],
)
def test_count_token_boundaries(quote):
    with pytest.raises(IngestionError) as err:
        verify_sample_counts(extracted(14, quote))
    assert err.value.code == "sample_count_not_explicit"


def test_dropout_sentence_does_not_establish_remaining_completed():
    quote = (
        "19 centenarians were enrolled, but 7 did not complete the study. "
        "The remaining 12 centenarians were randomly assigned to groups."
    )
    verify_sample_counts(extracted(12, quote, "randomized"))
    with pytest.raises(IngestionError) as err:
        verify_sample_counts(extracted(12, quote, "completed"))
    assert err.value.code == "sample_role_unsupported"
    with pytest.raises(IngestionError):
        verify_sample_counts(
            extracted(7, "7 participants did not complete the study.", "completed")
        )


def test_invalid_count_keeps_raw_but_cannot_publish_evidence_or_cache(tmp_path):
    from healthlab.extraction import cache_path, extract_run
    from healthlab.provider import FakeProvider
    from tests.test_extraction import completion, second_completion, source_run

    store, parent = source_run(tmp_path)
    wrong = evidence()
    wrong["sample_sizes"][0]["value"] = 61
    run = extract_run(
        store, parent["run_id"], FakeProvider([completion(wrong), second_completion()])
    )
    entry = run["results"][0]
    assert run["status"] == "partial"
    assert entry["error"]["code"] == "sample_count_not_explicit"
    assert entry["content_sha256"] and not entry.get("evidence_item_id")
    assert not cache_path(store, entry["cache_key"]).exists()


def test_legacy_quote_validation_is_explicit_not_default():
    from healthlab.evidence import verify_passages
    from healthlab.pubmed import parse_records
    from tests.test_ingestion import XML

    document = parse_records(XML)[0][0]
    wrong = evidence()
    wrong["sample_sizes"][0]["value"] = 61
    parsed = parse_content(json.dumps(wrong))
    assert verify_passages(parsed, document, "a" * 64, validator_version="passage-1")
    with pytest.raises(IngestionError):
        verify_passages(parsed, document, "a" * 64)
    with pytest.raises(IngestionError):
        verify_passages(parsed, document, "a" * 64, validator_version="unknown")


def test_same_number_can_count_both_studies_and_people():
    verify_sample_counts(
        extracted(14, "We reviewed 14 studies and recruited 14 healthy volunteers.", "recruited")
    )
