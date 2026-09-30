"""
Outbound email via the Gmail API (prompt §9).

Why Gmail's HTTP API and not SMTP: Render blocks outbound SMTP on every plan,
Ferrocrete already has Google Workspace, and single-user OAuth means one
person clicks Allow once and the refresh token keeps working. Same setup as
the pay app.

Every send is recorded in `email_log`. §9 requires that, and the log is also
what makes the scheduled jobs idempotent:

    1. INSERT the log row first, which CLAIMS the slot. A unique index on
       (kind, recipient, created_at::date) WHERE error IS NULL means a second
       attempt for the same person, same kind, same day cannot insert — so a
       double cron fire skips instead of double-mailing.
    2. Send.
    3. On success, stamp sent_at. The slot stays held.
    4. On failure, write the error — which drops the row out of the partial
       index and FREES the slot, so a retry can legitimately try again.

That ordering is the whole design. Claiming after sending would leave a race
where two runs both send; claiming without releasing on failure would make a
transient Gmail error permanent for the rest of the day.

Failures alert the admin and are never silent (§9). The alert itself goes
through the same claim mechanism under kind `admin_alert`, so a job failing
repeatedly produces one alert per day, not one per attempt.
"""

from __future__ import annotations

import base64
import logging
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from typing import Optional

from .config import settings
from .supabase_client import get_service_client

log = logging.getLogger("ferrocrete.email")

# Minimum scope: send only. This app never reads or modifies a mailbox.
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

# email_log.kind values, matching the CHECK constraint in migration 001.
KIND_DAILY_COURT = "daily_court"
KIND_EOD_SUMMARY = "eod_summary"
KIND_ADMIN_ALERT = "admin_alert"


class SendResult:
    """What happened to one send attempt."""

    def __init__(
        self,
        status: str,
        recipient: str,
        log_id: Optional[str] = None,
        error: Optional[str] = None,
    ):
        self.status = status  # sent | skipped_duplicate | failed | disabled
        self.recipient = recipient
        self.log_id = log_id
        self.error = error

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "recipient": self.recipient,
            "error": self.error,
        }

    def __repr__(self) -> str:
        return f"<SendResult {self.status} {self.recipient}>"


def _sb():
    return get_service_client()


# ─── Public API ───────────────────────────────────────────────────────


