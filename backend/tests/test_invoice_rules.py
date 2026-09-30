"""
The SOP §4/§5 field rules. These are the ones that put wrong data into
BuilderTrend when they drift, so they are pinned tightly.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.core.invoice_rules import (
    allocate_proportionally,
    bill_no_for,
    confidence_band,
    costs_balance,
    due_date_for,
    is_do_not_select,
    is_yard_invoice,
    next_phases,
    phase_of,
    signed_amount,
    to_money,
)


# ─── Due date: end of the month AFTER the invoice date ────────────────


def test_due_date_worked_example_from_the_sop():
    """SOP §4 gives 9/2 → 10/31 explicitly."""
    assert due_date_for(date(2026, 9, 2)) == date(2026, 10, 31)


def test_due_date_rolls_across_the_year():
    """A December invoice is due end of January, not month 13."""
    assert due_date_for(date(2026, 12, 15)) == date(2027, 1, 31)


def test_due_date_handles_short_following_month():
    assert due_date_for(date(2026, 1, 20)) == date(2026, 2, 28)


def test_due_date_handles_leap_february():
    assert due_date_for(date(2028, 1, 5)) == date(2028, 2, 29)


def test_due_date_from_last_day_of_month():
    assert due_date_for(date(2026, 1, 31)) == date(2026, 2, 28)


# ─── Bill number: last 4 digits, suffixes ignored ─────────────────────


def test_bill_no_worked_example_from_the_sop():
    """SOP §4 gives 278461-1 → 8461. Note it is NOT 4611 (last four digits
    overall) and NOT '61-1' (last four characters)."""
    assert bill_no_for("278461-1") == "8461"


def test_bill_no_plain_number():
    assert bill_no_for("278461") == "8461"


def test_bill_no_ignores_a_letter_prefix():
    assert bill_no_for("INV-278461-1") == "8461"


def test_bill_no_prefers_the_long_run_over_a_year_prefix():
    """'2026-278461' is a year prefix plus the number, not the reverse."""
    assert bill_no_for("2026-278461") == "8461"


def test_bill_no_short_number_returned_whole():
    assert bill_no_for("42") == "42"


def test_bill_no_none_when_there_are_no_digits():
    """Better to flag than to type a guess into the Bill # field."""
    assert bill_no_for("CREDIT MEMO") is None
    assert bill_no_for("") is None
    assert bill_no_for(None) is None


# ─── Yard invoices ────────────────────────────────────────────────────


def test_yard_invoice_detected_from_the_sop_examples():
    assert is_yard_invoice("EL CAJON YARD") is True
    assert is_yard_invoice("SD Yard") is True


def test_yard_match_is_case_insensitive_and_scans_all_fields():
    assert is_yard_invoice(None, "customer job no. el cajon yard", None) is True


def test_a_job_whose_name_merely_contains_yard_is_not_a_yard_invoice():
    """Entering a real job's invoice is bad; skipping one is worse. The match
    is against known yard names, not the bare word."""
    assert is_yard_invoice("Yard Street Lofts") is False
    assert is_yard_invoice("Courtyard Apartments") is False


def test_do_not_select_vendor_detected():
    assert is_do_not_select("** DO NOT SELECT ** Old Vendor") is True
    assert is_do_not_select("Catalina Pacific") is False
    assert is_do_not_select(None) is False


# ─── Money ────────────────────────────────────────────────────────────


def test_to_money_rounds_floats_to_cents():
    assert to_money(2294.255) == Decimal("2294.26")
    assert to_money(447.46) == Decimal("447.46")


def test_to_money_rejects_garbage_rather_than_guessing():
    assert to_money(None) is None
    assert to_money("") is None
    assert to_money("n/a") is None


def test_credit_memo_is_stored_negative_whichever_sign_the_vendor_printed():
    """Vendors are inconsistent about the minus sign; the stored sign is not."""
    assert signed_amount(Decimal("500.00"), is_credit=True) == Decimal("-500.00")
    assert signed_amount(Decimal("-500.00"), is_credit=True) == Decimal("-500.00")


def test_normal_bill_is_stored_positive():
    assert signed_amount(Decimal("-500.00"), is_credit=False) == Decimal("500.00")
    assert signed_amount(Decimal("500.00"), is_credit=False) == Decimal("500.00")


# ─── Cost split balance ───────────────────────────────────────────────


def test_balanced_split_passes():
    ok, diff = costs_balance(Decimal("1000.00"),
                             [Decimal("600.00"), Decimal("400.00")])
    assert ok is True
    assert diff == Decimal("0.00")


