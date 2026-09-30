"""
field_diff() drives prompt §7.4 — the record of how often the AI was right.
A phantom diff from re-saving an unchanged form would corrupt that measure.
"""

from datetime import date
from decimal import Decimal

from app.core.audit import field_diff


def test_no_change_produces_no_diff():
    before = {"amount": Decimal("100.00"), "vendor": "CalPortland"}
    assert field_diff(before, {"amount": 100.0, "vendor": "CalPortland"}) == {}


def test_real_change_is_recorded_with_both_sides():
    diff = field_diff({"amount": Decimal("100.00")}, {"amount": 250.5})
    assert diff == {"amount": {"from": 100.0, "to": 250.5}}


def test_blank_and_null_are_the_same_absence():
    assert field_diff({"note": None}, {"note": ""}) == {}
    assert field_diff({"note": ""}, {"note": None}) == {}


def test_whitespace_only_edit_is_not_a_change():
    assert field_diff({"invoice_no": "278461"}, {"invoice_no": " 278461 "}) == {}


def test_fields_absent_from_after_are_ignored():
    """A PATCH that omits a field must not read as clearing it."""
    before = {"a": 1, "b": 2}
    assert field_diff(before, {"a": 1}) == {}


def test_dates_compare_by_iso_value():
    assert field_diff({"d": date(2026, 9, 2)}, {"d": "2026-09-02"}) == {}


def test_diff_is_json_serializable():
    import json

    diff = field_diff(
        {"amount": Decimal("1.25"), "d": date(2026, 1, 1)},
        {"amount": Decimal("2.50"), "d": date(2026, 2, 1)},
    )
    json.dumps(diff)  # must not raise
