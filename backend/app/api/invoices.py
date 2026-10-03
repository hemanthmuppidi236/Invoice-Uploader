"""
Invoices API — read, triage, and metadata correction (prompt §10, §11).

Phase 1 covers everything up to `suggested`, plus the flagged triage actions.
The workflow transitions that move an invoice toward BuilderTrend —
assign, mark-reviewed, approve, reject, mark-uploaded, mark-filed — arrive in
Phases 2 and 3 and are deliberately absent rather than stubbed: a route that
exists but does not enforce the §12 guardrails is worse than no route.

`PATCH /invoices/{id}` corrects what an invoice SAYS. It cannot change where
the invoice IS — `InvoiceUpdate` has no `status` field, so a smuggled one is
a 422.
"""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status as http,
)

from ..core import audit, filing, intake, state_machine, storage
from ..core.auth import (
    Actor,
    CurrentUser,
    agent_or_user,
    as_actor,
    get_current_user,
    require_agent_or_role,
    require_role,
)
from ..core.claude_client import MAX_PDF_BYTES
from ..core.config import settings
from ..core.invoice_rules import (
    bill_no_for,
    costs_balance,
    due_date_for,
    is_do_not_select,
    to_money,
)
from ..core.supabase_client import get_service_client
from ..schemas.invoices import (
    AssignIn,
    AuditEntryOut,
    BulkAssignIn,
    BulkAssignResult,
    FilingOut,
    FlagIn,
    MarkFiledIn,
    MarkReviewedIn,
    MarkUploadedIn,
    MarkUploadedOut,
    InvoiceCostOut,
    InvoiceDashboard,
    InvoiceDetail,
    InvoiceLineOut,
    InvoiceOut,
    InvoiceStats,
    InvoiceUpdate,
    NotDuplicateIn,
    ReassignIn,
    RejectIn,
    SignedUrlOut,
    SuggestionOut,
    UploadQueueCost,
    UploadQueueItem,
    UploadQueueOut,
    UploadResultOut,
    VoidIn,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/invoices", tags=["invoices"])

# Statuses that count as "still moving through the workflow".
OPEN_STATUSES = (
    "ingested",
    "suggested",
    "assigned",
    "pending_approval",
    "approved",
    "flagged",
)

# Prompt §10: auto-save on the detail form only while the record is `assigned`
# (the reviewer's turn) or `flagged` (Linda's turn). Correcting an approved or
# uploaded invoice in place would rewrite the basis of a decision already
# made, so a PATCH is refused there.
PATCHABLE_STATUSES = ("ingested", "suggested", "assigned", "flagged")


def _sb():
    return get_service_client()


# ─── Lookup tables ────────────────────────────────────────────────────


def _lookups() -> dict:
    """Name maps for the joined display fields, fetched once per request."""
    users = _sb().table("app_users").select("id,name,email").execute().data or []
    projects = (
        _sb().table("projects").select("id,project_no,name").execute().data or []
    )
    vendors = (
        _sb().table("vendors").select("id,invoice_name,bt_name").execute().data or []
    )
    codes = (
        _sb().table("cost_codes").select("id,code,base_code").execute().data or []
    )
    return {
        "users": {u["id"]: (u.get("name") or u.get("email") or "") for u in users},
        "projects": {p["id"]: p for p in projects},
        "vendors": {v["id"]: v for v in vendors},
        "codes": {c["id"]: c for c in codes},
    }


def _age_days(row: dict) -> Optional[int]:
    """How long this invoice has been waiting, for the §10 Age column."""
    created = row.get("created_at")
    if not created:
        return None
    try:
        started = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, (datetime.now(timezone.utc) - started).days)


def _to_out(
    row: dict,
    lookups: dict,
    *,
    costs: Optional[list[dict]] = None,
    suggestion: Optional[dict] = None,
) -> InvoiceOut:
    project = lookups["projects"].get(row.get("project_id") or "")
    vendor = lookups["vendors"].get(row.get("vendor_id") or "")

    amount = row.get("amount")
    amount_dec = Decimal(str(amount)) if amount is not None else None
    cost_amounts = [
        Decimal(str(c["amount"])) for c in (costs or []) if c.get("amount") is not None
    ]
    balanced, diff = costs_balance(amount_dec, cost_amounts)

    suggested_code = None
    if suggestion and suggestion.get("cost_code_id"):
        code_row = lookups["codes"].get(suggestion["cost_code_id"])
        suggested_code = code_row["code"] if code_row else None

    return InvoiceOut(
        **{k: row.get(k) for k in InvoiceOut.model_fields if k in row},
        project_no=project["project_no"] if project else None,
        project_name=project["name"] if project else None,
        vendor_name=vendor["invoice_name"] if vendor else None,
        vendor_bt_name=vendor["bt_name"] if vendor else None,
        reviewer_name=lookups["users"].get(row.get("reviewer_id") or ""),
        approver_name=lookups["users"].get(row.get("approver_id") or ""),
        suggested_code=suggested_code,
        suggested_confidence=(suggestion or {}).get("confidence"),
        costs_balanced=balanced,
        costs_difference=diff if not balanced else Decimal("0.00"),
        age_days=_age_days(row),
    )


