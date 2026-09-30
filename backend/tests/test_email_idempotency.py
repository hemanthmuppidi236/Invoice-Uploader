"""
Send-once-per-day idempotency (prompt §9).

The design: the email_log row is INSERTED first, claiming the slot, then the
send happens, then success stamps sent_at and failure writes the error. The
unique index is partial — `WHERE error IS NULL` — so a failure drops the row
out of it and frees the slot for a legitimate retry, while a success holds it.

Claiming after sending would leave a race where two cron firings both send.
Claiming without releasing on failure would make a transient Gmail error
permanent for the rest of the day. Both directions are tested here.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.core import email
from app.core.config import settings

from tests.test_intake_idempotency import FakeClient, FakeTable


@pytest.fixture
def db(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(email, "_sb", lambda: client)
    client.tables["email_log"] = FakeTable("email_log", [])
    return client


@pytest.fixture
def sending_enabled(monkeypatch):
    """Pretend Gmail is configured, and capture the transport call."""
    monkeypatch.setattr(
        type(settings), "email_enabled", property(lambda self: True)
    )
    calls: list[str] = []
    monkeypatch.setattr(
        email, "_send_via_gmail", lambda msg: calls.append(msg["To"])
    )
    return calls


def _send(**over):
    payload = {
        "kind": email.KIND_DAILY_COURT,
        "recipient": "sam@f.com",
        "subject": "3 invoices are waiting for your review",
        "body_html": "<p>hi</p>",
    }
    payload.update(over)
    return email.send(**payload)


# ─── The happy path ───────────────────────────────────────────────────


def test_a_successful_send_is_logged_with_a_sent_timestamp(db, sending_enabled):
    result = _send()
    assert result.status == "sent"
    row = db.tables["email_log"].rows[0]
    assert row["kind"] == email.KIND_DAILY_COURT
    assert row["recipient"] == "sam@f.com"
    assert row["sent_at"] is not None
    assert row.get("error") is None
    assert sending_enabled == ["sam@f.com"]


def test_the_slot_is_claimed_before_the_send(db, sending_enabled, monkeypatch):
    """A row has to exist even if the transport blows up, or a crash
    mid-send would leave no record that we tried."""
    def boom(_msg):
        assert db.tables["email_log"].rows, "the claim must precede the send"
        raise RuntimeError("Gmail 503")

    monkeypatch.setattr(email, "_send_via_gmail", boom)
    result = _send()
    assert result.status == "failed"
    assert len(db.tables["email_log"].rows) == 1


# ─── Duplicate suppression ────────────────────────────────────────────


def test_a_second_send_the_same_day_is_skipped(db, sending_enabled):
    """A double cron fire must not double-mail."""
    assert _send().status == "sent"
    second = _send()
    assert second.status == "skipped_duplicate"
    assert len(sending_enabled) == 1, "the transport must not be called twice"


def test_a_different_recipient_is_not_suppressed(db, sending_enabled):
    _send(recipient="sam@f.com")
    assert _send(recipient="raz@f.com").status == "sent"
    assert len(sending_enabled) == 2


def test_a_different_kind_to_the_same_person_is_not_suppressed(db, sending_enabled):
    """Linda legitimately gets both a court digest and the EOD summary."""
    _send(kind=email.KIND_DAILY_COURT, recipient="linda@f.com")
    result = _send(kind=email.KIND_EOD_SUMMARY, recipient="linda@f.com")
    assert result.status == "sent"
    assert len(sending_enabled) == 2


def test_yesterdays_send_does_not_suppress_todays(db, sending_enabled):
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    db.tables["email_log"] = FakeTable(
        "email_log",
        [{"id": "old", "kind": email.KIND_DAILY_COURT, "recipient": "sam@f.com",
          "sent_at": yesterday, "error": None, "created_at": yesterday}],
    )
    assert _send().status == "sent"


def test_force_bypasses_the_duplicate_check(db, sending_enabled):
    """For a deliberate re-send after fixing a template."""
    _send()
    assert _send(force=True).status == "sent"
    assert len(sending_enabled) == 2


# ─── Failure releases the slot ────────────────────────────────────────


def test_a_failed_send_records_the_error(db, sending_enabled, monkeypatch):
    monkeypatch.setattr(
        email, "_send_via_gmail",
        lambda _m: (_ for _ in ()).throw(RuntimeError("Gmail 503")),
    )
    result = _send()
    assert result.status == "failed"
    assert "Gmail 503" in result.error
    row = db.tables["email_log"].rows[0]
    assert "Gmail 503" in row["error"]
    assert row.get("sent_at") is None


def test_a_failure_frees_the_slot_so_a_retry_can_try_again(db, monkeypatch):
    """The index is `WHERE error IS NULL`, so writing the error drops the row
    out of it. Without that, one transient 503 would block the digest for the
    rest of the day."""
    monkeypatch.setattr(
        type(settings), "email_enabled", property(lambda self: True)
    )
    monkeypatch.setattr(
        email, "_send_via_gmail",
        lambda _m: (_ for _ in ()).throw(RuntimeError("transient")),
    )
    assert _send().status == "failed"

    succeeded: list[str] = []
    monkeypatch.setattr(
        email, "_send_via_gmail", lambda m: succeeded.append(m["To"])
    )
    retry = _send()
    assert retry.status == "sent", "a retry after a failure must be allowed"
    assert succeeded == ["sam@f.com"]


def test_a_long_error_is_truncated_rather_than_rejected(db, monkeypatch):
    monkeypatch.setattr(
        type(settings), "email_enabled", property(lambda self: True)
    )
    monkeypatch.setattr(
        email, "_send_via_gmail",
        lambda _m: (_ for _ in ()).throw(RuntimeError("x" * 5000)),
    )
    _send()
    assert len(db.tables["email_log"].rows[0]["error"]) == 2000


# ─── Disabled provider ────────────────────────────────────────────────


def test_with_email_disabled_the_row_is_logged_but_nothing_is_sent(db):
    """`log_only` is the default until the OAuth credentials are in place.
    That is a configuration, not an error — but the log has to distinguish
    "logged" from "sent"."""
    result = _send()
    assert result.status == "disabled"
    row = db.tables["email_log"].rows[0]
    assert row.get("sent_at") is None
    assert row.get("error") is None


def test_no_recipient_fails_rather_than_silently_doing_nothing(db):
    result = _send(recipient="   ")
    assert result.status == "failed"
    assert db.tables["email_log"].rows == []


def test_an_alert_with_no_admin_address_configured_returns_none(db, monkeypatch):
    """Logged at ERROR by the implementation — a silent alerting path is worse
    than no alerting path."""
    monkeypatch.setattr(settings, "admin_alert_email", None)
    assert email.alert_admin(subject="x", body="y") is None
