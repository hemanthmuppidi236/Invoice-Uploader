"""
The two guards standing between an approved invoice and a bill in
BuilderTrend (prompt §12, SOP §8.4).

**§12** — "No invoice reaches BuilderTrend without a human `reviewed` and a
human `approved` stamp. Enforce in the mark-uploaded endpoint: reject if
`approved_at` is null." This is the whole point of the app: the old process
had accounting guessing a cost code after the fact, and the new one is only
better if the two human stamps are real.

**SOP §8.4** — "Never click Save twice without checking whether the first one
fired — that's how duplicate bills get created." Whatever the session does in
the browser, the app has to make the *reporting* of a save unambiguous: the
same bill reported twice is a replay, a different bill reported twice is a
duplicate that someone has to go delete.
"""

from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.api import invoices as api
from app.core import state_machine
from app.core.auth import Actor
from app.schemas.invoices import InvoiceDetail, MarkFiledIn, MarkUploadedIn
from tests.fakes import FakeClient, FakeTable


AGENT = Actor(is_agent=True)


# ─── §12: the human stamps ────────────────────────────────────────────


def test_no_approval_stamp_is_refused():
    problem = state_machine.require_human_stamps(
        {"reviewed_at": "2026-09-20T10:00:00Z", "approved_at": None}
    )
    assert problem and "approval stamp" in problem


def test_no_review_stamp_is_refused_even_with_an_approval_stamp():
    """Both stamps, not either. An approval without a review means the cost
    code was never checked by the person who did the work — the exact failure
    the app exists to remove."""
    problem = state_machine.require_human_stamps(
        {"reviewed_at": None, "approved_at": "2026-09-21T10:00:00Z"}
    )
    assert problem and "review stamp" in problem


def test_both_stamps_pass():
    assert (
        state_machine.require_human_stamps(
            {
                "reviewed_at": "2026-09-20T10:00:00Z",
                "approved_at": "2026-09-21T10:00:00Z",
            }
        )
        is None
    )


def test_the_guard_tells_the_session_not_to_save_rather_than_just_failing():
    """The session reaches this endpoint AFTER driving the form. If the app
    says no, the message has to be an instruction, not a status code."""
    problem = state_machine.require_human_stamps({"approved_at": None})
    assert "Do not save it" in problem


def test_mark_uploaded_only_accepts_an_approved_invoice():
    assert state_machine.MARK_UPLOADED.from_states == ("approved",)
    assert state_machine.MARK_UPLOADED.precondition is state_machine.require_human_stamps


def test_the_agent_is_not_trusted_with_either_human_stamp():
    """§11 scopes the agent key to list-approved, mark-uploaded, mark-filed
    and flag. §12 is why: the session can report what it did, never authorise
    it."""
    with pytest.raises(state_machine.TransitionError) as e:
        state_machine.require_assigned_reviewer({}, AGENT)
    assert e.value.status_code == 403

    with pytest.raises(state_machine.TransitionError) as e:
        state_machine.require_assigned_approver({}, AGENT)
    assert e.value.status_code == 403


# ─── SOP §8.4: replay versus duplicate ────────────────────────────────


APPROVED = {
    "id": "inv-1",
    "status": "approved",
    "invoice_no": "278461-1",
    "amount": Decimal("2600.00"),
    "reviewed_at": "2026-09-20T10:00:00+00:00",
    "approved_at": "2026-09-21T10:00:00+00:00",
    "bt_bill_id": None,
    "filed_at": None,
    "filing_warnings": [],
}


@pytest.fixture
def db(monkeypatch):
    client = FakeClient()
    client.tables["invoices"] = FakeTable("invoices", [dict(APPROVED)])
    monkeypatch.setattr(api, "_sb", lambda: client)
    monkeypatch.setattr(state_machine, "_sb", lambda: client)

    from app.core import audit, filing

    monkeypatch.setattr(audit, "get_service_client", lambda: client)
    # The detail payload is assembled by its own battery of queries; the
    # guards under test here are all decided before it is built, so it is
    # stubbed down to the two fields the response model requires.
    def _stub_detail(invoice_id):
        row = next(
            (r for r in client.tables["invoices"].rows if r["id"] == invoice_id), {}
        )
        return InvoiceDetail(id=invoice_id, status=row.get("status", "approved"))

    monkeypatch.setattr(api, "_detail", _stub_detail)
    monkeypatch.setattr(
        filing,
        "file_and_record",
        lambda **kw: {
            "filed": True,
            "filed_path": "BT Invoices/A/B/09-14-26 $2,600.00.pdf",
            "error": None,
            "needs_folder": False,
            "warnings": [],
            "invoice": {"original_archived": True},
        },
    )
    return client


