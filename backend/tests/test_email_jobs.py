"""
Recipient selection and idempotency for the two daily emails (prompt §9).

The rules with real consequences are "skip people with nothing pending" and
"only send the summary if something was approved today" — a digest that is
usually empty stops being read, and then the one that matters gets ignored
with it. Plus the idempotency: a double cron fire must not double-mail.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.core import email, email_jobs, email_templates
from app.core.config import settings

from tests.test_intake_idempotency import FakeClient, FakeTable


PE = "u-pe"
RAZ = "u-raz"
LINDA = "u-linda"


@pytest.fixture
def db(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(email_jobs, "_sb", lambda: client)
    monkeypatch.setattr(email, "_sb", lambda: client)
    client.tables["app_users"] = FakeTable(
        "app_users",
        [
            {"id": PE, "name": "Sam", "email": "sam@f.com", "role": ["pe"],
             "deactivated_at": None},
            {"id": RAZ, "name": "Raz", "email": "raz@f.com", "role": ["approver"],
             "deactivated_at": None},
            {"id": LINDA, "name": "Linda", "email": "linda@f.com",
             "role": ["accountant", "approver"], "deactivated_at": None},
        ],
    )
    client.tables["projects"] = FakeTable(
        "projects", [{"id": "p-1", "project_no": "25-20", "name": "A Street Flats"}]
    )
    client.tables["vendors"] = FakeTable(
        "vendors", [{"id": "v-1", "invoice_name": "CalPortland"}]
    )
    client.tables["cost_codes"] = FakeTable(
        "cost_codes", [{"id": "cc-1", "code": "3002 - Concrete Columns"}]
    )
    client.tables["invoice_suggestions"] = FakeTable("invoice_suggestions", [])
    client.tables["invoice_costs"] = FakeTable("invoice_costs", [])
    client.tables["distribution_list"] = FakeTable("distribution_list", [])
    client.tables["email_log"] = FakeTable("email_log", [])
    client.tables["invoices"] = FakeTable("invoices", [])
    return client


@pytest.fixture(autouse=True)
def no_real_sending(monkeypatch):
    """Never reach Gmail. Record what would have gone out instead."""
    sent: list[dict] = []

    def fake_send(*, kind, recipient, subject, body_html,
                  body_text=None, invoice_ids=None, force=False):
        sent.append(
            {"kind": kind, "recipient": recipient, "subject": subject,
             "html": body_html, "invoice_ids": invoice_ids or []}
        )
        return email.SendResult("sent", recipient, log_id="log-1")

    monkeypatch.setattr(email_jobs.email, "send", fake_send)
    monkeypatch.setattr(email_jobs.email, "alert_admin", lambda **k: None)
    return sent


def invoice(**over):
    base = {
        "id": "inv-1",
        "invoice_no": "278461",
        "amount": "2294.25",
        "project_id": "p-1",
        "vendor_id": "v-1",
        "reviewer_id": PE,
        "approver_id": RAZ,
        "status": "assigned",
        "created_at": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
    }
    base.update(over)
    return base


# ─── §9.1 Daily "in your court" ───────────────────────────────────────


def test_a_reviewer_with_pending_work_gets_one_email(db, no_real_sending):
    db.tables["invoices"] = FakeTable("invoices", [invoice()])
    result = email_jobs.send_daily_court(force=True)
    assert result.sent == 1
    assert no_real_sending[0]["recipient"] == "sam@f.com"
    assert no_real_sending[0]["subject"].endswith("waiting for your review")


def test_an_approver_gets_the_approval_wording(db, no_real_sending):
    db.tables["invoices"] = FakeTable(
        "invoices", [invoice(status="pending_approval")]
    )
    email_jobs.send_daily_court(force=True)
    assert no_real_sending[0]["recipient"] == "raz@f.com"
    assert no_real_sending[0]["subject"].endswith("waiting for your approval")


def test_nobody_is_emailed_when_nothing_is_pending(db, no_real_sending):
    """§9.1: "Skip people with nothing pending." """
    db.tables["invoices"] = FakeTable(
        "invoices", [invoice(status="approved"), invoice(id="inv-2", status="filed")]
    )
    result = email_jobs.send_daily_court(force=True)
    assert result.sent == 0
    assert no_real_sending == []
    assert "Nothing is assigned" in result.notes[0]


def test_someone_in_both_roles_gets_two_emails_not_one_merged_list(db, no_real_sending):
    """Linda is accountant and approver. The two piles need different actions,
    so one table would bury which is which."""
    db.tables["invoices"] = FakeTable(
        "invoices",
        [
            invoice(id="inv-1", status="assigned", reviewer_id=LINDA),
            invoice(id="inv-2", status="pending_approval", approver_id=LINDA),
        ],
    )
    result = email_jobs.send_daily_court(force=True)
    assert result.sent == 2
    subjects = sorted(s["subject"] for s in no_real_sending)
    assert "review" in subjects[1]
    assert "approval" in subjects[0]


def test_each_person_gets_only_their_own_invoices(db, no_real_sending):
    db.tables["invoices"] = FakeTable(
        "invoices",
        [
            invoice(id="inv-1", reviewer_id=PE),
            invoice(id="inv-2", reviewer_id=LINDA),
        ],
    )
    email_jobs.send_daily_court(force=True)
    by_recipient = {s["recipient"]: s["invoice_ids"] for s in no_real_sending}
    assert by_recipient["sam@f.com"] == ["inv-1"]
    assert by_recipient["linda@f.com"] == ["inv-2"]


def test_a_deactivated_person_is_skipped_and_reported(db, no_real_sending):
    """Their invoices would otherwise vanish from every digest silently."""
    db.tables["app_users"].rows[0]["deactivated_at"] = "2026-09-01T00:00:00Z"
    db.tables["invoices"] = FakeTable("invoices", [invoice()])
    result = email_jobs.send_daily_court(force=True)
    assert result.sent == 0
    assert any("deactivated" in n for n in result.notes)


def test_an_invoice_waiting_with_nobody_assigned_is_reported(db, no_real_sending):
    """It appears in no digest at all, so it would sit indefinitely with no
    signal that anything is wrong."""
    db.tables["invoices"] = FakeTable(
        "invoices", [invoice(reviewer_id=None)]
    )
    result = email_jobs.send_daily_court(force=True)
    assert result.sent == 0
    assert any("nobody assigned" in n for n in result.notes)


def test_the_kill_switch_stops_the_digest(db, no_real_sending, monkeypatch):
    """§12: JOBS_PAUSED pauses both email jobs without a deploy."""
    db.tables["invoices"] = FakeTable("invoices", [invoice()])
    monkeypatch.setattr(settings, "jobs_paused", True)
    result = email_jobs.send_daily_court()
    assert result.sent == 0
    assert "JOBS_PAUSED" in result.notes[0]
    assert no_real_sending == []


def test_weekends_are_skipped_by_default(db, no_real_sending, monkeypatch):
    """§14: weekdays only."""
    db.tables["invoices"] = FakeTable("invoices", [invoice()])
    monkeypatch.setattr(
        email_jobs, "_local_today", lambda: __import__("datetime").date(2026, 10, 3)
    )  # a Saturday
    result = email_jobs.send_daily_court()
    assert result.sent == 0
    assert "not a weekday" in result.notes[0]


# ─── §9.2 End-of-day summary ──────────────────────────────────────────


def approved_today(**over):
    base = invoice(
        status="approved",
        approved_at=datetime.now(timezone.utc).isoformat(),
    )
    base.update(over)
    return base


def test_a_quiet_day_sends_no_summary(db, no_real_sending):
    """§9.2: "Only sent if at least one invoice was approved that day." """
    db.tables["invoices"] = FakeTable("invoices", [invoice()])
    result = email_jobs.send_eod_summary(force=True)
    assert result.sent == 0
    assert "Nothing was approved today" in result.notes[0]


def test_the_summary_goes_to_the_whole_distribution_list(db, no_real_sending):
    db.tables["invoices"] = FakeTable("invoices", [approved_today()])
    db.tables["distribution_list"] = FakeTable(
        "distribution_list",
        [
            {"email": "a@f.com", "name": "A", "active": True},
            {"email": "b@f.com", "name": "B", "active": True},
        ],
    )
    result = email_jobs.send_eod_summary(force=True)
    assert result.sent == 2
    assert {s["recipient"] for s in no_real_sending} == {"a@f.com", "b@f.com"}


def test_inactive_distribution_entries_are_not_mailed(db, no_real_sending):
    db.tables["invoices"] = FakeTable("invoices", [approved_today()])
    db.tables["distribution_list"] = FakeTable(
        "distribution_list",
        [
            {"email": "a@f.com", "name": "A", "active": True},
            {"email": "paused@f.com", "name": "P", "active": False},
        ],
    )
    result = email_jobs.send_eod_summary(force=True)
    assert result.sent == 1
    assert no_real_sending[0]["recipient"] == "a@f.com"


def test_an_empty_distribution_list_is_reported_not_silent(db, no_real_sending):
    """Invoices were approved and the summary had nowhere to go — that is a
    configuration problem someone has to hear about."""
    db.tables["invoices"] = FakeTable("invoices", [approved_today()])
    result = email_jobs.send_eod_summary(force=True)
    assert result.sent == 0
    assert any("nowhere to go" in n for n in result.notes)


def test_only_todays_approvals_are_included(db, no_real_sending):
    old = approved_today(
        id="inv-old",
        approved_at=(datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
    )
    db.tables["invoices"] = FakeTable("invoices", [approved_today(), old])
    db.tables["distribution_list"] = FakeTable(
        "distribution_list", [{"email": "a@f.com", "name": "A", "active": True}]
    )
    email_jobs.send_eod_summary(force=True)
    assert no_real_sending[0]["invoice_ids"] == ["inv-1"]


def test_the_kill_switch_stops_the_summary(db, no_real_sending, monkeypatch):
    db.tables["invoices"] = FakeTable("invoices", [approved_today()])
    monkeypatch.setattr(settings, "jobs_paused", True)
    result = email_jobs.send_eod_summary()
    assert result.sent == 0
    assert "JOBS_PAUSED" in result.notes[0]


# ─── Templates ────────────────────────────────────────────────────────


def test_the_court_subject_matches_the_spec_wording():
    """§9.1 names the subject lines explicitly."""
    assert email_templates.daily_court_subject(count=4, role="reviewer") == (
        "4 invoices waiting for your review"
    )
    assert email_templates.daily_court_subject(count=1, role="approver") == (
        "1 invoice waiting for your approval"
    )


def test_confidence_colour_matches_the_app_thresholds():
    """Gold in the mail has to mean what gold means on the screen."""
    assert email_templates.confidence_color(0.95) == email_templates.GREEN
    assert email_templates.confidence_color(0.70) == email_templates.AMBER
    assert email_templates.confidence_color(0.20) == email_templates.RED
    assert email_templates.confidence_color(None) == email_templates.TEXT_FAINT


def test_the_court_email_links_every_row_to_its_invoice():
    html = email_templates.daily_court_html(
        name="Sam",
        role="reviewer",
        invoices=[
            {"id": "inv-9", "vendor_name": "CalPortland", "project_name": "A Street",
             "project_no": "25-20", "amount": "2294.25", "invoice_no": "278461",
             "suggested_code": "3002 - Concrete Columns",
             "suggested_confidence": 0.7, "age_days": 3}
        ],
    )
    assert "/invoices/inv-9" in html
    assert "3002 - Concrete Columns" in html
    assert "$2,294.25" in html
    assert "70%" in html


def test_the_court_email_flags_a_missing_suggestion_in_red():
    html = email_templates.daily_court_html(
        name="Sam", role="reviewer",
        invoices=[{"id": "i", "vendor_name": "V", "project_name": "P",
                   "amount": "1", "suggested_code": None,
                   "suggested_confidence": None, "age_days": 0}],
    )
    assert "no suggestion" in html
    assert email_templates.RED in html


def test_a_credit_memo_renders_in_accounting_parentheses():
    html = email_templates.daily_court_html(
        name="Sam", role="reviewer",
        invoices=[{"id": "i", "vendor_name": "V", "project_name": "P",
                   "amount": "-500.00", "suggested_code": "3015 - Hardware & Misc",
                   "suggested_confidence": 0.9, "age_days": 1}],
    )
    assert "($500.00)" in html


def test_vendor_names_are_html_escaped():
    """A vendor called 'Smith & Sons <Inc>' must not break the markup."""
    html = email_templates.daily_court_html(
        name="Sam", role="reviewer",
        invoices=[{"id": "i", "vendor_name": "Smith & Sons <Inc>",
                   "project_name": "P", "amount": "1",
                   "suggested_code": None, "suggested_confidence": None,
                   "age_days": 0}],
    )
    assert "Smith &amp; Sons &lt;Inc&gt;" in html
    assert "<Inc>" not in html


def test_the_eod_email_groups_by_project_and_shows_the_counts():
    html = email_templates.eod_html(
        groups=[
            {
                "project_name": "A Street Flats",
                "project_no": "25-20",
                "invoices": [
                    {"id": "i1", "vendor_name": "CalPortland",
                     "invoice_no": "278461", "amount": "2294.25",
                     "cost_codes": ["3002 - Concrete Columns"],
                     "reviewer_name": "Sam", "approver_name": "Raz"}
                ],
            }
        ],
        counts={"approved_today": 1, "uploaded_today": 0,
                "still_pending": 5, "flagged": 2},
    )
    assert "A Street Flats" in html
    assert "25-20" in html
    assert "Sam" in html and "Raz" in html
    assert "Approved today" in html and "Flagged" in html
    assert "$2,294.25" in html


def test_html_to_text_gives_a_readable_fallback():
    text = email.html_to_text(
        "<p>Two invoices</p><table><tr><td>CalPortland</td>"
        "<td>$100.00</td></tr></table>"
    )
    assert "Two invoices" in text
    assert "CalPortland" in text
    assert "<" not in text