def _fetch_invoice(invoice_id: str) -> dict:
    res = (
        _sb().table("invoices").select("*").eq("id", invoice_id).limit(1).execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return res.data[0]


def _latest_suggestions(invoice_ids: list[str]) -> dict[str, dict]:
    """Newest suggestion per invoice.

    Suggestions are append-only (a retry adds a row), so the list screen needs
    the most recent one per invoice rather than the first row returned.
    """
    if not invoice_ids:
        return {}
    rows = (
        _sb()
        .table("invoice_suggestions")
        .select("*")
        .in_("invoice_id", invoice_ids)
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(r["invoice_id"], r)
    return out


def _costs_by_invoice(invoice_ids: list[str]) -> dict[str, list[dict]]:
    if not invoice_ids:
        return {}
    rows = (
        _sb()
        .table("invoice_costs")
        .select("*")
        .in_("invoice_id", invoice_ids)
        .order("sort_order")
        .execute()
        .data
        or []
    )
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["invoice_id"], []).append(r)
    return out


# ─── List and dashboard ───────────────────────────────────────────────


@router.get("", response_model=InvoiceDashboard)
def list_invoices(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    project_id: Optional[str] = None,
    vendor_id: Optional[str] = None,
    reviewer_id: Optional[str] = None,
    approver_id: Optional[str] = None,
    flag_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    limit: int = Query(default=500, le=2000),
    user: CurrentUser = Depends(get_current_user),
):
    """The §10 landing screen: a filtered list, the stat cards, and the
    role-aware "Your court" queue.

    Reads only require authentication — a viewer sees everything and writes
    nothing, same posture as the pay app.
    """
    q = _sb().table("invoices").select("*")
    if status_filter:
        if status_filter == "open":
            q = q.in_("status", list(OPEN_STATUSES))
        else:
            q = q.eq("status", status_filter)
    if project_id:
        q = q.eq("project_id", project_id)
    if vendor_id:
        q = q.eq("vendor_id", vendor_id)
    if reviewer_id:
        q = q.eq("reviewer_id", reviewer_id)
    if approver_id:
        q = q.eq("approver_id", approver_id)
    if flag_code:
        q = q.eq("flag_code", flag_code)
    if date_from:
        q = q.gte("invoice_date", str(date_from))
    if date_to:
        q = q.lte("invoice_date", str(date_to))

    rows = (
        q.order("created_at", desc=True).limit(limit).execute().data or []
    )

    lookups = _lookups()
    ids = [r["id"] for r in rows]
    suggestions = _latest_suggestions(ids)
    costs = _costs_by_invoice(ids)

    invoices = [
        _to_out(
            r,
            lookups,
            costs=costs.get(r["id"], []),
            suggestion=suggestions.get(r["id"]),
        )
        for r in rows
    ]

    # Stats are company-wide, not filtered: the cards are a standing picture
    # of the workload, and having them move with the filters would make
    # "flagged: 0" mean two different things.
    stats = _compute_stats()
    court, court_label = _your_court(user, lookups)

    return InvoiceDashboard(
        invoices=invoices,
        stats=stats,
        your_court=court,
        your_court_label=court_label,
    )


def _compute_stats() -> InvoiceStats:
    rows = (
        _sb()
        .table("invoices")
        .select("status,flag_code,reviewer_id")
        .in_("status", list(OPEN_STATUSES))
        .execute()
        .data
        or []
    )
    stats = InvoiceStats()
    for r in rows:
        s = r["status"]
        if s in ("ingested", "suggested"):
            # §10 "unassigned": read, suggested, but nobody has been put on it.
            stats.unassigned += 1
        elif s == "assigned":
            stats.in_review += 1
        elif s == "pending_approval":
            stats.pending_approval += 1
        elif s == "approved":
            stats.approved_not_uploaded += 1
        elif s == "flagged":
            stats.flagged += 1
            code = r.get("flag_code") or "other"
            stats.flagged_by_reason[code] = stats.flagged_by_reason.get(code, 0) + 1
    stats.total_open = len(rows)
    return stats


def _your_court(
    user: CurrentUser, lookups: dict
) -> tuple[list[InvoiceOut], Optional[str]]:
    """The §10 role-aware queue.

    Reviewers see what is assigned to them, approvers what awaits their
    approval, and Linda the flagged and unassigned pile. Someone holding
    several roles sees the queue of the role with the most urgent work, and
    the label says which — an unlabelled mixed list is how people miss things.
    """
    if user.is_approver:
        rows = (
            _sb()
            .table("invoices")
            .select("*")
            .eq("status", "pending_approval")
            .eq("approver_id", user.id)
            .order("created_at")
            .execute()
            .data
            or []
        )
        if rows:
            return _decorate(rows, lookups), "Awaiting your approval"

    if user.is_pe or user.is_accountant:
        rows = (
            _sb()
            .table("invoices")
            .select("*")
            .eq("status", "assigned")
            .eq("reviewer_id", user.id)
            .order("created_at")
            .execute()
            .data
            or []
        )
        if rows:
            return _decorate(rows, lookups), "Assigned to you for review"

    if user.is_accountant or user.is_admin:
        rows = (
            _sb()
            .table("invoices")
            .select("*")
            .in_("status", ["flagged", "suggested", "ingested"])
            .order("created_at")
            .execute()
            .data
            or []
        )
        if rows:
            return _decorate(rows, lookups), "Flagged and unassigned"

    return [], None


def _decorate(rows: list[dict], lookups: dict) -> list[InvoiceOut]:
    ids = [r["id"] for r in rows]
    suggestions = _latest_suggestions(ids)
    costs = _costs_by_invoice(ids)
    return [
        _to_out(
            r,
            lookups,
            costs=costs.get(r["id"], []),
            suggestion=suggestions.get(r["id"]),
        )
        for r in rows
    ]


# ─── Manual upload (prompt §7.1, the non-Drive path) ──────────────────


@router.post(
    "/upload", response_model=UploadResultOut, status_code=http.HTTP_201_CREATED
)
async def upload_invoice(
    file: UploadFile = File(...),
    note: Optional[str] = Form(default=None),
    actor: Actor = Depends(as_actor),
    user: CurrentUser = Depends(require_role("admin", "accountant")),
):
    """Add an invoice by handing the app its PDF.

    The Drive poll is the normal way in and stays the normal way in. This is
    for the invoice that arrived as an email attachment, the re-scan of
    something that flagged as unreadable, and the first invoice on a
    deployment that has no Drive credentials yet.

    Accounting only. A project engineer reviews what they are given; letting
    anyone add a payable would put a second, unaudited door into the ledger.

    Everything after the PDF lands in Storage is the Drive pipeline exactly —
    same extraction, same flag reasons, same states. Re-uploading the same
    file returns the invoice that already exists rather than creating a
    second payable, keyed on a hash of the bytes, because for an upload the
    bytes are the only identity there is.
    """
    filename = file.filename or "invoice.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=422,
            detail="Invoices have to be PDFs. Scan or print to PDF first.",
        )

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=422, detail="That file is empty.")

    # Checked before anything is stored, so an oversized PDF cannot leave an
    # orphan in the bucket and an invoice row the AI can never read.
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"That PDF is {len(pdf_bytes) / 1_048_576:.1f} MB; the limit "
                f"is {MAX_PDF_BYTES / 1_048_576:.0f} MB, because the Claude "
                "API caps a request at 32 MB and base64 inflates it by a "
                "third. Downsample the scan or split it."
            ),
        )

    # Trust the bytes, not the extension. A .pdf that is really a JPEG costs
    # a Claude call and comes back as an unreadable flag with no useful
    # reason attached.
    if not pdf_bytes.startswith(b"%PDF"):
        raise HTTPException(
            status_code=422,
            detail=(
                "That file is named .pdf but is not a PDF — it has no PDF "
                "header. Re-export it from whatever produced it."
            ),
        )

    try:
        outcome = intake.ingest_upload(
            filename=filename, pdf_bytes=pdf_bytes, actor=actor, note=note
        )
    except Exception as e:
        log.exception("manual invoice upload failed")
        raise HTTPException(
            status_code=502, detail=f"Could not ingest the PDF: {e}"
        )

    detail = _detail(outcome["invoice_id"])
    return UploadResultOut(
        invoice_id=outcome["invoice_id"],
        status=detail.status,
        created=outcome["outcome"] != "existing",
        detail=outcome.get("detail") or None,
        invoice=detail,
    )


# ─── Upload queue (prompt §7.6 step 1, §11) ───────────────────────────
#
# Registered BEFORE `GET /{invoice_id}`. FastAPI matches routes in
# declaration order, so a later registration would resolve this path as
# `invoice_id="upload-queue"` and 404 on a lookup for an invoice that does
# not exist.

# SOP §4.2: "Check for duplicates if the invoice is more than a few weeks
# old." Three weeks is the line, because that is when a bill is old enough to
# plausibly have been entered by hand already.
DUPLICATE_CHECK_AGE_DAYS = 21

# Signed PDF URLs in the queue outlive a typical batch. SOP §8.1 budgets a
# draft-save-reload cycle per bill, so twenty invoices can run past an hour;
# a URL that expires mid-batch looks to the session like a missing PDF. The
# session can always re-fetch one invoice's URL from /pdf-url.
QUEUE_PDF_URL_TTL = 4 * 3600


