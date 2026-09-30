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
    filed_file_id: Optional[str] = None
    original_archived: bool = False
    filing_error: Optional[str] = None
    filing_warnings: list[str] = []
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


# ─── Upload to BuilderTrend (prompt §7.6, §11) ────────────────────────
#
# The queue payload is deliberately verbose. The Chrome session is driving a
# form that is slow and quirky (SOP §8), and every value it has to derive for
# itself is a value it can derive wrongly. So the base cost code, the 4-digit
# bill number, the end-of-next-month due date, the BuilderTrend vendor name
# and the bill URL are all computed here and handed over ready to type.


class UploadQueueCost(BaseModel):
    """One Costs row on the BuilderTrend bill form.

    SOP §4: "Costs row → Title: leave blank", "Unit cost: invoice total
    (Qty = 1)". So `amount` is the unit cost and there is no quantity field —
    a split invoice is several rows, never one row with a quantity.
    """

    cost_code: str
    base_code: Optional[str] = None
    name: Optional[str] = None
    amount: Decimal
    note: Optional[str] = None


class UploadQueueItem(BaseModel):
    invoice_id: str

    # Where the bill goes. SOP §8.5: job context drifts, so the session opens
    # bills by URL and then confirms the Job field on the form.
    bt_job_id: Optional[str] = None
    bill_url: Optional[str] = None
    project_no: Optional[str] = None
    project_name: Optional[str] = None

    # The form fields, in SOP §4 order.
    bill_title: Optional[str] = None
    bill_no: Optional[str] = None
    pay_to: Optional[str] = None
    invoice_no: Optional[str] = None
    invoice_date: Optional[date] = None
    due_date: Optional[date] = None
    amount: Optional[Decimal] = None
    is_credit: bool = False
    costs: list[UploadQueueCost] = []

    # The PDF goes in Custom fields → Invoice, not Attachments (SOP §4).
    pdf_url: Optional[str] = None
    pdf_expires_in: int = 3600

    # Per-project and per-vendor gotchas the session should read first.
    quirks: dict = {}
    project_notes: Optional[str] = None
    vendor_notes: Optional[str] = None

    age_days: Optional[int] = None

    # `warnings` means proceed with care. `blockers` means do not save this
    # one at all — flag it and move on. Kept as two lists rather than one
    # severity field so the session cannot treat a blocker as advisory.
    warnings: list[str] = []
    blockers: list[str] = []


class UploadQueueOut(BaseModel):
    generated_at: datetime
    count: int = 0
    queue: list[UploadQueueItem] = []
    blocked: list[UploadQueueItem] = []

    # Context for the /uploads screen and for the session's closing summary.
    awaiting_filing: list[InvoiceOut] = []
    recently_uploaded: list[InvoiceOut] = []

    # §14 chose backend filing. Surfaced so the Chrome session does not have
    # to assume: false means it should file and call mark-filed itself.
    filing_by_backend: bool = True
    notes: list[str] = []


class MarkUploadedIn(BaseModel):
    """§7.6 step 3, called only after a *verified* save (SOP §4.6).

    `bt_bill_id` is required and is the whole idempotency story. SOP §8.4:
    "Never click Save twice without checking whether the first one fired —
    that's how duplicate bills get created." Repeating the call with the same
    id is a quiet success; repeating it with a different id is a 409, because
    that means two bills exist for one invoice.
    """

    model_config = ConfigDict(extra="forbid")

    bt_bill_id: str
    note: Optional[str] = None

    @field_validator("bt_bill_id")
    @classmethod
    def _bill_id_required(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError(
                "The BuilderTrend bill id is required. It is the only way to "
                "tell a repeated call apart from a second bill, so a save "
                "that cannot be identified must not be recorded."
            )
        return v


class MarkFiledIn(BaseModel):
    """§7.7, and the §14 escape hatch.

    Leave `filed_path` unset and the backend does the filing now — that is
    the normal path and the retry button on /uploads. Set it to record a copy
    that was filed outside the backend, which is what §14 allows "if Phase 3
    shows Drive permissions make that awkward".
    """

    model_config = ConfigDict(extra="forbid")

    filed_path: Optional[str] = None
    original_archived: bool = False


class FilingOut(BaseModel):
    filed: bool = False
    filed_path: Optional[str] = None
    original_archived: bool = False
    error: Optional[str] = None
    needs_folder: bool = False
    warnings: list[str] = []


class MarkUploadedOut(BaseModel):
    invoice: InvoiceDetail
    filing: FilingOut = FilingOut()
    # True when this call found the invoice already recorded with the same
    # bill id. The session uses it to tell "I already did this" apart from
    # "I just did this", so a retried batch does not double-count.
    already_recorded: bool = False
