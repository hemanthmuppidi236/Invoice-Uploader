"""
The upload queue payload (prompt §7.6 step 1, §11).

Everything in this payload exists so the Chrome session does not have to
derive it. A derived value that is wrong lands on a real bill in a real
accounting system, and the SOP's own history section is a list of exactly
that kind of mistake. So the derivations are tested here rather than trusted
to the session.

The distinction these tests protect hardest is `warnings` versus `blockers`.
A warning means proceed carefully; a blocker means do not save this one at
all. Collapsing them into one severity field is how a blocker gets treated as
advisory at 5pm on a Friday.
"""

from decimal import Decimal

import pytest

from app.api import invoices as api
from app.schemas.invoices import UploadQueueCost


# ─── The Bill Title (SOP §4) ──────────────────────────────────────────


def _cost(code, base, amount):
    return UploadQueueCost(
        cost_code=code, base_code=base, amount=Decimal(str(amount))
    )


def test_a_single_base_code_is_the_title():
    title, bases = api._bill_title([_cost("3002 - Concrete Walls", "3002", 2600)])
    assert title == "3002"
    assert bases == ["3002"]


def test_a_split_within_one_base_code_is_still_one_title():
    title, bases = api._bill_title(
        [
            _cost("3002 - Concrete Walls", "3002", 1600),
            _cost("3002 - Concrete Columns", "3002", 1000),
        ]
    )
    assert title == "3002"
    assert bases == ["3002"]


def test_a_split_across_base_codes_picks_the_largest_share():
    """BuilderTrend's Title takes one base code, but an invoice can legally
    split across two. The form cannot express that, so the largest share wins
    and the caller warns — picking the first row would put a plausible wrong
    code on the bill that nobody would notice."""
    title, bases = api._bill_title(
        [
            _cost("3008 - Slab on Grade", "3008", 400),
            _cost("3002 - Concrete Walls", "3002", 2200),
        ]
    )
    assert title == "3002"
    assert bases == ["3002", "3008"]


def test_a_tie_breaks_on_the_lower_code_not_on_row_order():
    a = api._bill_title(
        [_cost("3008 - SOG", "3008", 500), _cost("3002 - Walls", "3002", 500)]
    )
    b = api._bill_title(
        [_cost("3002 - Walls", "3002", 500), _cost("3008 - SOG", "3008", 500)]
    )
    assert a[0] == b[0] == "3002"


def test_a_credit_memo_share_is_measured_by_size_not_sign():
    """A negative row is still that code's share of the bill. Summing signed
    amounts would let a credit line cancel out the code it belongs to."""
    title, _ = api._bill_title(
        [_cost("3015 - Hardware", "3015", -900), _cost("3002 - Walls", "3002", 100)]
    )
    assert title == "3015"


def test_codes_with_no_base_code_contribute_no_title():
    title, bases = api._bill_title([_cost("Bids", None, 500)])
    assert title is None
    assert bases == []


# ─── Assembling one queue item ────────────────────────────────────────

CODES = {
    "c-walls": {
        "id": "c-walls",
        "code": "3002 - Concrete Walls",
        "base_code": "3002",
        "description": "Concrete Walls",
        "active": True,
    },
    "c-sog": {
        "id": "c-sog",
        "code": "3008 - Slab on Grade",
        "base_code": "3008",
        "description": "Slab on Grade",
        "active": True,
    },
    "c-dead": {
        "id": "c-dead",
        "code": "3099 - Retired",
        "base_code": "3099",
        "description": "Retired",
        "active": False,
    },
}

PROJECT = {
    "id": "p1",
    "project_no": "25-20",
    "name": "A Street Flats",
    "bt_job_id": "778899",
    "drive_folder_name": "A Street Flats",
    "quirks": {},
    "notes": None,
    "status": "old",
}

VENDOR = {
    "id": "v1",
    "invoice_name": "CalPortland",
    "bt_name": "Catalina Pacific",
    "drive_folder_name": "CalPortland",
    "notes": None,
    "active": True,
}


