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

from fastapi import APIRouter, Depends, HTTPException, Query, status as http

from ..core import audit, intake, state_machine, storage
from ..core.auth import Actor, CurrentUser, as_actor, get_current_user, require_role
from ..core.config import settings
from ..core.invoice_rules import costs_balance, bill_no_for, due_date_for, to_money
from ..core.supabase_client import get_service_client
from ..schemas.invoices import (
    AssignIn,
    AuditEntryOut,
    BulkAssignIn,
    BulkAssignResult,
    FlagIn,
    MarkReviewedIn,
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


# ─── Detail ───────────────────────────────────────────────────────────


@router.get("/{invoice_id}", response_model=InvoiceDetail)
def get_invoice(invoice_id: str, user: CurrentUser = Depends(get_current_user)):
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
def get_pdf_url(invoice_id: str, user: CurrentUser = Depends(get_current_user)):
    """Signed URL for the invoice PDF.

    Not role-gated beyond authentication: prompt §10 says downloads are not
    gated, same as the pay app.
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

    return get_invoice(invoice_id, user=user)


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
    actor: Actor = Depends(as_actor),
    user: CurrentUser = Depends(require_role("admin", "accountant", "pe")),
):
    """Flag an invoice from any state, with a reason."""
    row = _fetch_invoice(invoice_id)
    if row["status"] == "void":
        raise HTTPException(
            status_code=409, detail="A voided invoice cannot be flagged."
        )
    intake.flag_invoice(
        invoice_id,
        code=payload.code,
        detail=payload.detail,
        actor=actor,
        current_status=row["status"],
    )
    return get_invoice(invoice_id, user=user)


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
    return get_invoice(invoice_id, user=user)


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
    return get_invoice(invoice_id, user=user)


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

    return get_invoice(invoice_id, user=user)


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
        return get_invoice(invoice_id, user=user)

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
    return get_invoice(invoice_id, user=user)


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

    return get_invoice(updated["id"], user=user)


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
    return get_invoice(invoice_id, user=user)


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

    return get_invoice(updated["id"], user=user)


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

    return get_invoice(updated["id"], user=user)


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

    return get_invoice(updated["id"], user=user)


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
