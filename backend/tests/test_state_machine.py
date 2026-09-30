"""
The invoice state machine (prompt §6).

Every transition has to validate the current state, stamp the actor and time,
write audit_log, and enforce its preconditions. These tests pin the guards,
including the refusal paths — a state machine that only gets tested on the
happy path is a state machine that lets an unbalanced invoice through.
"""

from decimal import Decimal

import pytest

from app.core import state_machine
from app.core.auth import Actor, CurrentUser
from app.core.state_machine import TransitionError

from tests.fakes import FakeClient, FakeTable


REVIEWER = "u-pe"
APPROVER = "u-raz"
OTHER_APPROVER = "u-shant"


def admin() -> Actor:
    return Actor(user=CurrentUser(id="u-admin", email="a@f.com", roles=["admin"]))


def accountant() -> Actor:
    return Actor(
        user=CurrentUser(id="u-linda", email="linda@f.com",
                         roles=["accountant", "approver"])
    )


def reviewer() -> Actor:
    return Actor(user=CurrentUser(id=REVIEWER, email="pe@f.com", roles=["pe"]))


def other_reviewer() -> Actor:
    return Actor(user=CurrentUser(id="u-pe2", email="pe2@f.com", roles=["pe"]))


def approver() -> Actor:
    return Actor(
        user=CurrentUser(id=APPROVER, email="raz@f.com", roles=["approver"])
    )


def other_approver() -> Actor:
    return Actor(
        user=CurrentUser(id=OTHER_APPROVER, email="shant@f.com", roles=["approver"])
    )


def agent() -> Actor:
    return Actor(is_agent=True)