def send(
    *,
    kind: str,
    recipient: str,
    subject: str,
    body_html: str,
    body_text: Optional[str] = None,
    invoice_ids: Optional[list[str]] = None,
    force: bool = False,
) -> SendResult:
    """Send one email, once per recipient per kind per day.

    `force` bypasses the duplicate check for a manual re-send. It still writes
    a log row, so a forced re-send is visible rather than invisible — it just
    does not claim the daily slot.
    """
    recipient = (recipient or "").strip()
    if not recipient:
        return SendResult("failed", "", error="No recipient.")

    if not force and _already_sent_today(kind, recipient):
        log.info("skipping %s to %s — already sent today", kind, recipient)
        return SendResult("skipped_duplicate", recipient)

    log_id = _claim(
        kind=kind,
        recipient=recipient,
        subject=subject,
        invoice_ids=invoice_ids or [],
    )
    if log_id is None and not force:
        # The insert was rejected by the unique index, which means another run
        # claimed this slot between the check above and here. That is exactly
        # the race the index exists to lose safely.
        log.info("lost the claim race for %s to %s — skipping", kind, recipient)
        return SendResult("skipped_duplicate", recipient)

    if not settings.email_enabled:
        # Not an error: `log_only` is a legitimate configuration, and it is
        # the default until the OAuth credentials are in place. The row stays
        # claimed with no sent_at, so the log distinguishes "logged but not
        # sent" from "sent".
        log.info(
            "email disabled (provider=%s) — logged only: %s to %s",
            settings.email_provider,
            subject,
            recipient,
        )
        return SendResult("disabled", recipient, log_id=log_id)

    try:
        message = _build_message(
            to=recipient,
            subject=subject,
            body_html=body_html,
            body_text=body_text,
        )
        _send_via_gmail(message)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log.error("Gmail send failed (%s to %s): %s", kind, recipient, error)
        _release(log_id, error)
        return SendResult("failed", recipient, log_id=log_id, error=error)

    if log_id:
        _sb().table("email_log").update(
            {"sent_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", log_id).execute()

    log.info("sent %s to %s", kind, recipient)
    return SendResult("sent", recipient, log_id=log_id)


def alert_admin(*, subject: str, body: str) -> Optional[SendResult]:
    """Tell the admin something went wrong (§9: never fail silently).

    Returns None when no admin address is configured, which is itself logged
    at ERROR — a silent alerting path is worse than no alerting path.
    """
    to = settings.admin_alert_email
    if not to:
        log.error(
            "ADMIN_ALERT_EMAIL is not set, so this alert has nowhere to go: %s",
            subject,
        )
        return None

    return send(
        kind=KIND_ADMIN_ALERT,
        recipient=to,
        subject=f"[Invoice app] {subject}",
        body_html=(
            f"<p style='font-family:Georgia,serif;font-size:15px;"
            f"line-height:1.6;color:#2a2820'>{_escape(body)}</p>"
        ),
        body_text=body,
    )


# ─── email_log ────────────────────────────────────────────────────────


def _already_sent_today(kind: str, recipient: str) -> bool:
    """Has this person already had this kind of email today?

    Compared on the UTC date, matching the index expression. The jobs run in
    the afternoon Pacific, which is the same UTC day, so the window lines up
    with the working day in practice.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    existing = (
        _sb()
        .table("email_log")
        .select("id")
        .eq("kind", kind)
        .eq("recipient", recipient)
        .is_("error", "null")
        .gte("created_at", f"{today}T00:00:00+00:00")
        .limit(1)
        .execute()
    )
    return bool(existing.data)


def _claim(
    *, kind: str, recipient: str, subject: str, invoice_ids: list[str]
) -> Optional[str]:
    """Insert the log row, claiming today's slot. None if the slot was taken."""
    try:
        created = (
            _sb()
            .table("email_log")
            .insert(
                {
                    "kind": kind,
                    "recipient": recipient,
                    "subject": subject[:500],
                    "invoice_ids": invoice_ids,
                }
            )
            .execute()
        )
        return created.data[0]["id"] if created.data else None
    except Exception as e:
        # A unique violation here is the expected, healthy outcome of a double
        # fire. Anything else is a real problem and worth the ERROR.
        text = str(e).lower()
        if "duplicate" in text or "unique" in text or "23505" in text:
            return None
        log.error("could not write email_log for %s to %s: %s", kind, recipient, e)
        return None


def _release(log_id: Optional[str], error: str) -> None:
    """Record the failure, which frees the slot for a retry."""
    if not log_id:
        return
    try:
        _sb().table("email_log").update({"error": error[:2000]}).eq(
            "id", log_id
        ).execute()
    except Exception as e:
        log.error("could not record email failure on %s: %s", log_id, e)


# ─── MIME and transport ───────────────────────────────────────────────


def _build_message(
    *, to: str, subject: str, body_html: str, body_text: Optional[str]
) -> EmailMessage:
    message = EmailMessage()
    sender = settings.gmail_sender_email or settings.email_from
    message["From"] = formataddr(("Ferrocrete Builders", sender))
    message["To"] = to
    message["Subject"] = subject

    # Text first, HTML as the alternative. A message with no text part scores
    # higher with spam filters, and these go to the whole distribution list.
    message.set_content(body_text or html_to_text(body_html))
    message.add_alternative(body_html, subtype="html")
    return message


def _send_via_gmail(message: EmailMessage) -> None:
    """POST the message to Gmail's users.messages.send.

    Imports are function-local so this module still loads where the Google
    libraries are absent — the tests never reach this path.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=settings.gmail_oauth_refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.gmail_oauth_client_id,
        client_secret=settings.gmail_oauth_client_secret,
        scopes=GMAIL_SCOPES,
    )
    creds.refresh(Request())

    # cache_discovery=False: the on-disk cache breaks on Render's read-only
    # container filesystem.
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


# ─── Helpers ──────────────────────────────────────────────────────────


def html_to_text(html: str) -> str:
    """Rough HTML to text for the text/plain alternative.

    Not a parser. The real markup is the HTML part; this exists so a
    text-only client sees something readable rather than tag soup.
    """
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    text = re.sub(r"</(p|tr|div|h1|h2|h3)>", "\n", text, flags=re.I)
    text = re.sub(r"</t[dh]>", "  ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'")
    text = re.sub(r"[ \t]{2,}", "  ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