@router.get("/upload-queue", response_model=UploadQueueOut)
def upload_queue(
    limit: int = Query(default=100, le=500),
    actor: Actor = Depends(require_agent_or_role("admin", "accountant")),
):
    """Everything the Chrome session needs to type, computed server-side.

    §7.6 step 1 describes this as `GET /invoices?status=approved`. It is a
    separate endpoint because the list payload is shaped for a table and the
    session needs something different: the BuilderTrend field values rather
    than the app's own record. Every value the session would otherwise derive
    for itself — the base code for the Title, the last four digits for the
    Bill #, the end-of-next-month due date, the BuilderTrend vendor name, the
    bill URL — is derived here instead, where it is tested.

    Two lists come back. `queue` is safe to work. `blocked` is approved but
    missing something BuilderTrend needs, and each item says what; the
    session must flag those rather than improvise. They are separate lists so
    a blocker cannot be read as advisory.
    """
    rows = (
        _sb()
        .table("invoices")
        .select("*")
        .eq("status", "approved")
        .order("approved_at")
        .limit(limit)
        .execute()
        .data
        or []
    )

    # Oldest approved first, same reasoning as the intake backlog: with a cap
    # in place the order decides who waits, and the longest-approved invoice
    # is the most overdue vendor.
    lookups = _lookups()
    ids = [r["id"] for r in rows]
    costs = _costs_by_invoice(ids)

    projects = _full_projects([r.get("project_id") for r in rows])
    vendors = _full_vendors([r.get("vendor_id") for r in rows])
    code_rows = _full_cost_codes()

    queue: list[UploadQueueItem] = []
    blocked: list[UploadQueueItem] = []
    for row in rows:
        item = _to_queue_item(
            row,
            project=projects.get(row.get("project_id") or ""),
            vendor=vendors.get(row.get("vendor_id") or ""),
            cost_rows=costs.get(row["id"], []),
            codes=code_rows,
        )
        (blocked if item.blockers else queue).append(item)

    awaiting = (
        _sb()
        .table("invoices")
        .select("*")
        .eq("status", "uploaded")
        .order("uploaded_at")
        .limit(200)
        .execute()
        .data
        or []
    )
    recent = (
        _sb()
        .table("invoices")
        .select("*")
        .in_("status", ["uploaded", "filed"])
        .order("uploaded_at", desc=True)
        .limit(50)
        .execute()
        .data
        or []
    )

    notes: list[str] = []
    if blocked:
        notes.append(
            f"{len(blocked)} approved invoice(s) are missing something "
            "BuilderTrend needs. Flag them; do not improvise a value."
        )
    if awaiting:
        notes.append(
            f"{len(awaiting)} invoice(s) are in BuilderTrend but not yet "
            "filed to Drive. Retry filing from /uploads."
        )
    if not settings.drive_enabled or not settings.drive_folder_bt_invoices:
        notes.append(
            "Drive filing is not configured, so mark-uploaded will not file "
            "the copy. The bills still save; the filing step has to be done "
            "by hand until Drive is set up."
        )

    return UploadQueueOut(
        generated_at=datetime.now(timezone.utc),
        count=len(queue),
        queue=queue,
        blocked=blocked,
        awaiting_filing=_decorate(awaiting, lookups),
        recently_uploaded=_decorate(recent, lookups),
        filing_by_backend=True,
        notes=notes,
    )


def _full_projects(project_ids: list[Optional[str]]) -> dict[str, dict]:
    """Projects with the columns only the upload path needs."""
    wanted = [p for p in dict.fromkeys(project_ids) if p]
    if not wanted:
        return {}
    rows = (
        _sb()
        .table("projects")
        .select(
            "id,project_no,name,bt_job_id,drive_folder_name,quirks,notes,status"
        )
        .in_("id", wanted)
        .execute()
        .data
        or []
    )
    return {r["id"]: r for r in rows}


def _full_vendors(vendor_ids: list[Optional[str]]) -> dict[str, dict]:
    wanted = [v for v in dict.fromkeys(vendor_ids) if v]
    if not wanted:
        return {}
    rows = (
        _sb()
        .table("vendors")
        .select("id,invoice_name,bt_name,drive_folder_name,notes,active")
        .in_("id", wanted)
        .execute()
        .data
        or []
    )
    return {r["id"]: r for r in rows}


def _full_cost_codes() -> dict[str, dict]:
    rows = (
        _sb()
        .table("cost_codes")
        .select("id,code,base_code,description,active")
        .execute()
        .data
        or []
    )
    return {r["id"]: r for r in rows}


def _bill_title(cost_items: list[UploadQueueCost]) -> tuple[Optional[str], list[str]]:
    """The one base code that goes in the Bill Title (SOP §4).

    BuilderTrend's Title field takes a single base code, but an invoice can
    legitimately split across codes with different bases — 3002 for walls and
    3008 for a slab on the same ready-mix bill. The form cannot express that,
    so the largest share wins and the caller warns about the rest. Silently
    picking the first row would put a plausible wrong code on the bill, which
    nobody would notice.

    Ties break on the lower code, so the answer does not depend on row order.
    """
    totals: dict[str, Decimal] = {}
    for c in cost_items:
        if not c.base_code:
            continue
        totals[c.base_code] = totals.get(c.base_code, Decimal("0")) + abs(c.amount)
    if not totals:
        return None, []
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[0][0], [code for code, _ in ranked]