def _rows(db):
    return db.tables["invoices"].rows


def test_a_first_report_records_the_bill_id_and_stamps_uploaded(db):
    out = api.mark_uploaded("inv-1", MarkUploadedIn(bt_bill_id="BT-99001"), actor=AGENT)
    row = _rows(db)[0]
    assert row["status"] == "uploaded"
    assert row["bt_bill_id"] == "BT-99001"
    assert row["uploaded_at"]
    assert out.already_recorded is False


def test_the_same_bill_id_reported_twice_is_a_quiet_success(db):
    """A session that lost its connection mid-batch has to be able to replay
    without a scary error, or the operator starts guessing."""
    api.mark_uploaded("inv-1", MarkUploadedIn(bt_bill_id="BT-99001"), actor=AGENT)
    before = dict(_rows(db)[0])

    out = api.mark_uploaded("inv-1", MarkUploadedIn(bt_bill_id="BT-99001"), actor=AGENT)
    assert out.already_recorded is True
    # And nothing moved: no second filing, no re-stamp.
    assert _rows(db)[0]["uploaded_at"] == before["uploaded_at"]


def test_a_different_bill_id_on_the_same_invoice_is_a_loud_409(db):
    """Two bill ids for one invoice means two bills exist. This is the SOP
    §8.4 failure, and it needs a person, not a retry."""
    api.mark_uploaded("inv-1", MarkUploadedIn(bt_bill_id="BT-99001"), actor=AGENT)

    with pytest.raises(HTTPException) as e:
        api.mark_uploaded("inv-1", MarkUploadedIn(bt_bill_id="BT-99002"), actor=AGENT)
    assert e.value.status_code == 409
    assert "two bills exist" in e.value.detail
    assert "delete the duplicate" in e.value.detail
    # The original record is untouched, so the audit trail still says which
    # bill was reported first.
    assert _rows(db)[0]["bt_bill_id"] == "BT-99001"


def test_the_same_bill_id_on_a_different_invoice_is_refused(db):
    """The other half of the same failure: a save that landed on the wrong
    record. Without this, two invoices both look uploaded and only one bill
    exists."""
    db.tables["invoices"].rows.append(
        {**APPROVED, "id": "inv-2", "invoice_no": "278999"}
    )
    api.mark_uploaded("inv-1", MarkUploadedIn(bt_bill_id="BT-99001"), actor=AGENT)

    with pytest.raises(HTTPException) as e:
        api.mark_uploaded("inv-2", MarkUploadedIn(bt_bill_id="BT-99001"), actor=AGENT)
    assert e.value.status_code == 409
    assert "278461-1" in e.value.detail


def test_an_unapproved_invoice_is_refused_with_a_422(db):
    db.tables["invoices"].rows[0].update(
        {"status": "pending_approval", "approved_at": None}
    )
    with pytest.raises(HTTPException) as e:
        api.mark_uploaded("inv-1", MarkUploadedIn(bt_bill_id="BT-1"), actor=AGENT)
    # 409 on the state, since `pending_approval` is not in from_states at all.
    assert e.value.status_code == 409
    assert _rows(db)[0].get("bt_bill_id") is None


def test_an_approved_row_with_no_stamps_is_refused_by_the_precondition(db):
    """Belt and braces: `approved` implies both stamps today, but §12 names
    the check explicitly and it should not depend on the status column being
    the only way in."""
    db.tables["invoices"].rows[0].update({"reviewed_at": None, "approved_at": None})
    with pytest.raises(HTTPException) as e:
        api.mark_uploaded("inv-1", MarkUploadedIn(bt_bill_id="BT-1"), actor=AGENT)
    assert e.value.status_code == 422
    assert _rows(db)[0].get("bt_bill_id") is None


def test_an_empty_bill_id_is_rejected_by_the_schema():
    """An unidentifiable save cannot be told apart from a second bill, so it
    must not be recordable at all."""
    with pytest.raises(Exception):
        MarkUploadedIn(bt_bill_id="   ")


def test_a_smuggled_status_is_a_422_not_a_silent_write():
    with pytest.raises(Exception):
        MarkUploadedIn(bt_bill_id="BT-1", status="filed")


# ─── Filing transitions ───────────────────────────────────────────────


def test_filing_runs_automatically_after_a_successful_upload(db):
    out = api.mark_uploaded("inv-1", MarkUploadedIn(bt_bill_id="BT-99001"), actor=AGENT)
    assert out.filing.filed is True
    assert out.filing.filed_path.endswith("09-14-26 $2,600.00.pdf")