def _row(**over):
    base = {
        "id": "inv-1",
        "invoice_no": "278461-1",
        "bill_no": "8461",
        "invoice_date": "2026-09-14",
        "due_date": "2026-10-31",
        "amount": Decimal("2600.00"),
        "is_credit": False,
        "pdf_storage_path": "2026/09/inv-1.pdf",
        "project_id": "p1",
        "vendor_id": "v1",
        "created_at": "2026-09-15T00:00:00+00:00",
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def no_storage(monkeypatch):
    monkeypatch.setattr(
        api.storage, "signed_url", lambda bucket, path, expires_in=3600: "https://signed"
    )


def _item(row=None, *, project=PROJECT, vendor=VENDOR, costs=None):
    return api._to_queue_item(
        row or _row(),
        project=project,
        vendor=vendor,
        cost_rows=costs
        if costs is not None
        else [{"cost_code_id": "c-walls", "amount": Decimal("2600.00")}],
        codes=CODES,
    )


def test_a_clean_invoice_is_ready_to_type_with_nothing_to_derive():
    item = _item()
    assert item.blockers == []
    assert item.warnings == []
    assert item.bill_title == "3002"
    assert item.bill_no == "8461"          # SOP §4: last 4, suffix ignored
    assert item.pay_to == "Catalina Pacific"   # SOP §6, not the letterhead
    assert item.bill_url.endswith("/app/Bills/Bill/0/778899")
    assert item.pdf_url == "https://signed"
    assert item.costs[0].cost_code == "3002 - Concrete Walls"


def test_the_due_date_is_derived_when_the_row_has_none():
    """SOP §4: end of the month AFTER the invoice date, vendor terms ignored.
    There is nothing on the invoice to look up, so a blank is filled rather
    than left for the session to guess."""
    item = _item(_row(due_date=None))
    assert str(item.due_date) == "2026-10-31"


def test_a_missing_vendor_blocks_rather_than_warns():
    item = _item(vendor=None)
    assert any("Pay To" in b for b in item.blockers)


def test_a_do_not_select_vendor_blocks():
    """SOP §6: "Never pick a vendor labeled ** DO NOT SELECT **." A bill
    against one of those is unrecoverable bookkeeping."""
    item = _item(
        vendor={**VENDOR, "bt_name": "Catalina Pacific ** DO NOT SELECT **"}
    )
    assert any("DO NOT SELECT" in b for b in item.blockers)


def test_a_current_job_blocks_because_it_is_out_of_scope():
    """§4.1: a current job is a different workflow. Reaching approved here
    means something upstream let it through."""
    item = _item(project={**PROJECT, "status": "active"})
    assert any("current job" in b for b in item.blockers)


def test_a_missing_pdf_blocks_because_sop_requires_it_attached():
    item = _item(_row(pdf_storage_path=None))
    assert any("PDF" in b for b in item.blockers)
    assert item.pdf_url is None


def test_an_unbalanced_split_blocks_with_both_numbers():
    item = _item(costs=[{"cost_code_id": "c-walls", "amount": Decimal("2500.00")}])
    blocker = next(b for b in item.blockers if "sum to" in b)
    assert "$2,500.00" in blocker and "$2,600.00" in blocker


def test_no_cost_rows_blocks():
    item = _item(costs=[])
    assert any("Costs grid" in b for b in item.blockers)


def test_an_invoice_number_with_no_digits_blocks_on_the_bill_number():
    item = _item(_row(invoice_no="CREDIT-MEMO", bill_no=None))
    assert any("Bill #" in b for b in item.blockers)


def test_a_missing_job_id_warns_and_leaves_no_bill_url():
    """SOP §8.5: job context drifts, which is why bills are opened by URL.
    Without an id the session has to navigate by name and re-confirm the Job
    field, so it is told to."""
    item = _item(project={**PROJECT, "bt_job_id": None})
    assert item.bill_url is None
    assert any("confirm the Job field" in w for w in item.warnings)
    assert item.blockers == []


def test_an_old_invoice_warns_to_check_all_bills_first():
    """SOP §4.2: an invoice more than a few weeks old may already have been
    entered by hand."""
    item = _item(_row(created_at="2026-01-01T00:00:00+00:00"))
    assert any("All Bills" in w for w in item.warnings)


def test_a_credit_memo_warns_how_to_enter_it():
    item = _item(_row(is_credit=True, amount=Decimal("-500.00")),
                 costs=[{"cost_code_id": "c-walls", "amount": Decimal("-500.00")}])
    assert any("Credit memo" in w for w in item.warnings)
    assert item.blockers == []


def test_a_zero_total_warns_but_does_not_block():
    """Somebody already reviewed and approved it. A zero bill is odd, not
    impossible, and blocking here would strand an approved invoice."""
    item = _item(_row(amount=Decimal("0.00")), costs=[])
    assert any("$0.00" in w for w in item.warnings)


def test_a_cross_base_code_split_warns_and_names_the_chosen_title():
    item = _item(
        costs=[
            {"cost_code_id": "c-walls", "amount": Decimal("2200.00")},
            {"cost_code_id": "c-sog", "amount": Decimal("400.00")},
        ]
    )
    warning = next(w for w in item.warnings if "base codes" in w)
    assert "3002 and 3008" in warning
    assert "set to 3002" in warning
    assert item.blockers == []
    # Both sub-codes still go on the Costs grid; only the Title is singular.
    assert {c.cost_code for c in item.costs} == {
        "3002 - Concrete Walls",
        "3008 - Slab on Grade",
    }


def test_an_inactive_cost_code_warns_rather_than_blocking():
    item = _item(costs=[{"cost_code_id": "c-dead", "amount": Decimal("2600.00")}])
    assert any("inactive" in w for w in item.warnings)
    assert item.blockers == []


def test_a_cost_code_that_no_longer_exists_blocks():
    item = _item(costs=[{"cost_code_id": "c-vanished", "amount": Decimal("2600.00")}])
    assert any("no longer exists" in b for b in item.blockers)


def test_a_missing_drive_folder_name_warns_about_filing_not_about_the_bill():
    """The BuilderTrend half is unaffected by a Drive gap, and saying so stops
    the session abandoning a bill it could have entered."""
    item = _item(project={**PROJECT, "drive_folder_name": None})
    warning = next(w for w in item.warnings if "filing" in w)
    assert "BuilderTrend part is unaffected" in warning
    assert item.blockers == []


def test_project_quirks_are_handed_over_and_flagged_for_reading():
    quirks = {"job_field": "Two jobs share this address; confirm Parking vs Apartments"}
    item = _item(project={**PROJECT, "quirks": quirks})
    assert item.quirks == quirks
    assert any("quirks" in w for w in item.warnings)
