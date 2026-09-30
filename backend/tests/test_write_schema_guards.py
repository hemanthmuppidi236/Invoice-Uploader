"""
The §6 guardrail: a PATCH can never move a status.

"PATCH /invoices/{id} uses extra='forbid' and never accepts status. Status
only moves through the named endpoints." These tests pin that at the schema
level, where it is enforced, rather than trusting the handler to notice.
"""

import pytest
from pydantic import ValidationError

from app.schemas.invoices import (
    FlagIn,
    InvoiceUpdate,
    MarkFiledIn,
    MarkUploadedIn,
    VoidIn,
)
from app.schemas.projects import ProjectUpdate


# ─── Invoice ──────────────────────────────────────────────────────────


def test_invoice_update_rejects_status():
    with pytest.raises(ValidationError) as e:
        InvoiceUpdate(status="approved")
    assert "status" in str(e.value)


@pytest.mark.parametrize(
    "field",
    [
        "reviewed_at",
        "approved_at",
        "rejected_at",
        "uploaded_at",
        "filed_at",
        "reviewer_id",
        "approver_id",
        "bt_bill_id",
        "flag_code",
        "voided_at",
    ],
)
def test_invoice_update_rejects_every_workflow_field(field):
    """Not just `status` — every stamp that records a human decision or a
    BuilderTrend fact is off limits to a generic edit."""
    with pytest.raises(ValidationError):
        InvoiceUpdate(**{field: "x"})


def test_invoice_update_accepts_the_metadata_it_should():
    payload = InvoiceUpdate(invoice_no="278461-1", amount=100)
    assert payload.invoice_no == "278461-1"
    # exclude_unset is what stops an omitted field reading as a clear
    assert set(payload.model_dump(exclude_unset=True)) == {"invoice_no", "amount"}


def test_invoice_update_trims_the_invoice_number():
    assert InvoiceUpdate(invoice_no="  278461  ").invoice_no == "278461"


def test_invoice_update_blank_invoice_number_becomes_null():
    """A cleared field should be NULL, not an empty string that then produces
    a bill_no of ''."""
    assert InvoiceUpdate(invoice_no="   ").invoice_no is None


def test_invoice_update_rejects_a_typo_rather_than_ignoring_it():
    """extra='forbid' turns a client-side typo into a 422 naming the field,
    instead of a save that silently did nothing."""
    with pytest.raises(ValidationError):
        InvoiceUpdate(invoce_no="278461")


# ─── Project ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("field", ["onboarded_at", "onboarded_by", "mix_design_pdf_path"])
def test_project_update_cannot_self_onboard(field):
    """§7.0 is a gate. A PATCH that could stamp onboarded_at would route
    invoices to a project with no reviewer."""
    with pytest.raises(ValidationError):
        ProjectUpdate(**{field: "x"})


def test_project_update_rejects_an_unknown_scope():
    with pytest.raises(ValidationError):
        ProjectUpdate(status="closed")


def test_project_update_accepts_the_two_real_scopes():
    assert ProjectUpdate(status="old").status == "old"
    assert ProjectUpdate(status="active").status == "active"


# ─── Reasons are required ─────────────────────────────────────────────


def test_flagging_requires_a_reason():
    """A flag with no detail leaves triage guessing what to fix."""
    with pytest.raises(ValidationError):
        FlagIn(code="other", detail="   ")


def test_flagging_rejects_an_unknown_code():
    """/flagged groups by code, so an unknown one would hide the invoice in a
    bucket that never renders."""
    with pytest.raises(ValidationError) as e:
        FlagIn(code="made_up", detail="something")
    assert "Unknown flag code" in str(e.value)


def test_flagging_accepts_a_known_code():
    assert FlagIn(code="possible_duplicate", detail="dupe of 8461").code == (
        "possible_duplicate"
    )


def test_voiding_requires_a_reason():
    with pytest.raises(ValidationError):
        VoidIn(reason="")
    assert VoidIn(reason="Entered by hand already").reason == (
        "Entered by hand already"
    )


# ─── Phase 3 write schemas ────────────────────────────────────────────


@pytest.mark.parametrize(
    "field",
    ["status", "approved_at", "reviewed_at", "uploaded_at", "filed_at", "amount"],
)
def test_mark_uploaded_forbids_everything_but_the_bill_id(field):
    """The Chrome session reports one fact: the BuilderTrend bill id. It has
    no business setting a stamp, a status, or an amount — §12 keeps every
    authorising write behind a person, and `extra="forbid"` is what makes a
    smuggled field a 422 naming the field rather than a silent write."""
    with pytest.raises(ValidationError) as e:
        MarkUploadedIn(bt_bill_id="BT-1", **{field: "x"})
    assert field in str(e.value)


def test_mark_uploaded_requires_a_bill_id():
    with pytest.raises(ValidationError):
        MarkUploadedIn()


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_mark_uploaded_rejects_a_blank_bill_id(blank):
    """An unidentifiable save cannot be told apart from a second bill on the
    next call, so it must not be recordable (SOP §8.4)."""
    with pytest.raises(ValidationError):
        MarkUploadedIn(bt_bill_id=blank)


def test_mark_filed_forbids_a_status():
    with pytest.raises(ValidationError):
        MarkFiledIn(status="filed")


def test_mark_filed_defaults_to_the_backend_doing_the_work():
    """No path means "file it now", which is §14's preferred route and the
    retry button on /uploads."""
    payload = MarkFiledIn()
    assert payload.filed_path is None
    assert payload.original_archived is False