def _to_queue_item(
    row: dict,
    *,
    project: Optional[dict],
    vendor: Optional[dict],
    cost_rows: list[dict],
    codes: dict[str, dict],
) -> UploadQueueItem:
    warnings: list[str] = []
    blockers: list[str] = []

    cost_items: list[UploadQueueCost] = []
    for c in cost_rows:
        code_row = codes.get(c.get("cost_code_id") or "") or {}
        if not code_row:
            blockers.append(
                "A cost row points at a cost code that no longer exists. "
                "Accounting has to re-code this invoice."
            )
            continue
        if not code_row.get("active"):
            warnings.append(
                f"Cost code {code_row.get('code')} is marked inactive in the "
                "app. Confirm it is still selectable in BuilderTrend."
            )
        cost_items.append(
            UploadQueueCost(
                cost_code=code_row.get("code") or "",
                base_code=code_row.get("base_code"),
                name=code_row.get("description"),
                amount=to_money(c.get("amount")) or Decimal("0.00"),
            )
        )

    bill_title, base_codes = _bill_title(cost_items)
    if len(base_codes) > 1:
        warnings.append(
            "The cost split crosses base codes "
            + _join_codes(base_codes)
            + f". BuilderTrend's Bill Title takes one, so it is set to "
            f"{bill_title} (the largest share). The Costs rows still carry "
            "every sub-code. Check with Linda if the Title should differ."
        )

    # ── Blockers: anything BuilderTrend cannot be given a value for ──
    if not project:
        blockers.append("No project on this invoice, so there is no job to bill to.")
    elif project.get("status") == "active":
        # §4.1: a current job is handled by a different workflow entirely.
        blockers.append(
            f"{project.get('name')} is a current job, which this workflow "
            "does not cover (§4.1). It should never have reached approved."
        )
    if not vendor:
        blockers.append("No vendor on this invoice, so Pay To cannot be filled.")
    else:
        if not vendor.get("bt_name"):
            blockers.append(
                "This vendor has no BuilderTrend name recorded. Add the "
                "mapping in /admin (SOP §6)."
            )
        elif is_do_not_select(vendor.get("bt_name")):
            # SOP §6: "Never pick a vendor labeled ** DO NOT SELECT **."
            blockers.append(
                f"The recorded BuilderTrend vendor is {vendor['bt_name']!r}, "
                "which BuilderTrend marks DO NOT SELECT. Fix the mapping "
                "before uploading."
            )
        if vendor.get("active") is False:
            warnings.append(
                f"{vendor.get('invoice_name')} is marked inactive in the app."
            )

    if not cost_items:
        blockers.append("No cost rows, so the Costs grid cannot be filled.")
    if not bill_title:
        blockers.append(
            "None of the cost codes has a base code, so the Bill Title has "
            "no value (SOP §4)."
        )

    amount = to_money(row.get("amount"))
    if amount is None:
        blockers.append("No invoice total.")
    else:
        split_total = sum((c.amount for c in cost_items), Decimal("0.00"))
        if cost_items and split_total != amount:
            blockers.append(
                f"The cost rows sum to ${split_total:,.2f} but the invoice "
                f"total is ${amount:,.2f}. Do not enter this."
            )
        if amount == 0:
            warnings.append(
                "The invoice total is $0.00. BuilderTrend will take it, but "
                "confirm it is not a misread before saving."
            )

    invoice_date = row.get("invoice_date")
    if not invoice_date:
        blockers.append("No invoice date.")
    bill_no = row.get("bill_no") or bill_no_for(row.get("invoice_no"))
    if not bill_no:
        blockers.append(
            "No Bill # could be derived — the invoice number has no digits "
            "in it (SOP §4)."
        )

    due_date = row.get("due_date")
    if not due_date and invoice_date:
        # Derived rather than left blank: SOP §4 is explicit that vendor terms
        # are ignored, so there is nothing to look up on the invoice.
        parsed = _as_date(invoice_date)
        due_date = due_date_for(parsed) if parsed else None

    # ── Warnings: proceed, but read this first ──
    bill_url = None
    if project and project.get("bt_job_id"):
        bill_url = (
            f"{settings.buildertrend_base_url.rstrip('/')}"
            f"/app/Bills/Bill/0/{project['bt_job_id']}"
        )
    elif project:
        warnings.append(
            "No BuilderTrend job id is recorded for this project, so there "
            "is no direct bill URL. Navigate to the job by name and confirm "
            "the Job field before typing anything (SOP §8.5)."
        )

    age = _age_days(row)
    if age is not None and age > DUPLICATE_CHECK_AGE_DAYS:
        warnings.append(
            f"This invoice is {age} days old. Before saving, check the job's "
            "Bills → All Bills tab filtered by Pay To for a bill at this "
            "amount and date (SOP §4.2)."
        )

    if row.get("is_credit") or (amount is not None and amount < 0):
        warnings.append(
            "Credit memo. Enter it as a normal bill on the same job with the "
            "negative amount (SOP §5)."
        )

    if project and not project.get("drive_folder_name"):
        warnings.append(
            "No Drive folder name is recorded for this project, so filing "
            "will stop and ask after the save. The BuilderTrend part is "
            "unaffected."
        )
    if vendor and not (vendor.get("drive_folder_name") or vendor.get("invoice_name")):
        warnings.append(
            "No Drive folder name is recorded for this vendor, so filing "
            "will stop and ask after the save."
        )

    if not row.get("pdf_storage_path"):
        blockers.append(
            "No PDF is stored for this invoice, and SOP §4 requires it in "
            "Custom fields → Invoice."
        )
        pdf_url = None
    else:
        try:
            pdf_url = storage.signed_url(
                storage.INVOICES,
                row["pdf_storage_path"],
                expires_in=QUEUE_PDF_URL_TTL,
            )
        except Exception as e:  # noqa: BLE001
            # One unreachable object must not take the whole queue down with
            # it. The invoice is blocked, everything else still gets worked.
            log.exception("signed URL failed for invoice %s", row["id"])
            pdf_url = None
            blockers.append(
                f"The stored PDF could not be prepared for download ({e}). "
                "SOP §4 requires it attached, so hold this one."
            )

    quirks = (project or {}).get("quirks") or {}
    if quirks:
        warnings.append(
            "This project has recorded quirks. Read them before starting."
        )

    return UploadQueueItem(
        invoice_id=row["id"],
        bt_job_id=(project or {}).get("bt_job_id"),
        bill_url=bill_url,
        project_no=(project or {}).get("project_no"),
        project_name=(project or {}).get("name"),
        bill_title=bill_title,
        bill_no=bill_no,
        pay_to=(vendor or {}).get("bt_name"),
        invoice_no=row.get("invoice_no"),
        invoice_date=_as_date(invoice_date),
        due_date=_as_date(due_date),
        amount=amount,
        is_credit=bool(row.get("is_credit")),
        costs=cost_items,
        pdf_url=pdf_url,
        pdf_expires_in=QUEUE_PDF_URL_TTL,
        quirks=quirks,
        project_notes=(project or {}).get("notes"),
        vendor_notes=(vendor or {}).get("notes"),
        age_days=age,
        # Deduplicated: a per-cost-row check can add the same sentence twice
        # on a split invoice, and a warning repeated three times reads like
        # three problems.
        warnings=list(dict.fromkeys(warnings)),
        blockers=list(dict.fromkeys(blockers)),
    )


def _as_date(value) -> Optional[date]:
    if value is None or isinstance(value, date):
        return value if not isinstance(value, datetime) else value.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _join_codes(codes: list[str]) -> str:
    if len(codes) == 1:
        return codes[0]
    return ", ".join(codes[:-1]) + " and " + codes[-1]


# ─── Detail ───────────────────────────────────────────────────────────


@router.get("/{invoice_id}", response_model=InvoiceDetail)
def get_invoice(invoice_id: str, user: CurrentUser = Depends(get_current_user)):
    return _detail(invoice_id)


