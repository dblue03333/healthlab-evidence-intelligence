"""Editorial abbreviation boundaries, frozen readers and source-date safeguards."""

import json
from types import SimpleNamespace

import pytest

from healthlab import editorial as ed
from healthlab.editorial.passages import MAX_PASSAGE_CHARS, editorial_passages, source_passages
from healthlab.editorial.scope import scope_gate, validate_scope
from tests.editorial_legacy import canonical_v12, resolve_v12
from tests.test_editorial import WINDOW, row, scope_assessment, setup_run
from tests.test_editorial_review_policy import review

CITY = "Ngày 05/11/2026 tại TP. Hồ Chí Minh, hội nghị về chăm sóc người cao tuổi được tổ chức."


def assert_literal_coverage(text, units):
    assert [u["passage_id"] for u in units] == [f"P{i + 1}" for i in range(len(units))]
    assert "".join("".join(u["quote"].split()) for u in units) == "".join(text.split())
    previous = 0
    for unit in units:
        assert unit["start"] >= previous
        assert not text[previous : unit["start"]].strip()
        assert 0 < unit["end"] - unit["start"] <= MAX_PASSAGE_CHARS
        assert text[unit["start"] : unit["end"]] == unit["quote"]
        previous = unit["end"]
    assert not text[previous:].strip()


@pytest.mark.parametrize(
    "abbreviation,name",
    [
        ("TP.", "Hồ Chí Minh"),
        ("Tp.", "Hồ Chí Minh"),
        ("TX.", "Bình Minh"),
        ("PGS.TS.", "Nguyễn An"),
        ("TS.BS.", "Trần Bình"),
        ("ThS.", "Lê An"),
        ("BSCKII.", "Lê An"),
        ("Dr.", "Smith"),
        ("Prof.", "Jones"),
    ],
)
def test_nonterminal_abbreviation_stays_with_its_continuation(abbreviation, name):
    first = f"Ngày 05/11/2026, {abbreviation} {name} tham dự hội nghị."
    text = first + " Một cuộc họp khác diễn ra ngày 06/11/2026."
    units = editorial_passages(text)
    assert [u["quote"] for u in units] == [first, "Một cuộc họp khác diễn ra ngày 06/11/2026."]
    assert_literal_coverage(text, units)


def test_unrelated_event_sentence_and_explicit_paragraph_are_not_merged():
    text = CITY + "\n\nNgày 06/11/2026, một sự kiện khác được tổ chức."
    units = editorial_passages(text)
    assert len(units) == 2 and units[0]["quote"] == CITY
    assert_literal_coverage(text, units)
    # Explicit paragraph boundaries take precedence even after a title.
    assert len(editorial_passages("Danh sách BS.\nNgười cao tuổi được hỗ trợ.")) == 2
    assert len(editorial_passages("Kết thúc? Bắt đầu! Nội dung tiếp theo.")) == 3


def test_generic_initials_and_sentence_final_etc_are_not_assumed_nonterminal_titles():
    text = "Nội dung A. Một câu khác. Several items etc. A new sentence."
    assert len(editorial_passages(text)) == 4
    assert_literal_coverage(text, editorial_passages(text))


@pytest.mark.parametrize(
    "text",
    [
        "",
        " \n\n ",
        "đ" * 4001,
        ("Phục hồi chức năng tại TP. Hồ Chí Minh " * 120),
        "Tại (PGS.TS. Nguyễn An), thông tin được thuật lại.\n\nDòng cuối 😀.",
    ],
)
def test_unicode_and_bounded_passages_retain_all_nonspace_source_text(text):
    assert_literal_coverage(text, editorial_passages(text))


def reference_payload(article, *, passage_id="P1", date_id="D1", rationale=None):
    return {
        "relevance": "relevant",
        "topics": ["aging_society"],
        "jurisdiction": "vietnam",
        "content_kind": "new_development",
        "source_role": "original",
        "summary": "Hội nghị chăm sóc người cao tuổi.",
        "rationale": "Nguồn nêu sự kiện chăm sóc người cao tuổi.",
        "relevance_passage_id": passage_id,
        "facts": [
            {
                "statement": "Theo nguồn, hội nghị được tổ chức ngày 05/11/2026 tại TP. Hồ Chí Minh.",
                "passage_id": passage_id,
                "event_date_candidate_id": date_id,
                "date_link_rationale": rationale,
                "reported_status": "announced",
            }
        ],
        "uncertainties": [],
    }