@pytest.fixture
def db(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(state_machine, "_sb", lambda: client)
    from app.core import audit

    monkeypatch.setattr(audit, "get_service_client", lambda: client)
    return client


def ready_invoice(**over):
    """An invoice that satisfies every mark-reviewed precondition."""
    base = {
        "id": "inv-1",
        "status": "assigned",
        "project_id": "p-1",
        "vendor_id": "v-1",
        "amount": "1000.00",
        "invoice_date": "2026-09-02",
        "invoice_no": "278461",
        "reviewer_id": REVIEWER,
        "approver_id": APPROVER,
        "reviewed_at": None,
    }
    base.update(over)
    return base


def seed(db, invoice, costs=None):
    db.tables["invoices"] = FakeTable("invoices", [dict(invoice)])
    db.tables["invoice_costs"] = FakeTable(
        "invoice_costs",
        costs if costs is not None
        else [{"invoice_id": invoice["id"], "cost_code_id": "cc-1",
               "amount": "1000.00"}],
    )
    return db


# ─── State validation ─────────────────────────────────────────────────


def test_a_valid_transition_stamps_status_and_time(db):
    seed(db, ready_invoice())
    updated = state_machine.apply(
        invoice=ready_invoice(),
        transition=state_machine.MARK_REVIEWED,
        actor=reviewer(),
    )
    assert updated["status"] == "pending_approval"
    assert updated["reviewed_at"] is not None


def test_a_transition_from_the_wrong_state_is_a_409(db):
    """Almost always means someone else acted while this page was open."""
    seed(db, ready_invoice(status="approved"))
    with pytest.raises(TransitionError) as e:
        state_machine.apply(
            invoice=ready_invoice(status="approved"),
            transition=state_machine.MARK_REVIEWED,
            actor=reviewer(),
        )
    assert e.value.status_code == 409
    assert "approved" in str(e.value)
    assert "reload" in str(e.value).lower()


def test_nothing_is_written_when_the_state_does_not_match(db):
    seed(db, ready_invoice(status="approved"))
    with pytest.raises(TransitionError):
        state_machine.apply(
            invoice=ready_invoice(status="approved"),
            transition=state_machine.MARK_REVIEWED,
            actor=reviewer(),
        )
    assert db.tables["invoices"].rows[0]["status"] == "approved"
    assert "audit_log" not in db.tables or not db.tables["audit_log"].rows


def test_every_transition_writes_an_audit_row(db):
    seed(db, ready_invoice())
    state_machine.apply(
        invoice=ready_invoice(),
        transition=state_machine.MARK_REVIEWED,
        actor=reviewer(),
    )
    rows = db.tables["audit_log"].rows
    assert len(rows) == 1
    assert rows[0]["action"] == "reviewed"
    assert rows[0]["from_status"] == "assigned"
    assert rows[0]["to_status"] == "pending_approval"
    assert rows[0]["actor_id"] == REVIEWER


def test_assign_accepts_a_flagged_invoice(db):
    """Linda routinely clears a flag by setting the project and handing it
    straight to a reviewer; forcing an unflag first is a click for no gain."""
    seed(db, ready_invoice(status="flagged"))
    updated = state_machine.apply(
        invoice=ready_invoice(status="flagged"),
        transition=state_machine.ASSIGN,
        actor=accountant(),
    )
    assert updated["status"] == "assigned"


# ─── Preconditions ────────────────────────────────────────────────────


def test_mark_reviewed_needs_a_balanced_cost_split(db):
    """§5: invoice_costs must sum to invoices.amount."""
    seed(
        db,
        ready_invoice(),
        costs=[{"invoice_id": "inv-1", "cost_code_id": "cc-1", "amount": "600.00"}],
    )
    with pytest.raises(TransitionError) as e:
        state_machine.apply(
            invoice=ready_invoice(),
            transition=state_machine.MARK_REVIEWED,
            actor=reviewer(),
        )
    assert e.value.status_code == 422
    assert "under the invoice total by $400.00" in str(e.value)


def test_the_balance_error_names_the_direction_and_the_gap(db):
    seed(
        db,
        ready_invoice(),
        costs=[{"invoice_id": "inv-1", "cost_code_id": "cc-1", "amount": "1012.50"}],
    )
    with pytest.raises(TransitionError) as e:
        state_machine.apply(
            invoice=ready_invoice(),
            transition=state_machine.MARK_REVIEWED,
            actor=reviewer(),
        )
    assert "over the invoice total by $12.50" in str(e.value)


def test_mark_reviewed_needs_no_cost_rows_to_be_a_clear_error(db):
    seed(db, ready_invoice(), costs=[])
    with pytest.raises(TransitionError) as e:
        state_machine.apply(
            invoice=ready_invoice(),
            transition=state_machine.MARK_REVIEWED,
            actor=reviewer(),
        )
    assert "No cost code" in str(e.value)


@pytest.mark.parametrize(
    "field,phrase",
    [
        ("project_id", "a project"),
        ("vendor_id", "a vendor"),
        ("amount", "an amount"),
        ("invoice_date", "an invoice date"),
        ("invoice_no", "an invoice number"),
    ],
)
def test_mark_reviewed_needs_everything_buildertrend_needs(db, field, phrase):
    """Past review the next decision is 'approve', then Chrome types these
    into BuilderTrend. A missing job or vendor there is a bill on the wrong
    record, so it is caught while a person is still looking at it."""
    invoice = ready_invoice(**{field: None})
    seed(db, invoice)
    with pytest.raises(TransitionError) as e:
        state_machine.apply(
            invoice=invoice,
            transition=state_machine.MARK_REVIEWED,
            actor=reviewer(),
        )
    assert e.value.status_code == 422
    assert phrase in str(e.value)


def test_mark_reviewed_needs_an_approver_named(db):
    """Otherwise the invoice lands in pending_approval with nobody asked."""
    invoice = ready_invoice(approver_id=None)
    seed(db, invoice)
    with pytest.raises(TransitionError) as e:
        state_machine.apply(
            invoice=invoice,
            transition=state_machine.MARK_REVIEWED,
            actor=reviewer(),
        )
    assert "Pick an approver" in str(e.value)


def test_approve_requires_a_review_stamp(db):
    """§12: nothing reaches BuilderTrend without a human review."""
    invoice = ready_invoice(status="pending_approval", reviewed_at=None)
    seed(db, invoice)
    with pytest.raises(TransitionError) as e:
        state_machine.apply(
            invoice=invoice,
            transition=state_machine.APPROVE,
            actor=approver(),
        )
    assert "has not been reviewed" in str(e.value)


def test_approve_rechecks_the_balance(db):
    """The split can be edited between review and approval, so a stale check
    would let an unbalanced invoice through to BuilderTrend."""
    invoice = ready_invoice(
        status="pending_approval", reviewed_at="2026-09-29T10:00:00Z"
    )
    seed(
        db,
        invoice,
        costs=[{"invoice_id": "inv-1", "cost_code_id": "cc-1", "amount": "900.00"}],
    )
    with pytest.raises(TransitionError) as e:
        state_machine.apply(
            invoice=invoice, transition=state_machine.APPROVE, actor=approver()
        )
    assert e.value.status_code == 422
    assert "$100.00" in str(e.value)


def test_approve_succeeds_when_everything_checks_out(db):
    invoice = ready_invoice(
        status="pending_approval", reviewed_at="2026-09-29T10:00:00Z"
    )
    seed(db, invoice)
    updated = state_machine.apply(
        invoice=invoice, transition=state_machine.APPROVE, actor=approver()
    )
    assert updated["status"] == "approved"
    assert updated["approved_at"] is not None


def test_a_zero_amount_invoice_can_balance_and_be_approved(db):
    """A $0 corrected bill is real and must not be permanently unapprovable."""
    invoice = ready_invoice(
        status="pending_approval", amount="0.00",
        reviewed_at="2026-09-29T10:00:00Z",
    )
    seed(
        db,
        invoice,
        costs=[{"invoice_id": "inv-1", "cost_code_id": "cc-1", "amount": "0.00"}],
    )
    updated = state_machine.apply(
        invoice=invoice, transition=state_machine.APPROVE, actor=approver()
    )
    assert updated["status"] == "approved"


def test_a_credit_memo_balances_negative_and_can_be_approved(db):
    invoice = ready_invoice(
        status="pending_approval", amount="-500.00",
        reviewed_at="2026-09-29T10:00:00Z",
    )
    seed(
        db,
        invoice,
        costs=[{"invoice_id": "inv-1", "cost_code_id": "cc-1", "amount": "-500.00"}],
    )
    updated = state_machine.apply(
        invoice=invoice, transition=state_machine.APPROVE, actor=approver()
    )
    assert updated["status"] == "approved"


# ─── Reject ───────────────────────────────────────────────────────────


def test_reject_sends_it_back_and_clears_the_review_stamp(db):
    """§7.5 sends it back to `assigned`. Clearing reviewed_at matters: with it
    still set, the invoice could be approved again without a second review,
    because require_reviewed only checks that the stamp exists."""
    invoice = ready_invoice(
        status="pending_approval", reviewed_at="2026-09-29T10:00:00Z"
    )
    seed(db, invoice)
    updated = state_machine.apply(
        invoice=invoice,
        transition=state_machine.REJECT,
        actor=approver(),
        extra_fields={"reject_reason": "Wrong cost code — this was a deck pour."},
    )
    assert updated["status"] == "assigned"
    assert updated["reviewed_at"] is None
    assert updated["rejected_at"] is not None
    assert "deck pour" in updated["reject_reason"]


def test_reject_keeps_the_same_reviewer(db):
    invoice = ready_invoice(
        status="pending_approval", reviewed_at="2026-09-29T10:00:00Z"
    )
    seed(db, invoice)
    updated = state_machine.apply(
        invoice=invoice, transition=state_machine.REJECT, actor=approver(),
        extra_fields={"reject_reason": "x"},
    )
    assert updated["reviewer_id"] == REVIEWER


# ─── Record-level permission ──────────────────────────────────────────


def test_the_assigned_reviewer_may_review():
    state_machine.require_assigned_reviewer(ready_invoice(), reviewer())


def test_another_project_engineer_may_not_review_someone_elses_invoice():
    """The audit trail has to record that the person asked is the person who
    certified it."""
    with pytest.raises(TransitionError) as e:
        state_machine.require_assigned_reviewer(ready_invoice(), other_reviewer())
    assert e.value.status_code == 403
    assert "assigned to someone else" in str(e.value)


def test_an_admin_may_review_anything():
    state_machine.require_assigned_reviewer(ready_invoice(), admin())


def test_an_accountant_may_review_anything():
    """Linda manages the queue and is herself a reviewer."""
    state_machine.require_assigned_reviewer(ready_invoice(), accountant())


def test_the_chrome_session_may_never_review():
    """§12: nothing reaches BuilderTrend without a HUMAN review stamp."""
    with pytest.raises(TransitionError) as e:
        state_machine.require_assigned_reviewer(ready_invoice(), agent())
    assert e.value.status_code == 403
    assert "Chrome session cannot review" in str(e.value)


def test_the_assigned_approver_may_approve():
    state_machine.require_assigned_approver(ready_invoice(), approver())


def test_another_approver_may_not_clear_someone_elses_invoice():
    """Otherwise the approver field is decorative and the audit trail lies
    about who was asked. Accounting reassigns instead, which is recorded."""
    with pytest.raises(TransitionError) as e:
        state_machine.require_assigned_approver(ready_invoice(), other_approver())
    assert e.value.status_code == 403
    assert "different approver" in str(e.value)


def test_someone_without_the_approver_role_may_not_approve():
    with pytest.raises(TransitionError) as e:
        state_machine.require_assigned_approver(ready_invoice(), reviewer())
    assert "approver role" in str(e.value)


def test_an_admin_may_approve_so_nothing_gets_permanently_stuck():
    state_machine.require_assigned_approver(ready_invoice(), admin())


def test_the_chrome_session_may_never_approve():
    with pytest.raises(TransitionError) as e:
        state_machine.require_assigned_approver(ready_invoice(), agent())
    assert e.value.status_code == 403
    assert "human approval stamp" in str(e.value)


# ─── Atomicity ────────────────────────────────────────────────────────


def test_a_stale_read_cannot_write_over_a_concurrent_transition(monkeypatch):
    """Two callers who both read `approved` must not both write.

    Exercised the way it actually happens: the caller holds an invoice dict
    read a moment ago, and the row has moved since. The from_states check
    passes — it is looking at the stale copy — so the only thing standing
    between the two writers is the status being matched in the UPDATE's own
    filter. For approve, losing that is two approval stamps from two tabs;
    for mark-uploaded it is two audit entries each claiming to be the first
    save of the same bill.
    """
    from app.core import audit, state_machine as sm
    from tests.fakes import FakeClient, FakeTable

    client = FakeClient()
    client.tables["invoices"] = FakeTable(
        "invoices",
        [
            {
                "id": "inv-1",
                # Somebody else already moved it.
                "status": "uploaded",
                "reviewed_at": "2026-09-20T10:00:00+00:00",
                "approved_at": "2026-09-21T10:00:00+00:00",
            }
        ],
    )
    monkeypatch.setattr(sm, "_sb", lambda: client)
    monkeypatch.setattr(audit, "get_service_client", lambda: client)

    stale = {
        "id": "inv-1",
        "status": "approved",
        "reviewed_at": "2026-09-20T10:00:00+00:00",
        "approved_at": "2026-09-21T10:00:00+00:00",
    }

    with pytest.raises(sm.TransitionError) as e:
        sm.apply(
            invoice=stale,
            transition=sm.MARK_UPLOADED,
            actor=Actor(is_agent=True),
            extra_fields={"bt_bill_id": "BT-2"},
        )
    assert e.value.status_code == 409
    assert "moved while" in str(e.value)
    # Nothing was written: the other caller's bill id is not overwritten.
    assert client.tables["invoices"].rows[0].get("bt_bill_id") is None
    # And no audit row claims a second transition happened.
    assert not client.tables.get("audit_log", FakeTable("audit_log")).rows
