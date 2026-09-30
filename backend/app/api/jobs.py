"""
Scheduled job endpoints (prompt §11).

Render cron hits these with the shared key in `X-Agent-Key`. They are never
open: each one either moves money-adjacent data or sends mail, and an
unauthenticated poll endpoint is a way to make the app download arbitrary
Drive files on demand.

Every job honors the §12 kill switch (`JOBS_PAUSED`) and reports what it did
rather than returning a bare 200 — a cron job whose only signal is its exit
code is a cron job nobody notices has stopped working.

"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from ..core import email_jobs, intake
from ..core.auth import Actor, require_job_key
from ..core.config import settings
from ..schemas.invoices import EmailJobResultOut, PollResultOut

log = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("/poll-drive", response_model=PollResultOut)
def poll_drive(
    limit: int = Query(
        default=50,
        le=200,
        description=(
            "Cap on new invoices ingested this run. Each one costs a Claude "
            "call, so a first run over a full backlog is bounded rather than "
            "unbounded."
        ),
    ),
    actor: Actor = Depends(require_job_key()),
):
    """Scan the Drive intake folders and process anything new (§7.1).

    Idempotent: keyed on the Drive file id, so the 15-minute cadence re-reads
    the same folder without creating duplicates. Never moves or deletes a
    Drive original — that happens after a verified BuilderTrend save, in
    Phase 3.
    """
    if settings.jobs_paused:
        # 200 with the reason, not an error: a paused job is a deliberate
        # state, and a cron alert firing every 15 minutes while someone has
        # intentionally paused intake trains people to ignore the alerts.
        return PollResultOut(
            errors=["JOBS_PAUSED is set; Drive polling is off."]
        )

    if not settings.drive_enabled:
        raise HTTPException(
            status_code=503,
            detail=(
                "Drive is not configured. Set GOOGLE_DRIVE_CREDENTIALS_JSON "
                "and DRIVE_FOLDER_INVOICE_UPLOADS."
            ),
        )

    result = intake.poll_drive(actor=actor, limit=limit)
    return PollResultOut(**result.as_dict())


@router.post("/email-daily", response_model=EmailJobResultOut)
def email_daily(
    force: bool = Query(
        default=False,
        description=(
            "Bypass the weekday and once-per-day guards. For testing the "
            "template and for re-sending after a fix, not for the cron."
        ),
    ),
    actor: Actor = Depends(require_job_key()),
):
    """§9.1: the 3:30 PM Pacific "in your court" digest.

    One email per person who has at least one invoice in `assigned` (as
    reviewer) or `pending_approval` (as approver). Anyone with nothing pending
    gets nothing — a digest that is usually empty is a digest people stop
    opening.

    Idempotent per person per day through `email_log`, so a double cron fire
    does not double-mail. Failures alert the admin and are never silent.
    """
    result = email_jobs.send_daily_court(force=force)
    return EmailJobResultOut(**result.as_dict())


@router.post("/email-eod", response_model=EmailJobResultOut)
def email_eod(
    force: bool = Query(
        default=False,
        description="Bypass the weekday and once-per-day guards.",
    ),
    actor: Actor = Depends(require_job_key()),
):
    """§9.2: the 5:30 PM Pacific end-of-day summary to the distribution list.

    Sends only when at least one invoice was approved that day, grouped by
    project, with the counts footer. A quiet day sends nothing.
    """
    result = email_jobs.send_eod_summary(force=force)
    return EmailJobResultOut(**result.as_dict())