def _detail(invoice_id: str) -> InvoiceDetail:
    """The full detail payload, with no auth dependency of its own.

    Every write endpoint returns it, and the Chrome session's endpoints have
    an Actor rather than a CurrentUser — so the assembly lives here and the
    route above is only the auth wrapper.
    """
    row = _fetch_invoice(invoice_id)
    lookups = _lookups()

    lines = (
        _sb()
        .table("invoice_lines")
        .select("*")
        .eq("invoice_id", invoice_id)
        .order("sort_order")
        .execute()
        .data
        or []
    )
    costs = (
        _sb()
        .table("invoice_costs")
        .select("*")
        .eq("invoice_id", invoice_id)
        .order("sort_order")
        .execute()
        .data
        or []
    )
    suggestion_rows = (
        _sb()
        .table("invoice_suggestions")
        .select("*")
        .eq("invoice_id", invoice_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    audit_rows = (
        _sb()
        .table("audit_log")
        .select("*")
        .eq("invoice_id", invoice_id)
        .order("at", desc=True)
        .limit(200)
        .execute()
        .data
        or []
    )

    suggestion = suggestion_rows[0] if suggestion_rows else None
    base = _to_out(row, lookups, costs=costs, suggestion=suggestion)

    duplicate_out = None
    if row.get("duplicate_of_invoice_id"):
        dup = (
            _sb()
            .table("invoices")
            .select("*")
            .eq("id", row["duplicate_of_invoice_id"])
            .limit(1)
            .execute()
            .data
        )
        if dup:
            duplicate_out = _to_out(dup[0], lookups)

    return InvoiceDetail(
        **base.model_dump(),
        lines=[
            InvoiceLineOut(**{k: l.get(k) for k in InvoiceLineOut.model_fields if k in l})
            for l in lines
        ],
        costs=[
            InvoiceCostOut(
                **{k: c.get(k) for k in InvoiceCostOut.model_fields if k in c},
                code=(lookups["codes"].get(c["cost_code_id"]) or {}).get("code"),
                base_code=(lookups["codes"].get(c["cost_code_id"]) or {}).get("base_code"),
            )
            for c in costs
        ],
        suggestion=(
            SuggestionOut(
                **{k: suggestion.get(k) for k in SuggestionOut.model_fields if k in suggestion},
                code=(lookups["codes"].get(suggestion.get("cost_code_id") or "") or {}).get("code"),
            )
            if suggestion
            else None
        ),
        audit=[
            AuditEntryOut(
                **{k: a.get(k) for k in AuditEntryOut.model_fields if k in a},
                actor_name=lookups["users"].get(a.get("actor_id") or ""),
            )
            for a in audit_rows
        ],
        duplicate_of=duplicate_out,
    )


@router.get("/{invoice_id}/pdf-url", response_model=SignedUrlOut)
def get_pdf_url(invoice_id: str, actor: Actor = Depends(agent_or_user())):
    """Signed URL for the invoice PDF.

    Not role-gated beyond authentication: prompt §10 says downloads are not
    gated, same as the pay app. The Chrome session is allowed too — a batch
    can outlive the URLs handed out with the upload queue (SOP §8.1 budgets a
    save-reload cycle per bill), and it has to be able to ask for a fresh one
    rather than skip the PDF.
    """
    row = _fetch_invoice(invoice_id)
    if not row.get("pdf_storage_path"):
        raise HTTPException(
            status_code=404,
            detail="No PDF stored for this invoice; intake did not complete.",
        )
    return SignedUrlOut(
        url=storage.signed_url(storage.INVOICES, row["pdf_storage_path"])
    )


# ─── Metadata correction ──────────────────────────────────────────────


@router.patch("/{invoice_id}", response_model=InvoiceDetail)
def update_invoice(
    invoice_id: str,
    payload: InvoiceUpdate,
    actor: Actor = Depends(as_actor),
    user: CurrentUser = Depends(require_role("admin", "accountant", "pe")),
):
    """Correct what the invoice says.

    Cannot change status — `InvoiceUpdate` has no such field. Refused outright
    once the invoice is past review, because editing an approved record in
    place rewrites the basis of a decision that has already been made.
    """
    before = _fetch_invoice(invoice_id)

    if before["status"] not in PATCHABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This invoice is {before['status']} and can no longer be "
                "edited. Reject it back to the reviewer, or void it."
            ),
        )

    changes = payload.model_dump(exclude_unset=True)
    cost_rows = changes.pop("costs", None)

    if not changes and cost_rows is None:
        raise HTTPException(status_code=422, detail="No fields to update.")

    if changes.get("project_id"):
        _require_row("projects", changes["project_id"], "project")
    if changes.get("vendor_id"):
        _require_row("vendors", changes["vendor_id"], "vendor")

    # Derived fields follow their source. The SOP rules are not the client's
    # to send: bill_no and due_date are computed, so a client that changed the
    # invoice number or date does not also get to decide these.
    if "invoice_no" in changes:
        changes["bill_no"] = bill_no_for(changes["invoice_no"])
    if "invoice_date" in changes:
        if changes["invoice_date"]:
            parsed = changes["invoice_date"]
            if isinstance(parsed, str):
                parsed = date.fromisoformat(parsed)
            changes["due_date"] = str(due_date_for(parsed))
        else:
            # Clearing the source clears what was derived from it. Leaving a
            # stale due date behind a null invoice date is how a wrong date
            # reaches BuilderTrend without anyone editing it.
            changes["due_date"] = None

    serializable = {
        k: (str(v) if isinstance(v, (Decimal, date, datetime)) else v)
        for k, v in changes.items()
    }

    if serializable:
        _sb().table("invoices").update(serializable).eq("id", invoice_id).execute()

    if cost_rows is not None:
        _replace_cost_rows(invoice_id, cost_rows)

    audit.log_edit(
        entity_type="invoice",
        entity_id=invoice_id,
        invoice_id=invoice_id,
        before=before,
        after=changes,
        actor=actor,
    )
    if cost_rows is not None:
        audit.record(
            entity_type="invoice",
            entity_id=invoice_id,
            invoice_id=invoice_id,
            action="costs_updated",
            actor=actor,
            diff={
                "rows": [
                    {"cost_code_id": r.cost_code_id, "amount": str(r.amount)}
                    for r in cost_rows
                ]
            },
        )

    return _detail(invoice_id)


def _replace_cost_rows(invoice_id: str, rows: list) -> None:
    """Replace the cost split wholesale.

    Not merged: a split is a set, and a partial update would leave a row the
    reviewer thought they removed. Duplicate codes are rejected rather than
    collapsed, because the UNIQUE(invoice_id, cost_code_id) constraint would
    otherwise surface as an opaque 500.
    """
    seen: set[str] = set()
    for r in rows:
        if r.cost_code_id in seen:
            raise HTTPException(
                status_code=422,
                detail=(
                    "The same cost code appears twice. Combine the amounts "
                    "into one row instead."
                ),
            )
        seen.add(r.cost_code_id)
        _require_row("cost_codes", r.cost_code_id, "cost code")

    _sb().table("invoice_costs").delete().eq("invoice_id", invoice_id).execute()
    if rows:
        _sb().table("invoice_costs").insert(
            [
                {
                    "invoice_id": invoice_id,
                    "cost_code_id": r.cost_code_id,
                    "amount": str(r.amount),
                    "sort_order": i,
                }
                for i, r in enumerate(rows)
            ]
        ).execute()


def _require_row(table: str, row_id: str, label: str) -> dict:
    res = _sb().table(table).select("*").eq("id", row_id).limit(1).execute()
    if not res.data:
        raise HTTPException(status_code=422, detail=f"Unknown {label}: {row_id}")
    return res.data[0]


# ─── Triage (prompt §10 /flagged) ─────────────────────────────────────


@router.post("/{invoice_id}/flag", response_model=InvoiceDetail)
def flag(
    invoice_id: str,
    payload: FlagIn,
    actor: Actor = Depends(
        require_agent_or_role("admin", "accountant", "pe")
    ),
):
    """Flag an invoice, with a reason.

    Open to the Chrome session (§11). §7.6 step 5: on any stop-and-ask
    condition the session leaves the record in `approved`, flags it with the
    reason, and moves to the next invoice — so this is the one write the
    session makes that is not a success report. It is also the only reason
    the agent is trusted with a write at all; it can raise a hand, never
    lower one.
    """
    row = _fetch_invoice(invoice_id)
    if row["status"] == "void":
        raise HTTPException(
            status_code=409, detail="A voided invoice cannot be flagged."
        )
    if row["status"] in ("uploaded", "filed"):
        # Flagging replaces the status, and /flagged offers actions — void,
        # re-run the AI, reassign — that contradict a bill already sitting in
        # BuilderTrend. A problem found after the save belongs on /uploads.
        raise HTTPException(
            status_code=409,
            detail=(
                "This invoice is already in BuilderTrend, so it cannot go "
                "back into the flagged triage queue. Record what went wrong "
                "on /uploads, or fix the bill in BuilderTrend."
            ),
        )
    intake.flag_invoice(
        invoice_id,
        code=payload.code,
        detail=payload.detail,
        actor=actor,
        current_status=row["status"],
    )
    return _detail(invoice_id)


