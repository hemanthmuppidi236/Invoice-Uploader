"""
Project and mix design request/response schemas.

`extra="forbid"` on every write model, same protection as the pay app's
PayAppUpdate: a field the API does not accept is a 422, not a silent no-op.
ProjectUpdate additionally omits `onboarded_at` and `onboarded_by` so a
generic PATCH can never self-onboard a project around the §7.0 gate.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ─── Mix design rows ──────────────────────────────────────────────────


class MixDesignRowIn(BaseModel):
    """One confirmed yardage-sheet row, as the reviewer approved it."""

    model_config = ConfigDict(extra="forbid")

    mix_no: str
    psi: Optional[int] = None
    element_use: list[str] = []
    pump_line: Optional[str] = None
    cost_code_id: Optional[str] = None

    @field_validator("mix_no")
    @classmethod
    def _mix_no_present(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("mix_no is required")
        return v

    @field_validator("element_use")
    @classmethod
    def _clean_elements(cls, v: list[str]) -> list[str]:
        return [e.strip() for e in (v or []) if e and e.strip()]


class MixDesignRowOut(BaseModel):
    id: str
    project_id: str
    revision: str
    mix_no: str
    psi: Optional[int] = None
    element_use: list[str] = []
    pump_line: Optional[str] = None
    cost_code_id: Optional[str] = None
    cost_code: Optional[str] = None          # joined display value
    superseded_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class MixDesignRowPatch(BaseModel):
    """The PE mapping an element use to a cost code (§7.0)."""

    model_config = ConfigDict(extra="forbid")

    cost_code_id: Optional[str] = None
    element_use: Optional[list[str]] = None
    psi: Optional[int] = None
    pump_line: Optional[str] = None


# ─── Parse (pre-save) ─────────────────────────────────────────────────


class ParsedMixRowOut(BaseModel):
    """A parsed row plus the AI's proposal, for the confirmation table."""

    mix_no: str
    psi: Optional[int] = None
    element_use: list[str] = []
    pump_line: Optional[str] = None
    cost_code_id: Optional[str] = None
    proposed_code: Optional[str] = None
    confidence: Optional[float] = None
    rationale: Optional[str] = None


class MixDesignParseOut(BaseModel):
    """Result of parsing an uploaded submittal. Nothing is saved yet.

    `storage_path` is where the PDF now lives; the caller passes it back on
    create/confirm so the file and the confirmed rows land together.
    """

    storage_path: str
    original_filename: str
    revision: Optional[str] = None
    project_hint: Optional[str] = None
    supplier_hint: Optional[str] = None
    rows: list[ParsedMixRowOut] = []
    notes: list[str] = []
    warnings: list[str] = []
    model: Optional[str] = None


class MixDesignConfirm(BaseModel):
    """Confirmed rows for a new submittal revision on an existing project."""

    model_config = ConfigDict(extra="forbid")

    revision: str = "CMD-01"
    storage_path: Optional[str] = None
    rows: list[MixDesignRowIn] = []


# ─── Projects ─────────────────────────────────────────────────────────


class ProjectCreate(BaseModel):
    """§7.0 onboarding payload: project details plus the confirmed mix table.

    Both arrive in one call because the confirmation table must be approved
    before anything is written — there is no half-onboarded intermediate the
    user can walk away from.
    """

    model_config = ConfigDict(extra="forbid")

    project_no: str
    name: str
    bt_job_id: Optional[str] = None
    address: Optional[str] = None
    pe_user_id: Optional[str] = None
    default_approver_id: Optional[str] = None
    status: str = "old"
    drive_folder_name: Optional[str] = None
    has_concrete_supplier: bool = True
    expected_vendor_ids: list[str] = []
    quirks: dict = {}
    notes: Optional[str] = None

    # Mix design, as confirmed on screen.
    mix_design_revision: str = "CMD-01"
    mix_design_storage_path: Optional[str] = None
    mix_design_rows: list[MixDesignRowIn] = []

    # Set true to stamp onboarded_at on create. Rejected with a 422 listing
    # what is missing if the §7.0 gate is not satisfied.
    complete_onboarding: bool = True

    @field_validator("project_no", "name")
    @classmethod
    def _required(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("required")
        return v

    @field_validator("status")
    @classmethod
    def _status_valid(cls, v: str) -> str:
        if v not in ("old", "active"):
            raise ValueError("status must be 'old' or 'active'")
        return v


class ProjectUpdate(BaseModel):
    """Editable project fields.

    Deliberately omits `onboarded_at`, `onboarded_by`, `mix_design_pdf_path`,
    and every timestamp: onboarding is stamped only by the named endpoint,
    which runs the gate first.
    """

    model_config = ConfigDict(extra="forbid")

    project_no: Optional[str] = None
    name: Optional[str] = None
    bt_job_id: Optional[str] = None
    address: Optional[str] = None
    pe_user_id: Optional[str] = None
    default_approver_id: Optional[str] = None
    status: Optional[str] = None
    drive_folder_name: Optional[str] = None
    has_concrete_supplier: Optional[bool] = None
    expected_vendor_ids: Optional[list[str]] = None
    quirks: Optional[dict] = None
    notes: Optional[str] = None

    @field_validator("status")
    @classmethod
    def _status_valid(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("old", "active"):
            raise ValueError("status must be 'old' or 'active'")
        return v


class OnboardingStatus(BaseModel):
    """Whether this project may receive routed invoices, and why not."""

    onboarded: bool
    blockers: list[str] = []
    mix_design_rows: int = 0
    mix_design_rows_mapped: int = 0


class ProjectOut(BaseModel):
    id: str
    project_no: str
    name: str
    bt_job_id: Optional[str] = None
    address: Optional[str] = None
    pe_user_id: Optional[str] = None
    pe_name: Optional[str] = None
    default_approver_id: Optional[str] = None
    default_approver_name: Optional[str] = None
    status: str
    drive_folder_name: Optional[str] = None
    has_concrete_supplier: bool = True
    expected_vendor_ids: list[str] = []
    onboarded_at: Optional[datetime] = None
    mix_design_pdf_path: Optional[str] = None
    quirks: dict = {}
    notes: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    onboarding: Optional[OnboardingStatus] = None
    invoice_count: int = 0


class ProjectDetail(ProjectOut):
    mix_designs: list[MixDesignRowOut] = []


class SignedUrlOut(BaseModel):
    url: str
    expires_in: int = Field(default=3600)
