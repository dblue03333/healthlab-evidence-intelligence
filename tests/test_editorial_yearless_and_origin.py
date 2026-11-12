"""Yearless day/month resolution from the publication year, and trusted footer origins."""

import pytest

from healthlab.editorial_dates import (
    DATE_POLICY,
    INFERRED_YEAR_BASIS,
    editorial_literal_dates,
    resolve_yearless_dates,
)
from healthlab.editorial_origin import footer_attributions
from healthlab.editorial_review_policy import fact_date_supported


def resolved(text, published):
    return [(d["date"], d["quote"]) for d in resolve_yearless_dates(text, published)]


@pytest.mark.parametrize(
    ("text", "published", "expected"),
    [
        ("Sáng 2/11, đoàn làm việc", "2026-11-03", [("2026-11-02", "Sáng 2/11")]),
        ("Ngày 2/11 đoàn làm việc", "2026-11-02", [("2026-11-02", "Ngày 2/11")]),
        ("Ngày 30/12 đã họp", "2027-01-03", [("2026-12-30", "Ngày 30/12")]),
        ("Ngày 5/11 sẽ khai mạc", "2026-11-03", []),  # later day: previous year, far past
        ("Ngày 1/10 đã họp", "2026-11-03", []),  # more than 30 days before publication
        ("Ngày 31/2 đã họp", "2026-11-03", []),  # invalid calendar date
        ("Hạn chót ngày 5/11", "2026-11-06", []),  # deadline
        ("Tính đến ngày 5/11", "2026-11-06", []),  # as-of date
        ("Ngày 2/11 đã họp", None, []),  # no publication date
        ("Ngày 2/11 đã họp", "not-a-date", []),
        ("Đạt 95/100% chỉ tiêu", "2026-11-03", []),
    ],
)
def test_yearless_resolution(text, published, expected):
    assert resolved(text, published) == expected


def test_literal_date_catalog_marks_inferred_basis_only_under_current_policy():
    text = "Sáng 2/11, đoàn làm việc. Ngày 03/11/2026 báo cáo."
    legacy = editorial_literal_dates(text, publication_date="2026-11-03")
    assert [d["date"] for d in legacy] == ["2026-11-03"]
    current = editorial_literal_dates(text, policy=DATE_POLICY, publication_date="2026-11-03")
    assert [(d["date"], d.get("basis")) for d in current] == [
        ("2026-11-02", INFERRED_YEAR_BASIS),
        ("2026-11-03", None),
    ]
    assert [d["date"] for d in editorial_literal_dates(text, policy=DATE_POLICY)] == ["2026-11-03"]


def test_fact_date_supported_accepts_resolved_yearless_date():
    fact = dict(quote="Sáng 2/11, đoàn kiểm tra làm việc", event_date="2026-11-02")
    assert not fact_date_supported(fact, policy=DATE_POLICY)
    assert fact_date_supported(fact, policy=DATE_POLICY, publication_date="2026-11-03")
    assert not fact_date_supported(fact, publication_date="2026-11-03")
    assert not fact_date_supported(
        dict(fact, event_date="2026-11-03"), policy=DATE_POLICY, publication_date="2026-11-03"
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Nội dung bài. (Nguồn: suckhoedoisong.vn)", (True, ["suckhoedoisong.vn"], True)),
        ("Nội dung. Nguồn: www.who.int", (True, ["www.who.int"], True)),
        ("Nội dung. Nguồn: nhandan.vn", (True, ["nhandan.vn"], True)),
        ("Nội dung. Nguồn: baochinhphu.vn", (True, ["baochinhphu.vn"], True)),
        ("Nội dung. Nguồn: example.com", (True, [], False)),
        ("Nội dung. Nguồn: Báo Sức khỏe và Đời sống", (True, [], False)),
        ("Nội dung. Nguồn: suckhoedoisong.vn và example.com", (True, ["suckhoedoisong.vn"], False)),
        ("Nội dung. Nguồn: evilsuckhoedoisong.vn", (True, [], False)),
        ("Không có chân bài", (False, [], True)),
    ],
)
def test_footer_attributions(body, expected):
    assert footer_attributions(body) == expected