@router.post("/{invoice_id}/unflag", response_model=InvoiceDetail)
def unflag(
    invoice_id: str,
    actor: Actor = Depends(as_actor),
    user: CurrentUser = Depends(require_role("admin", "accountant")),
):
    """Clear a flag, restoring the state the invoice was in before it.

    An invoice flagged straight out of intake has nothing to go back to, so it
    returns to `ingested` and can be re-run through the AI.
    """
    row = _fetch_invoice(invoice_id)
    if row["status"] != "flagged":
        raise HTTPException(
            status_code=409,
            detail=f"This invoice is {row['status']}, not flagged.",
        )

    restored = row.get("status_before_flag") or "ingested"
    _sb().table("invoices").update(
        {
            "status": restored,
            "flag_code": None,
            "flag_detail": None,
            "flagged_at": None,
            "status_before_flag": None,
        }
    ).eq("id", invoice_id).execute()

    audit.log_transition(
        invoice_id=invoice_id,
        action="unflagged",
        from_status="flagged",
        to_status=restored,
        actor=actor,
        diff={"cleared_flag": row.get("flag_code")},
    )
    return _detail(invoice_id)


@router.post("/{invoice_id}/not-duplicate", response_model=InvoiceDetail)
def mark_not_duplicate(
    invoice_id: str,
    payload: NotDuplicateIn,
    actor: Actor = Depends(as_actor),
    user: CurrentUser = Depends(require_role("admin", "accountant")),
):
    """Clear a possible_duplicate flag (prompt §10 quick action).

    Drops the link to the other invoice as well, so the next retry does not
    immediately re-flag against the record Linda just cleared.
    """
    row = _fetch_invoice(invoice_id)
    if row.get("flag_code") != "possible_duplicate":
        raise HTTPException(
            status_code=409,
            detail="This invoice is not flagged as a possible duplicate.",
        )

    restored = row.get("status_before_flag") or "ingested"
    _sb().table("invoices").update(
        {
            "status": restored,
            "flag_code": None,
            "flag_detail": None,
            "flagged_at": None,
            "status_before_flag": None,
            "duplicate_of_invoice_id": None,
        }
    ).eq("id", invoice_id).execute()

    audit.record(
        entity_type="invoice",
        entity_id=invoice_id,
        invoice_id=invoice_id,
        action="marked_not_duplicate",
        actor=actor,
        from_status="flagged",
        to_status=restored,
        diff={
            "was_linked_to": row.get("duplicate_of_invoice_id"),
            "note": payload.note,
        },
    )
    return _detail(invoice_id)


@router.post("/{invoice_id}/retry-suggestion", response_model=InvoiceDetail)
def retry_suggestion(
    invoice_id: str,
    actor: Actor = Depends(as_actor),
    user: CurrentUser = Depends(require_role("admin", "accountant")),
):
    """Re-run the AI over a stored invoice (prompt §10, §11).

    Used after Linda corrects the project or vendor on a flagged record — the
    correction is passed to the model as a settled fact, so the retry is not
    just the same call again.
    """
    row = _fetch_invoice(invoice_id)

    # A retry rewrites the line items and the cost split wholesale. The line
    # to draw is not the status but whether a person has already put their
    # judgment into the record: once `reviewed_at` is stamped, someone
    # certified these values, and silently replacing them with a fresh guess
    # would erase a decision without telling anyone.
    if row.get("reviewed_at"):
        raise HTTPException(
            status_code=409,
            detail=(
                "A reviewer has already signed off on this invoice's values. "
                "Re-running the AI would overwrite them. Reject it back to "
                "the reviewer first if the coding needs to change."
            ),
        )
    if row["status"] in ("approved", "uploaded", "filed", "void"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"This invoice is {row['status']}; re-running the AI would "
                "overwrite values a person already approved."
            ),
        )
    if not settings.claude_enabled:
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY is not configured, so the AI cannot run.",
        )

    try:
        intake.apply_extraction(invoice_id, actor=actor)
    except Exception as e:
        log.exception("retry-suggestion failed for %s", invoice_id)
        raise HTTPException(status_code=502, detail=f"The AI call failed: {e}")

    return _detail(invoice_id)


@router.post("/{invoice_id}/void", response_model=InvoiceDetail)
def void(
    invoice_id: str,
    payload: VoidIn,
    actor: Actor = Depends(as_actor),
    user: CurrentUser = Depends(require_role("admin", "accountant")),
):
    """Void an invoice with a reason (prompt §6: admin or accountant only).

    Refused once the bill is in BuilderTrend: voiding here would not void it
    there, and the two would silently disagree.
    """
    row = _fetch_invoice(invoice_id)
    if row["status"] in ("uploaded", "filed"):
        raise HTTPException(
            status_code=409,
            detail=(
                "This invoice is already in BuilderTrend. Void the bill there "
                "first, then record it here."
            ),
        )
    if row["status"] == "void":
        return _detail(invoice_id)

    _sb().table("invoices").update(
        {
            "status": "void",
            "voided_at": datetime.now(timezone.utc).isoformat(),
            "void_reason": payload.reason,
        }
    ).eq("id", invoice_id).execute()

    audit.log_transition(
        invoice_id=invoice_id,
        action="voided",
        from_status=row["status"],
        to_status="void",
        actor=actor,
        diff={"reason": payload.reason},
    )
    return _detail(invoice_id)


# ─── Workflow transitions (prompt §7.3–7.5) ───────────────────────────
#
# Every one goes through state_machine.apply(), which validates the current
# state, stamps the actor and a UTC time, and writes audit_log. The route's
# job is the role check, the payload, and turning a TransitionError into the
# right HTTP status — nothing more, so a new transition cannot accidentally
# skip one of the three.


def _transition_error(e: state_machine.TransitionError) -> HTTPException:
    return HTTPException(status_code=e.status_code, detail=str(e))


@router.post("/{invoice_id}/assign", response_model=InvoiceDetail)
def assign(
    invoice_id: str,
    payload: AssignIn,
    actor: Actor = Depends(as_actor),
    user: CurrentUser = Depends(require_role("admin", "accountant")),
):
    """§7.3: Linda assigns a reviewer and an approver.

    Both default to what intake already filled in from the project — the PE
    as reviewer, the project's default approver — so an assign with an empty
    body accepts those. Passing either overrides it.
    """
    row = _fetch_invoice(invoice_id)

    reviewer_id = payload.reviewer_id or row.get("reviewer_id")
    approver_id = payload.approver_id or row.get("approver_id")

    if not reviewer_id:
        raise HTTPException(
            status_code=422,
            detail=(
                "This invoice has no reviewer. Its project has no assigned "
                "project engineer, so pick one here."
            ),
        )

    _require_active_user(reviewer_id, "reviewer")
    if approver_id:
        _require_active_user(approver_id, "approver", must_be_approver=True)

    try:
        updated = state_machine.apply(
            invoice=row,
            transition=state_machine.ASSIGN,
            actor=actor,
            extra_fields={
                "reviewer_id": reviewer_id,
                "approver_id": approver_id,
                # Assigning out of `flagged` clears the flag: the invoice is
                # being routed, so leaving the flag set would keep it in the
                # triage queue at the same time as a reviewer's queue.
                "flag_code": None,
                "flag_detail": None,
                "flagged_at": None,
                "status_before_flag": None,
            },
            diff={"reviewer_id": reviewer_id, "approver_id": approver_id},
        )
    except state_machine.TransitionError as e:
        raise _transition_error(e)

    return _detail(updated["id"])


