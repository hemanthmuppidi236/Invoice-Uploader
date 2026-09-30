"""
Invoice request/response schemas.

`InvoiceUpdate` is the one that matters for safety. Prompt §6: "PATCH
/invoices/{id} uses extra="forbid" and never accepts status. Status only moves
through the named endpoints." So every status field, every workflow timestamp,
and every actor field is absent from it by construction — a client trying to
smuggle `status` gets a 422 naming the field rather than a silent no-op.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator

INVOICE_STATUSES = (
    "ingested",
    "suggested",
    "assigned",
    "pending_approval",
    "approved",
    "uploaded",
    "filed",
    "flagged",
    "void",
)

FLAG_CODES = (
    "unknown_vendor",
    "unknown_project",
    "new_project",
    "project_not_onboarded",
    "possible_duplicate",
    "unreadable",
    "yard",
    "ai_error",
    "stop_and_ask",
    "other",
)


# ─── Nested ───────────────────────────────────────────────────────────


class InvoiceLineOut(BaseModel):
    id: str
    ticket_no: Optional[str] = None
    prod_num: Optional[str] = None
    description: Optional[str] = None
    qty: Optional[Decimal] = None
    uom: Optional[str] = None
    unit_price: Optional[Decimal] = None
    gross: Optional[Decimal] = None
    sort_order: int = 0


class InvoiceCostOut(BaseModel):
    id: str
    cost_code_id: str
    code: Optional[str] = None        # joined display value
    base_code: Optional[str] = None   # what goes in BuilderTrend's Title field
    amount: Decimal
    sort_order: int = 0


class SuggestionOut(BaseModel):
    id: str
    cost_code_id: Optional[str] = None
    code: Optional[str] = None
    confidence: Optional[float] = None
    rationale: Optional[str] = None
    alternatives: list[dict] = []
    extracted: dict = {}
    model: Optional[str] = None
    created_at: Optional[datetime] = None


class AuditEntryOut(BaseModel):
    id: str
    action: str
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    actor_id: Optional[str] = None
    actor_name: Optional[str] = None
    actor_label: Optional[str] = None
    diff: Optional[dict] = None
    at: datetime


# ─── Invoice ──────────────────────────────────────────────────────────


class InvoiceOut(BaseModel):
    id: str
    project_id: Optional[str] = None
    project_no: Optional[str] = None
    project_name: Optional[str] = None
    vendor_id: Optional[str] = None
    vendor_name: Optional[str] = None
    vendor_bt_name: Optional[str] = None

    invoice_no: Optional[str] = None
    bill_no: Optional[str] = None
    invoice_date: Optional[date] = None
    due_date: Optional[date] = None
    amount: Optional[Decimal] = None
    is_credit: bool = False

    status: str
    reviewer_id: Optional[str] = None
    reviewer_name: Optional[str] = None
    approver_id: Optional[str] = None
    approver_name: Optional[str] = None

    assigned_at: Optional[datetime] = None
    reviewed_at: Optional[datetime] = None
    approved_at: Optional[datetime] = None
    rejected_at: Optional[datetime] = None
    reject_reason: Optional[str] = None
    uploaded_at: Optional[datetime] = None
    filed_at: Optional[datetime] = None

    flagged_at: Optional[datetime] = None
    flag_code: Optional[str] = None
    flag_detail: Optional[str] = None
    status_before_flag: Optional[str] = None
    duplicate_of_invoice_id: Optional[str] = None

    bt_bill_id: Optional[str] = None
    filed_path: Optional[str] = None
    void_reason: Optional[str] = None

    source_filename: Optional[str] = None
    source_path: Optional[str] = None
    source_file_id: Optional[str] = None
    source_page: int = 1
    pdf_storage_path: Optional[str] = None

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # Derived, for the list and detail screens
    suggested_code: Optional[str] = None
    suggested_confidence: Optional[float] = None
    costs_balanced: bool = False
    costs_difference: Optional[Decimal] = None
    age_days: Optional[int] = None


class InvoiceDetail(InvoiceOut):
    lines: list[InvoiceLineOut] = []
    costs: list[InvoiceCostOut] = []
    suggestion: Optional[SuggestionOut] = None
    audit: list[AuditEntryOut] = []
    duplicate_of: Optional[InvoiceOut] = None


class InvoiceCostIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cost_code_id: str
    amount: Decimal


class InvoiceUpdate(BaseModel):
    """Editable invoice metadata.

    Deliberately omits `status` and every workflow timestamp and actor field.
    A PATCH can correct what the invoice SAYS; only the named transition
    endpoints can change where it IS.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: Optional[str] = None
    vendor_id: Optional[str] = None
    invoice_no: Optional[str] = None
    invoice_date: Optional[date] = None
    due_date: Optional[date] = None
    amount: Optional[Decimal] = None
    is_credit: Optional[bool] = None
    costs: Optional[list[InvoiceCostIn]] = None

    @field_validator("invoice_no")
    @classmethod
    def _trim(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return v.strip() or None


# ─── Transitions ──────────────────────────────────────────────────────


class FlagIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = "other"
    detail: str

    @field_validator("code")
    @classmethod
    def _known_code(cls, v: str) -> str:
        if v not in FLAG_CODES:
            raise ValueError(
                f"Unknown flag code {v!r}. Valid: {', '.join(FLAG_CODES)}"
            )
        return v

    @field_validator("detail")
    @classmethod
    def _detail_required(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("A reason is required so triage knows what to fix.")
        return v


class VoidIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_required(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Voiding requires a reason.")
        return v


class NotDuplicateIn(BaseModel):
    """Clear a possible_duplicate flag and record why (prompt §10)."""

    model_config = ConfigDict(extra="forbid")

    note: Optional[str] = None


# ─── Dashboard ────────────────────────────────────────────────────────


class InvoiceStats(BaseModel):
    """The §10 stat cards."""

    unassigned: int = 0
    in_review: int = 0
    pending_approval: int = 0
    approved_not_uploaded: int = 0
    flagged: int = 0
    total_open: int = 0
    flagged_by_reason: dict[str, int] = {}


class InvoiceDashboard(BaseModel):
    invoices: list[InvoiceOut] = []
    stats: InvoiceStats = InvoiceStats()
    your_court: list[InvoiceOut] = []
    your_court_label: Optional[str] = None


class SignedUrlOut(BaseModel):
    url: str
    expires_in: int = 3600


class PollResultOut(BaseModel):
    scanned: int = 0
    ingested: int = 0
    suggested: int = 0
    flagged: int = 0
    skipped_existing: int = 0
    errors: list[str] = []
    detail: list[dict] = []


# ─── Workflow transitions (prompt §7.3–7.5) ───────────────────────────


class AssignIn(BaseModel):
    """§7.3: Linda assigns. The reviewer defaults to the project's PE, which
    intake already filled in at `suggested`, so both fields are optional —
    an assign with an empty body accepts the defaults."""

    model_config = ConfigDict(extra="forbid")

    reviewer_id: Optional[str] = None
    approver_id: Optional[str] = None


class BulkAssignIn(BaseModel):
    """§7.3: "She can also bulk-assign."

    Applies the same reviewer and approver across several invoices. Partial
    success is reported per invoice rather than failing the batch, because one
    invoice that moved state since the page loaded should not block the other
    nineteen.
    """

    model_config = ConfigDict(extra="forbid")

    invoice_ids: list[str]
    reviewer_id: Optional[str] = None
    approver_id: Optional[str] = None

    @field_validator("invoice_ids")
    @classmethod
    def _not_empty(cls, v: list[str]) -> list[str]:
        cleaned = [i for i in v if i]
        if not cleaned:
            raise ValueError("Pick at least one invoice.")
        if len(cleaned) > 200:
            raise ValueError("Assign at most 200 invoices at a time.")
        return list(dict.fromkeys(cleaned))


class ReassignIn(BaseModel):
    """Change the reviewer or approver on an invoice already in flight.

    At least one has to be named — an empty reassign is a no-op that would
    still write an audit row saying something changed.
    """

    model_config = ConfigDict(extra="forbid")

    reviewer_id: Optional[str] = None
    approver_id: Optional[str] = None


class MarkReviewedIn(BaseModel):
    """§7.4: "The reviewer may change it when marking reviewed."

    Letting the approver be set in the same call is deliberate: the reviewer
    knows who should sign off, and making them save the field and then click
    Reviewed is two steps for one decision.
    """

    model_config = ConfigDict(extra="forbid")

    approver_id: Optional[str] = None


class RejectIn(BaseModel):
    """§7.5: reject with a reason, required."""

    model_config = ConfigDict(extra="forbid")

    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_required(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError(
                "A reason is required — the reviewer sees it in the banner and "
                "needs to know what to change."
            )
        return v


class BulkAssignResult(BaseModel):
    assigned: int = 0
    failed: int = 0
    results: list[dict] = []


class EmailJobResultOut(BaseModel):
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    recipients: list[dict] = []
    notes: list[str] = []