def test_split_over_the_total_reports_the_overage():
    ok, diff = costs_balance(Decimal("1000.00"),
                             [Decimal("600.00"), Decimal("412.50")])
    assert ok is False
    assert diff == Decimal("12.50")


def test_split_under_the_total_reports_a_negative_difference():
    ok, diff = costs_balance(Decimal("1000.00"), [Decimal("900.00")])
    assert ok is False
    assert diff == Decimal("-100.00")


def test_no_cost_rows_does_not_balance():
    ok, _ = costs_balance(Decimal("1000.00"), [])
    assert ok is False


def test_zero_amount_invoice_balances_against_zero_rows():
    """A zero-total invoice is real (a corrected bill, a $0 delivery) and must
    not be permanently unapprovable."""
    ok, diff = costs_balance(Decimal("0.00"), [Decimal("0.00")])
    assert ok is True
    assert diff == Decimal("0.00")


def test_credit_memo_split_balances_while_negative():
    ok, _ = costs_balance(Decimal("-250.00"), [Decimal("-250.00")])
    assert ok is True


def test_unknown_amount_never_balances():
    ok, _ = costs_balance(None, [Decimal("100.00")])
    assert ok is False


# ─── Proportional allocation ──────────────────────────────────────────


def test_allocation_always_sums_to_the_total_exactly():
    """Independent rounding of each share loses cents, and a split that is a
    cent off blocks approval."""
    total = Decimal("1000.00")
    shares = allocate_proportionally(total, [Decimal("1"), Decimal("1"), Decimal("1")])
    assert sum(shares) == total
    assert len(shares) == 3


def test_allocation_is_proportional_to_weights():
    shares = allocate_proportionally(Decimal("900.00"),
                                     [Decimal("2"), Decimal("1")])
    assert shares == [Decimal("600.00"), Decimal("300.00")]


def test_allocation_with_zero_weights_splits_evenly():
    shares = allocate_proportionally(Decimal("100.00"),
                                     [Decimal("0"), Decimal("0")])
    assert sum(shares) == Decimal("100.00")


def test_allocation_of_a_credit_stays_negative_and_sums():
    shares = allocate_proportionally(Decimal("-100.00"),
                                     [Decimal("3"), Decimal("1")])
    assert sum(shares) == Decimal("-100.00")
    assert all(s <= 0 for s in shares)


def test_allocation_of_nothing_is_empty():
    assert allocate_proportionally(Decimal("100.00"), []) == []


# ─── Confidence bands (prompt §8) ─────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        (0.95, "high"),
        (0.85, "high"),      # boundary is inclusive
        (0.84, "medium"),
        (0.60, "medium"),    # boundary is inclusive
        (0.59, "low"),
        (0.0, "low"),
        (None, "none"),
    ],
)
def test_confidence_bands(value, expected):
    assert confidence_band(value) == expected


# ─── Pour sequence ────────────────────────────────────────────────────


def test_phase_recognises_the_sequence_elements():
    assert phase_of("Concrete Footings") == "foundations"
    assert phase_of("SOG") == "slab_or_basement"
    assert phase_of("Concrete Columns") == "vertical"
    assert phase_of("Elevated Deck") == "elevated"
    assert phase_of("Topping Slab") == "finishes"


def test_longest_keyword_wins_so_basement_wall_is_not_claimed_by_wall():
    assert phase_of("Basement Walls") == "slab_or_basement"
    assert phase_of("Shear Walls") == "vertical"


def test_unrecognised_element_has_no_phase():
    assert phase_of("Hardware & Misc") is None
    assert phase_of("") is None


def test_next_phases_starts_at_the_current_one():
    """Columns on level 2 pour while level 1 decks cure, so the current phase
    stays a candidate rather than being ruled out."""
    result = next_phases("vertical")
    assert result[0] == "vertical"
    assert result[1] == "elevated"
    assert len(result) == 6


def test_next_phases_with_no_history_returns_everything_in_order():
    assert next_phases(None)[0] == "foundations"


def test_bill_no_promotes_past_a_short_branch_code():
    """'9-278461' is a division prefix plus the number."""
    assert bill_no_for("9-278461") == "8461"


def test_bill_no_does_not_promote_a_date_suffix():
    """The SOP's own case must survive both exceptions: the first run wins
    when it is a plausible invoice number, even if a longer run follows."""
    assert bill_no_for("278461-1") == "8461"
    assert bill_no_for("8461-090226") == "8461"