@router.post("/bulk-assign", response_model=BulkAssignResult)
def bulk_assign(
    payload: BulkAssignIn,
    actor: Actor = Depends(as_actor),
    _: CurrentUser = Depends(require_role("admin", "accountant")),
):
    """§7.3: "She can also bulk-assign."

    Per-invoice outcomes rather than one all-or-nothing result. One invoice
    that changed state since the page loaded should not block the other
    nineteen, and the caller needs to know which one it was.
    """
    if payload.reviewer_id:
        _require_active_user(payload.reviewer_id, "reviewer")
    if payload.approver_id:
        _require_active_user(payload.approver_id, "approver", must_be_approver=True)

    result = BulkAssignResult()
    for invoice_id in payload.invoice_ids:
        try:
            row = _fetch_invoice(invoice_id)
        except HTTPException:
            result.failed += 1
            result.results.append(
                {"invoice_id": invoice_id, "ok": False, "detail": "Not found."}
            )
            continue

        reviewer_id = payload.reviewer_id or row.get("reviewer_id")
        approver_id = payload.approver_id or row.get("approver_id")
        if not reviewer_id:
            result.failed += 1
            result.results.append(
                {
                    "invoice_id": invoice_id,
                    "ok": False,
                    "detail": "No reviewer, and its project has no PE.",
                }
            )
            continue

        try:
            state_machine.apply(
                invoice=row,
                transition=state_machine.ASSIGN,
                actor=actor,
                extra_fields={
                    "reviewer_id": reviewer_id,
                    "approver_id": approver_id,
                    "flag_code": None,
                    "flag_detail": None,
                    "flagged_at": None,
                    "status_before_flag": None,
                },
                diff={"reviewer_id": reviewer_id, "approver_id": approver_id},
            )
            result.assigned += 1
            result.results.append({"invoice_id": invoice_id, "ok": True})
        except state_machine.TransitionError as e:
            result.failed += 1
            result.results.append(
                {"invoice_id": invoice_id, "ok": False, "detail": str(e)}
            )

    return result


@router.post("/{invoice_id}/reassign", response_model=InvoiceDetail)
def reassign(
    invoice_id: str,
    payload: ReassignIn,
    actor: Actor = Depends(as_actor),
    user: CurrentUser = Depends(require_role("admin", "accountant")),
):
    """Move an in-flight invoice to a different reviewer or approver.

    Not a state transition — the invoice stays where it is. This is how an
    absent approver gets worked around, and it is recorded, which is why
    approve itself can insist on the assigned approver.
    """
    row = _fetch_invoice(invoice_id)

    if row["status"] not in ("assigned", "pending_approval"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"This invoice is {row['status']}. Reassigning applies to "
                "invoices in review or awaiting approval."
            ),
        )

    changes: dict = {}
    if payload.reviewer_id is not None:
        _require_active_user(payload.reviewer_id, "reviewer")
        changes["reviewer_id"] = payload.reviewer_id
    if payload.approver_id is not None:
        _require_active_user(
            payload.approver_id, "approver", must_be_approver=True
        )
        changes["approver_id"] = payload.approver_id

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="Name a reviewer or an approver to reassign to.",
        )

    _sb().table("invoices").update(changes).eq("id", invoice_id).execute()
    audit.log_edit(
        entity_type="invoice",
        entity_id=invoice_id,
        invoice_id=invoice_id,
        before=row,
        after=changes,
        actor=actor,
        action="reassigned",
    )
    return _detail(invoice_id)


@router.post("/{invoice_id}/mark-reviewed", response_model=InvoiceDetail)
def mark_reviewed(
    invoice_id: str,
    payload: MarkReviewedIn,
    actor: Actor = Depends(as_actor),
    user: CurrentUser = Depends(require_role("admin", "accountant", "pe")),
):
    """§7.4: the reviewer certifies the values and hands it to an approver.

    The role check above says "could be a reviewer"; the record-level check
    below says "is THIS invoice's reviewer". Both are needed — one PE should
    not be able to certify another's invoice, because the audit trail has to
    record that the person asked is the person who signed off.
    """
    row = _fetch_invoice(invoice_id)

    try:
        state_machine.require_assigned_reviewer(row, actor)
    except state_machine.TransitionError as e:
        raise _transition_error(e)

    # Setting the approver in the same call, before the precondition runs, so
    # "pick an approver" and "mark reviewed" are one action for the reviewer.
    extra: dict = {}
    if payload.approver_id:
        _require_active_user(
            payload.approver_id, "approver", must_be_approver=True
        )
        extra["approver_id"] = payload.approver_id
        row = {**row, "approver_id": payload.approver_id}

    try:
        updated = state_machine.apply(
            invoice=row,
            transition=state_machine.MARK_REVIEWED,
            actor=actor,
            extra_fields={
                **extra,
                # A rejection that has now been addressed: clear the reason so
                # the banner stops showing stale feedback on the way forward.
                "reject_reason": None,
            },
            diff={"approver_id": row.get("approver_id")},
        )
    except state_machine.TransitionError as e:
        raise _transition_error(e)

    return _detail(updated["id"])


@router.post("/{invoice_id}/approve", response_model=InvoiceDetail)
def approve(
    invoice_id: str,
    actor: Actor = Depends(as_actor),
    user: CurrentUser = Depends(require_role("admin", "approver")),
):
    """§7.5: the approver signs off.

    This is the state the Chrome session reads, so it is the last human gate
    before BuilderTrend. The balance check runs again here — the split can be
    edited between review and approval, and approving an unbalanced invoice
    would send a wrong total.
    """
    row = _fetch_invoice(invoice_id)

    try:
        state_machine.require_assigned_approver(row, actor)
        updated = state_machine.apply(
            invoice=row,
            transition=state_machine.APPROVE,
            actor=actor,
            diff={
                "amount": str(row.get("amount")),
                "reviewer_id": row.get("reviewer_id"),
            },
        )
    except state_machine.TransitionError as e:
        raise _transition_error(e)

    return _detail(updated["id"])


@router.post("/{invoice_id}/reject", response_model=InvoiceDetail)
def reject(
    invoice_id: str,
    payload: RejectIn,
    actor: Actor = Depends(as_actor),
    user: CurrentUser = Depends(require_role("admin", "approver")),
):
    """§7.5: send it back to the same reviewer with a reason.

    The reviewer stays put — §7.5 says "back to `assigned` with the same
    reviewer" — and the reason is shown in their banner. The review stamp is
    cleared by the state machine so the invoice cannot be approved again
    without a second review.
    """
    row = _fetch_invoice(invoice_id)

    try:
        state_machine.require_assigned_approver(row, actor)
        updated = state_machine.apply(
            invoice=row,
            transition=state_machine.REJECT,
            actor=actor,
            extra_fields={"reject_reason": payload.reason},
            diff={"reason": payload.reason},
        )
    except state_machine.TransitionError as e:
        raise _transition_error(e)

    return _detail(updated["id"])


# ─── Shared validation ────────────────────────────────────────────────


def _require_active_user(
    user_id: str, label: str, *, must_be_approver: bool = False
) -> dict:
    """A reviewer or approver has to be a real, active person.

    The approver additionally has to hold the role: assigning an invoice to
    somebody who cannot approve it parks it in a queue nobody can clear, and
    the only symptom is an invoice that never moves.
    """
    row = (
        _sb()
        .table("app_users")
        .select("id,email,name,role,deactivated_at")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    if not row.data:
        raise HTTPException(status_code=422, detail=f"Unknown {label}: {user_id}")

    found = row.data[0]
    if found.get("deactivated_at"):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{found.get('name') or found.get('email')} is deactivated and "
                f"cannot be the {label}."
            ),
        )
    if must_be_approver and "approver" not in (found.get("role") or []):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{found.get('name') or found.get('email')} does not hold the "
                "approver role, so they could never clear this invoice."
            ),
        )
    return found


