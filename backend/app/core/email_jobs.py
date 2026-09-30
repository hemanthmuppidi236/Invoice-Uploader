"""
The two scheduled email jobs (prompt §9).

Deciding *who* gets mail lives here; deciding *what it looks like* lives in
`email_templates.py`; actually sending and logging lives in `email.py`. The
split matters because the recipient logic is the part with real rules in it —
"skip people with nothing pending", "only send the summary if something was
approved today" — and it should be testable without HTML or a mail server.

Both jobs honor the §12 kill switch and the §14 weekdays-only default, and
both report what they did rather than returning a bare success. A cron job
whose only signal is an exit code is a cron job nobody notices has stopped.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from . import email, email_templates
from .config import settings
from .supabase_client import get_service_client

log = logging.getLogger(__name__)


@dataclass
class EmailJobResult:
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    recipients: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def record(self, result: email.SendResult) -> None:
        self.recipients.append(result.as_dict())
        if result.status == "sent":
            self.sent += 1
        elif result.status == "failed":
            self.failed += 1
        else:
            # `disabled` and `skipped_duplicate` are both "did not send, and
            # that is fine" — counted together so the numbers add up.
            self.skipped += 1

    def as_dict(self) -> dict:
        return {
            "sent": self.sent,
            "skipped": self.skipped,
            "failed": self.failed,
            "recipients": self.recipients,
            "notes": self.notes,
        }


def _sb():
    return get_service_client()


# ─── Schedule guards ──────────────────────────────────────────────────


def _local_today() -> date:
    """Today in the configured business timezone.

    The jobs run at 3:30 and 5:30 PM Pacific, which is the same UTC calendar
    day — but "approved today" has to mean the working day people actually
    experienced, not a UTC window, or invoices approved late in the afternoon
    would land in tomorrow's summary.
    """
    return datetime.now(ZoneInfo(settings.email_timezone)).date()


def _skip_reason() -> Optional[str]:
    """Why this run should not send, or None."""
    if settings.jobs_paused:
        return "JOBS_PAUSED is set; scheduled email is off."
    if settings.weekdays_only and _local_today().weekday() >= 5:
        return (
            f"{_local_today().strftime('%A')} is not a weekday and "
            "WEEKDAYS_ONLY is set."
        )
    return None


# ─── Shared loading ───────────────────────────────────────────────────


def _lookup_maps() -> dict:
    users = (
        _sb()
        .table("app_users")
        .select("id,name,email,role,deactivated_at")
        .execute()
        .data
        or []
    )
    projects = (
        _sb().table("projects").select("id,project_no,name").execute().data or []
    )
    vendors = (
        _sb().table("vendors").select("id,invoice_name").execute().data or []
    )
    codes = _sb().table("cost_codes").select("id,code").execute().data or []
    return {
        "users": {u["id"]: u for u in users},
        "projects": {p["id"]: p for p in projects},
        "vendors": {v["id"]: v for v in vendors},
        "codes": {c["id"]: c["code"] for c in codes},
    }


def _age_days(created_at) -> Optional[int]:
    if not created_at:
        return None
    try:
        started = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, (datetime.now(timezone.utc) - started).days)


def _latest_suggestions(invoice_ids: list[str]) -> dict[str, dict]:
    if not invoice_ids:
        return {}
    rows = (
        _sb()
        .table("invoice_suggestions")
        .select("invoice_id,cost_code_id,confidence,created_at")
        .in_("invoice_id", invoice_ids)
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )
    out: dict[str, dict] = {}
    for row in rows:
        out.setdefault(row["invoice_id"], row)
    return out


def _costs_by_invoice(invoice_ids: list[str]) -> dict[str, list[dict]]:
    if not invoice_ids:
        return {}
    rows = (
        _sb()
        .table("invoice_costs")
        .select("invoice_id,cost_code_id,amount,sort_order")
        .in_("invoice_id", invoice_ids)
        .order("sort_order")
        .execute()
        .data
        or []
    )
    out: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        out[row["invoice_id"]].append(row)
    return dict(out)


def _decorate(rows: list[dict], maps: dict, suggestions: dict) -> list[dict]:
    """Flatten an invoice row into what the templates expect."""
    out = []
    for row in rows:
        project = maps["projects"].get(row.get("project_id") or "")
        vendor = maps["vendors"].get(row.get("vendor_id") or "")
        suggestion = suggestions.get(row["id"]) or {}
        out.append(
            {
                "id": row["id"],
                "invoice_no": row.get("invoice_no"),
                "amount": row.get("amount"),
                "vendor_name": vendor["invoice_name"] if vendor else None,
                "project_name": project["name"] if project else None,
                "project_no": project["project_no"] if project else None,
                "suggested_code": maps["codes"].get(
                    suggestion.get("cost_code_id") or ""
                ),
                "suggested_confidence": (
                    float(suggestion["confidence"])
                    if suggestion.get("confidence") is not None
                    else None
                ),
                "age_days": _age_days(row.get("created_at")),
            }
        )
    return out


# ─── §9.1 Daily "in your court" ───────────────────────────────────────


def send_daily_court(*, force: bool = False) -> EmailJobResult:
    """One email per person with pending work. Skips anyone with none.

    §9.1: reviewers get their `assigned` invoices, approvers get their
    `pending_approval` ones. Someone who is both — Linda — gets two emails
    rather than one merged list, because the two piles need different actions
    and one table would bury which is which.
    """
    result = EmailJobResult()

    reason = _skip_reason()
    if reason and not force:
        result.notes.append(reason)
        return result

    maps = _lookup_maps()

    pending = (
        _sb()
        .table("invoices")
        .select(
            "id,invoice_no,amount,project_id,vendor_id,reviewer_id,"
            "approver_id,status,created_at"
        )
        .in_("status", ["assigned", "pending_approval"])
        .order("created_at")
        .execute()
        .data
        or []
    )

    if not pending:
        result.notes.append("Nothing is assigned or awaiting approval.")
        return result

    suggestions = _latest_suggestions([p["id"] for p in pending])

    # Bucket by (person, role). Someone in both roles gets two buckets.
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for invoice in pending:
        if invoice["status"] == "assigned" and invoice.get("reviewer_id"):
            buckets[(invoice["reviewer_id"], "reviewer")].append(invoice)
        elif invoice["status"] == "pending_approval" and invoice.get("approver_id"):
            buckets[(invoice["approver_id"], "approver")].append(invoice)

    orphaned = [
        i["id"]
        for i in pending
        if (i["status"] == "assigned" and not i.get("reviewer_id"))
        or (i["status"] == "pending_approval" and not i.get("approver_id"))
    ]
    if orphaned:
        # An invoice in a waiting state with nobody to wait on appears in no
        # digest at all, so it would sit indefinitely with no signal. Say so.
        note = (
            f"{len(orphaned)} invoice(s) are waiting but have nobody assigned, "
            "so nobody was emailed about them."
        )
        result.notes.append(note)
        log.warning("%s ids=%s", note, orphaned[:10])
        email.alert_admin(
            subject="Invoices waiting with nobody assigned",
            body=(
                f"{len(orphaned)} invoice(s) are in a waiting state with no "
                "reviewer or approver set, so they were left out of today's "
                "digest and nobody has been told about them. Ids: "
                + ", ".join(orphaned[:20])
            ),
        )

    for (user_id, role), invoices in sorted(buckets.items()):
        user = maps["users"].get(user_id)
        if not user:
            result.notes.append(f"Unknown user {user_id}; skipped.")
            continue
        if user.get("deactivated_at"):
            result.notes.append(
                f"{user.get('email')} is deactivated; skipped "
                f"{len(invoices)} invoice(s). Reassign them."
            )
            continue
        if not user.get("email"):
            result.notes.append(f"User {user_id} has no email; skipped.")
            continue

        decorated = _decorate(invoices, maps, suggestions)
        name = (user.get("name") or user["email"].split("@")[0]).split(" ")[0]

        outcome = email.send(
            kind=email.KIND_DAILY_COURT,
            recipient=user["email"],
            subject=email_templates.daily_court_subject(
                count=len(decorated), role=role
            ),
            body_html=email_templates.daily_court_html(
                name=name, role=role, invoices=decorated
            ),
            invoice_ids=[i["id"] for i in decorated],
            force=force,
        )
        result.record(outcome)

    if result.failed:
        email.alert_admin(
            subject="Daily digest had send failures",
            body=(
                f"{result.failed} of {result.failed + result.sent} daily "
                "digest emails failed to send. Check the Render logs and the "
                "email_log table."
            ),
        )

    return result


# ─── §9.2 End-of-day summary ──────────────────────────────────────────


def send_eod_summary(*, force: bool = False) -> EmailJobResult:
    """The distribution list summary. Only sends if something was approved today.

    §9.2 is explicit that a quiet day sends nothing. A daily email that is
    usually empty gets filtered by its recipients, and then the one that
    matters gets filtered along with it.
    """
    result = EmailJobResult()

    reason = _skip_reason()
    if reason and not force:
        result.notes.append(reason)
        return result

    tz = ZoneInfo(settings.email_timezone)
    day_start = datetime.combine(_local_today(), datetime.min.time(), tzinfo=tz)
    day_end = day_start + timedelta(days=1)

    approved = (
        _sb()
        .table("invoices")
        .select(
            "id,invoice_no,amount,project_id,vendor_id,reviewer_id,"
            "approver_id,approved_at"
        )
        .gte("approved_at", day_start.astimezone(timezone.utc).isoformat())
        .lt("approved_at", day_end.astimezone(timezone.utc).isoformat())
        .order("approved_at")
        .execute()
        .data
        or []
    )

    if not approved:
        result.notes.append(
            "Nothing was approved today, so no summary was sent (§9.2)."
        )
        return result

    recipients = (
        _sb()
        .table("distribution_list")
        .select("email,name")
        .eq("active", True)
        .execute()
        .data
        or []
    )
    if not recipients:
        note = (
            "Invoices were approved today but the distribution list is empty, "
            "so the summary had nowhere to go."
        )
        result.notes.append(note)
        log.warning(note)
        email.alert_admin(subject="Distribution list is empty", body=note)
        return result

    maps = _lookup_maps()
    costs = _costs_by_invoice([a["id"] for a in approved])

    grouped: dict[str, list[dict]] = defaultdict(list)
    for invoice in approved:
        grouped[invoice.get("project_id") or ""].append(
            {
                "id": invoice["id"],
                "invoice_no": invoice.get("invoice_no"),
                "amount": invoice.get("amount"),
                "vendor_name": (
                    maps["vendors"].get(invoice.get("vendor_id") or "") or {}
                ).get("invoice_name"),
                "cost_codes": [
                    maps["codes"].get(c["cost_code_id"], "unknown code")
                    for c in costs.get(invoice["id"], [])
                ],
                "reviewer_name": _person(maps, invoice.get("reviewer_id")),
                "approver_name": _person(maps, invoice.get("approver_id")),
            }
        )

    groups = []
    for project_id, invoices in grouped.items():
        project = maps["projects"].get(project_id)
        groups.append(
            {
                "project_name": project["name"] if project else "No project",
                "project_no": project["project_no"] if project else None,
                "invoices": invoices,
            }
        )
    groups.sort(key=lambda g: g["project_name"] or "")

    counts = _eod_counts(day_start, day_end, approved_count=len(approved))
    subject = email_templates.eod_subject(approved_count=len(approved))
    html = email_templates.eod_html(groups=groups, counts=counts)

    for entry in recipients:
        address = (entry.get("email") or "").strip()
        if not address:
            continue
        outcome = email.send(
            kind=email.KIND_EOD_SUMMARY,
            recipient=address,
            subject=subject,
            body_html=html,
            invoice_ids=[a["id"] for a in approved],
            force=force,
        )
        result.record(outcome)

    if result.failed:
        email.alert_admin(
            subject="End-of-day summary had send failures",
            body=(
                f"{result.failed} end-of-day summary emails failed. Check the "
                "Render logs and the email_log table."
            ),
        )

    return result


def _person(maps: dict, user_id: Optional[str]) -> Optional[str]:
    user = maps["users"].get(user_id or "")
    if not user:
        return None
    return user.get("name") or user.get("email")


def _eod_counts(day_start, day_end, *, approved_count: int) -> dict:
    """§9.2 footer: approved today, uploaded today, still pending, flagged."""
    start_utc = day_start.astimezone(timezone.utc).isoformat()
    end_utc = day_end.astimezone(timezone.utc).isoformat()

    uploaded = (
        _sb()
        .table("invoices")
        .select("id")
        .gte("uploaded_at", start_utc)
        .lt("uploaded_at", end_utc)
        .execute()
        .data
        or []
    )
    pending = (
        _sb()
        .table("invoices")
        .select("id")
        .in_("status", ["ingested", "suggested", "assigned", "pending_approval"])
        .execute()
        .data
        or []
    )
    flagged = (
        _sb().table("invoices").select("id").eq("status", "flagged").execute().data
        or []
    )
    return {
        "approved_today": approved_count,
        "uploaded_today": len(uploaded),
        "still_pending": len(pending),
        "flagged": len(flagged),
    }