def test_a_filing_failure_leaves_the_upload_recorded(db, monkeypatch):
    """The bill IS in BuilderTrend. Rolling back the upload stamp because
    Drive refused would make a successful save look like a failed one, and
    the session would try it again."""
    from app.core import filing

    monkeypatch.setattr(
        filing,
        "file_and_record",
        lambda **kw: {
            "filed": False,
            "filed_path": None,
            "error": "No folder named 'A Street Flats' under BT Invoices.",
            "needs_folder": True,
            "warnings": [],
            "invoice": None,
        },
    )
    out = api.mark_uploaded("inv-1", MarkUploadedIn(bt_bill_id="BT-99001"), actor=AGENT)
    assert _rows(db)[0]["status"] == "uploaded"
    assert _rows(db)[0]["bt_bill_id"] == "BT-99001"
    assert out.filing.filed is False
    assert out.filing.needs_folder is True


def test_mark_filed_refuses_before_the_bill_exists(db):
    """Filing archives the Drive original out of the intake folder. Doing
    that before the bill is saved would hide an unentered invoice."""
    with pytest.raises(HTTPException) as e:
        api.mark_filed("inv-1", MarkFiledIn(), actor=AGENT)
    assert e.value.status_code == 409
    assert "nothing to file yet" in e.value.detail


def test_mark_filed_is_idempotent_once_filed(db):
    db.tables["invoices"].rows[0].update(
        {
            "status": "filed",
            "filed_at": "2026-09-22T00:00:00+00:00",
            "filed_path": "BT Invoices/A/B/x.pdf",
        }
    )
    out = api.mark_filed("inv-1", MarkFiledIn(), actor=AGENT)
    assert out.already_recorded is True
    assert out.filing.filed is True


def test_an_externally_filed_copy_can_be_recorded(db):
    """§14's escape hatch: Chrome may do the filing if Drive permissions make
    the backend route awkward. A path in the payload says that happened."""
    db.tables["invoices"].rows[0].update(
        {"status": "uploaded", "bt_bill_id": "BT-99001"}
    )
    out = api.mark_filed(
        "inv-1",
        MarkFiledIn(filed_path="BT Invoices/A/B/09-14-26 $2,600.00.pdf",
                    original_archived=True),
        actor=AGENT,
    )
    assert _rows(db)[0]["status"] == "filed"
    assert out.filing.original_archived is True


def test_an_externally_filed_copy_with_no_archive_says_so(db):
    db.tables["invoices"].rows[0].update(
        {"status": "uploaded", "bt_bill_id": "BT-99001"}
    )
    api.mark_filed(
        "inv-1", MarkFiledIn(filed_path="BT Invoices/A/B/x.pdf"), actor=AGENT
    )
    warnings = _rows(db)[0]["filing_warnings"]
    assert any("still in the intake folder" in w for w in warnings)


# ─── Flagging from the session (§7.6 step 5) ──────────────────────────


def test_a_bill_already_in_buildertrend_cannot_go_back_to_triage(db):
    """/flagged offers void, re-run the AI, and reassign — all of which
    contradict a bill that already exists in BuilderTrend."""
    from app.schemas.invoices import FlagIn

    db.tables["invoices"].rows[0].update({"status": "uploaded"})
    with pytest.raises(HTTPException) as e:
        api.flag("inv-1", FlagIn(code="stop_and_ask", detail="wrong job"), actor=AGENT)
    assert e.value.status_code == 409
    assert "already in BuilderTrend" in e.value.detail


# ─── The database-level duplicate guard ───────────────────────────────


class FakeAPIError(Exception):
    """Shaped like postgrest.exceptions.APIError."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def test_a_unique_violation_on_the_bill_id_is_recognised():
    e = FakeAPIError(
        "23505",
        'duplicate key value violates unique constraint "idx_invoices_bt_bill_id"',
    )
    assert api._is_duplicate_bill_id_violation(e) is True


def test_an_unrelated_unique_violation_is_not_mislabelled():
    """A 23505 on some other index is a genuine 500 with an error id, not a
    duplicate bill. Mislabelling it would send the operator to BuilderTrend
    looking for a bill that does not exist."""
    e = FakeAPIError(
        "23505",
        'duplicate key value violates unique constraint "idx_invoices_source"',
    )
    assert api._is_duplicate_bill_id_violation(e) is False


def test_an_arbitrary_exception_is_not_a_duplicate_bill():
    assert api._is_duplicate_bill_id_violation(RuntimeError("timeout")) is False
    assert api._is_duplicate_bill_id_violation(Exception()) is False