def test_city_case_no_longer_needs_a_cross_passage_date_link():
    article = row(CITY)
    old, old_dates = ed.passage_inputs(article, version="gold-editorial-10")
    new, new_dates = ed.passage_inputs(article)
    assert len(old) == 2 and old_dates[0]["passage_id"] == "P1"
    assert old[0]["quote"].endswith("TP.")
    with pytest.raises(ValueError, match="Cross-passage date"):
        ed.resolve_reference_ids(
            reference_payload(article, passage_id="P2"), article, version="gold-editorial-10"
        )
    assert len(new) == 1 and new_dates[0]["passage_id"] == "P1"
    resolved = resolve_v12(reference_payload(article), article)
    assert resolved["facts"][0]["event_date"] == "2026-11-05"
    assert resolved["facts"][0]["date_evidence"] is None
    assert resolved["facts"][0]["date_link_rationale"] is None


def test_actual_cross_event_date_still_requires_explicit_link_not_auto_readiness():
    article = row(CITY + " Một cuộc họp khác diễn ra ngày 06/11/2026.")
    with pytest.raises(ValueError, match="Cross-passage date"):
        resolve_v12(reference_payload(article, passage_id="P2"), article)
    with pytest.raises(ValueError, match="Unknown event date"):
        resolve_v12(reference_payload(article, date_id="D999"), article)


@pytest.mark.parametrize("version", [f"gold-editorial-{i}" for i in range(1, 11)])
def test_archived_versions_keep_exact_legacy_ids_and_offsets(version):
    article = row(CITY)
    units, _ = ed.passage_inputs(article, version=version)
    assert units == source_passages(SimpleNamespace(text=CITY))
    assert ed.passage_policy(version) == "editorial-passages-1"
    with pytest.raises(ValueError, match="Unknown editorial passage"):
        ed.passage_inputs(article, version="gold-editorial-unknown")


def test_archived_cross_passage_date_is_validated_against_old_units():
    article = row(CITY)
    raw = reference_payload(
        article, passage_id="P2", rationale="Cùng câu sự kiện trước viết tắt TP."
    )
    value = ed.resolve_reference_ids(raw, article, version="gold-editorial-10")
    window = {"mindate": "2026-11-01", "maxdate": "2026-11-07"}
    assert ed.canonical_proposal(value, article, window, version="gold-editorial-10")[
        "newsletter_candidate"
    ]
    with pytest.raises(ValueError, match="Date evidence differs"):
        canonical_v12(value, article, window)


def test_archived_evidence_reader_keeps_scope_and_date_ids_while_current_input_changes(
    tmp_path, monkeypatch
):
    article = row(
        "Ngày 10/10/2026 tại TP. Hồ Chí Minh, hội nghị chăm sóc người cao tuổi được tổ chức."
    )
    store, run, provider = setup_run(tmp_path, monkeypatch, [])
    run.update(
        version="gold-editorial-10",
        runtime_sha256="a" * 64,
        input_sha256=store.put(ed.json_bytes([article])),
    )
    run.pop("passage_policy")
    raw = reference_payload(article, passage_id="P2", rationale="Cùng câu sự kiện.")
    raw["facts"][0]["statement"] = (
        "Theo nguồn, hội nghị chăm sóc người cao tuổi được tổ chức ngày 10/10/2026 tại TP. Hồ Chí Minh."
    )
    p = ed.canonical_proposal(
        ed.resolve_reference_ids(raw, article, version=run["version"]),
        article,
        WINDOW,
        version=run["version"],
    )
    units, _ = ed.passage_inputs(article, version=run["version"])
    scope = validate_scope(scope_assessment(passage_ids=["P2"]), units)
    assert scope_gate(scope, units)["eligible"]
    review_value = review(review_version="editorial-review-3", supported_date_fact_indexes=[0])
    classify = store.load(run["jobs"][0])
    classify.update(status="completed", result_sha256=store.put(ed.json_bytes(p)))
    store.save(classify)
    for role, value in [("review", review_value), ("scope", scope)]:
        job = store.new("editorial_call", WINDOW)
        job.update(
            role=role,
            document_id=article["document_id"],
            status="completed",
            result_sha256=store.put(ed.json_bytes(value)),
        )
        store.save(job)
        run["jobs"].append(job["run_id"])
    store.save(run)
    packet = ed.evidence_packet(store, run["run_id"])
    assert packet["articles"][0]["eligible_for_unapproved_draft"]
    assert packet["draft_evidence"][0]["date_evidence"]["quote"].endswith("TP.")
    with pytest.raises(ValueError, match="read-only"):
        ed.load(store, run["run_id"], provider, for_inference=True)


def test_new_inputs_pin_passage_policy_and_still_send_the_complete_body(tmp_path, monkeypatch):
    store, run, _ = setup_run(tmp_path, monkeypatch, [])
    assert run["passage_policy"] == "editorial-passages-2"
    prompt, _ = ed.messages("scope", row(CITY), WINDOW)
    user = json.loads(prompt[1]["content"])
    assert user["article"]["passage_policy"] == run["passage_policy"]
    assert_literal_coverage(CITY, user["article"]["passages"])
    run["passage_policy"] = "editorial-passages-1"
    store.save(run)
    with pytest.raises(ValueError, match="passage policy differs"):
        ed.load(store, run["run_id"])