# ─── Upload and filing (prompt §7.6, §7.7, §11) ───────────────────────


@router.post("/{invoice_id}/mark-uploaded", response_model=MarkUploadedOut)
def mark_uploaded(
    invoice_id: str,
    payload: MarkUploadedIn,
    actor: Actor = Depends(require_agent_or_role("admin", "accountant")),
):
    """§7.6 step 3: record a verified BuilderTrend save, then file the PDF.

    This is the endpoint §12 names: "reject if `approved_at` is null". The
    check lives in the state machine precondition so it cannot be bypassed by
    a future caller, and it is enforced here rather than inferred from the
    status column.

    The double-save guard is SOP §8.4 — "never click Save twice without
    checking whether the first one fired, that's how duplicate bills get
    created". Calling this twice with the same bill id is a quiet success, so
    a session that lost its connection mid-batch can safely replay. Calling
    it with a *different* bill id is a 409, because that means two bills now
    exist for one invoice and somebody has to go delete one.
    """
    row = _fetch_invoice(invoice_id)
    bill_id = payload.bt_bill_id

    # ── Replay, or a second bill? ──
    if row["status"] in ("uploaded", "filed"):
        if (row.get("bt_bill_id") or "") == bill_id:
            return MarkUploadedOut(
                invoice=_detail(invoice_id),
                filing=_filing_status(row),
                already_recorded=True,
            )
        raise HTTPException(
            status_code=409,
            detail=(
                f"This invoice is already recorded as bill "
                f"{row.get('bt_bill_id')!r} in BuilderTrend, and you are "
                f"reporting {bill_id!r}. That means two bills exist for one "
                "invoice. Stop, check BuilderTrend, and delete the duplicate "
                "before recording anything else."
            ),
        )

    # The same bill id on a *different* invoice is the other half of the same
    # failure: a save that landed on the wrong record. The unique index on
    # bt_bill_id is the real guarantee; this is the readable error.
    clash = (
        _sb()
        .table("invoices")
        .select("id,invoice_no")
        .eq("bt_bill_id", bill_id)
        .neq("id", invoice_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if clash:
        raise HTTPException(
            status_code=409,
            detail=(
                f"BuilderTrend bill {bill_id!r} is already recorded against "
                f"invoice {clash[0].get('invoice_no') or clash[0]['id']}. "
                "Check which invoice the bill actually belongs to before "
                "recording this one."
            ),
        )

    try:
        updated = state_machine.apply(
            invoice=row,
            transition=state_machine.MARK_UPLOADED,
            actor=actor,
            extra_fields={"bt_bill_id": bill_id},
            diff={"bt_bill_id": bill_id, "note": payload.note},
        )
    except state_machine.TransitionError as e:
        raise _transition_error(e)
    except Exception as e:  # noqa: BLE001
        # The unique index on bt_bill_id is the real duplicate guard; the
        # checks above are the readable version of it and lose to a race
        # between two concurrent reports. Translate the constraint violation
        # rather than letting it surface as a 500, because "two bills exist"
        # is something the operator has to act on, not retry.
        if _is_duplicate_bill_id_violation(e):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"BuilderTrend bill {bill_id!r} is already recorded "
                    "against another invoice. Two reports arrived at once. "
                    "Check BuilderTrend for which invoice the bill belongs "
                    "to before recording anything else."
                ),
            )
        raise

    # §14: filing runs in the backend, right here, because it is
    # deterministic. Best-effort by design — the bill is already saved, and a
    # missing Drive folder must not make a successful upload look failed.
    outcome = filing.file_and_record(invoice=updated, actor=actor)

    return MarkUploadedOut(
        invoice=_detail(invoice_id),
        filing=FilingOut(
            filed=outcome["filed"],
            filed_path=outcome["filed_path"],
            original_archived=bool(
                (outcome.get("invoice") or {}).get("original_archived")
            ),
            error=outcome["error"],
            needs_folder=outcome["needs_folder"],
            warnings=outcome["warnings"],
        ),
        already_recorded=False,
    )


@router.post("/{invoice_id}/mark-filed", response_model=MarkUploadedOut)
def mark_filed(
    invoice_id: str,
    payload: MarkFiledIn,
    actor: Actor = Depends(require_agent_or_role("admin", "accountant")),
):
    """§7.7: file the PDF copy and archive the original, or record that
    someone else already did.

    Two modes, and the payload says which:

      - **No `filed_path`** — the backend files now. This is the retry button
        on /uploads after a filing failure, and the path §14 prefers.
      - **`filed_path` set** — record a copy filed outside the app. §14
        allows Chrome to do this step "if Phase 3 shows Drive permissions
        make that awkward", and a manual filing needs recording too.

    Either way the invoice has to already be `uploaded`: filing before the
    bill exists in BuilderTrend would archive the original out of the intake
    folder while the invoice is still unentered.
    """
    row = _fetch_invoice(invoice_id)

    if row["status"] == "filed":
        # Idempotent: a replayed call after a successful filing is a success.
        return MarkUploadedOut(
            invoice=_detail(invoice_id),
            filing=_filing_status(row),
            already_recorded=True,
        )

    if payload.filed_path:
        try:
            updated = state_machine.apply(
                invoice=row,
                transition=state_machine.MARK_FILED,
                actor=actor,
                extra_fields={
                    "filed_path": payload.filed_path,
                    "original_archived": payload.original_archived,
                    "filing_error": None,
                    "filing_warnings": (
                        []
                        if payload.original_archived
                        else [
                            "Filed outside the app and reported as not "
                            "archived. The Drive original is still in the "
                            "intake folder."
                        ]
                    ),
                },
                diff={"filed_path": payload.filed_path, "filed_externally": True},
            )
        except state_machine.TransitionError as e:
            raise _transition_error(e)
        return MarkUploadedOut(
            invoice=_detail(invoice_id), filing=_filing_status(updated)
        )

    if row["status"] != "uploaded":
        raise HTTPException(
            status_code=409,
            detail=(
                f"This invoice is {row['status']}. Filing happens after the "
                "bill is saved in BuilderTrend, so there is nothing to file "
                "yet."
            ),
        )

    outcome = filing.file_and_record(invoice=row, actor=actor)
    if not outcome["filed"]:
        # 502 rather than 500: the failure is in Drive, not in this app, and
        # the message says what a person has to do about it.
        raise HTTPException(status_code=502, detail=outcome["error"])

    return MarkUploadedOut(
        invoice=_detail(invoice_id),
        filing=FilingOut(
            filed=True,
            filed_path=outcome["filed_path"],
            original_archived=bool(
                (outcome.get("invoice") or {}).get("original_archived")
            ),
            warnings=outcome["warnings"],
        ),
    )


def _is_duplicate_bill_id_violation(e: Exception) -> bool:
    """Is this Postgres 23505 on idx_invoices_bt_bill_id?

    Matched on both the SQLSTATE and the index name so an unrelated unique
    violation still surfaces as a 500 with an error id, rather than being
    mislabelled as a duplicate bill.
    """
    code = getattr(e, "code", None) or (getattr(e, "args", [{}])[0] if e.args else None)
    text = f"{code} {e}".lower()
    return "23505" in text and "bt_bill_id" in text


def _filing_status(row: dict) -> FilingOut:
    return FilingOut(
        filed=bool(row.get("filed_at")),
        filed_path=row.get("filed_path"),
        original_archived=bool(row.get("original_archived")),
        error=row.get("filing_error"),
        warnings=list(row.get("filing_warnings") or []),
    )
