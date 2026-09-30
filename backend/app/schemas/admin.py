"""
Reference-data schemas for /admin (prompt §10).

Everything here exists so the §15 kickoff data is data entry rather than a
code change: cost codes, vendors, users and their roles, and the end-of-day
distribution list are all editable from the app.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, field_validator

from ..core.auth import ALL_ROLES


# ─── Cost codes ───────────────────────────────────────────────────────


class CostCodeOut(BaseModel):
    id: str
    code: str
    base_code: Optional[str] = None
    category: Optional[str] = None
    description: str
    element_keywords: list[str] = []
    active: bool = True


class CostCodeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    base_code: Optional[str] = None
    category: Optional[str] = None
    description: str
    element_keywords: list[str] = []
    active: bool = True


class CostCodeUpdate(BaseModel):
    """`code` is intentionally absent.

    invoice_costs rows and mix_designs rows point at a cost code by id, and
    the code string is what Chrome types into BuilderTrend. Renaming one in
    place would silently change what a past approval meant, so a BuilderTrend
    rename is handled by deactivating the old code and adding the new one.
    """

    model_config = ConfigDict(extra="forbid")

    base_code: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    element_keywords: Optional[list[str]] = None
    active: Optional[bool] = None


# ─── Vendors ──────────────────────────────────────────────────────────


class VendorOut(BaseModel):
    id: str
    invoice_name: str
    bt_name: str
    drive_folder_name: Optional[str] = None
    default_cost_code_id: Optional[str] = None
    default_cost_code: Optional[str] = None      # joined display value
    is_concrete_supplier: bool = False
    aliases: list[str] = []
    notes: Optional[str] = None
    active: bool = True


class VendorCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_name: str
    bt_name: str
    drive_folder_name: Optional[str] = None
    default_cost_code_id: Optional[str] = None
    is_concrete_supplier: bool = False
    aliases: list[str] = []
    notes: Optional[str] = None
    active: bool = True

    @field_validator("invoice_name", "bt_name")
    @classmethod
    def _required(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("required")
        return v

    @field_validator("bt_name")
    @classmethod
    def _not_do_not_select(cls, v: str) -> str:
        # SOP §6: never pick a BuilderTrend vendor labeled "** DO NOT SELECT **".
        # Catching it here stops a bad mapping from ever reaching a session.
        if "DO NOT SELECT" in v.upper():
            raise ValueError(
                "SOP §6: never map to a BuilderTrend vendor marked "
                "'** DO NOT SELECT **'."
            )
        return v


class VendorUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_name: Optional[str] = None
    bt_name: Optional[str] = None
    drive_folder_name: Optional[str] = None
    default_cost_code_id: Optional[str] = None
    is_concrete_supplier: Optional[bool] = None
    aliases: Optional[list[str]] = None
    notes: Optional[str] = None
    active: Optional[bool] = None

    @field_validator("bt_name")
    @classmethod
    def _not_do_not_select(cls, v: Optional[str]) -> Optional[str]:
        if v and "DO NOT SELECT" in v.upper():
            raise ValueError(
                "SOP §6: never map to a BuilderTrend vendor marked "
                "'** DO NOT SELECT **'."
            )
        return v


# ─── Users ────────────────────────────────────────────────────────────


class UserOut(BaseModel):
    id: str
    email: str
    name: Optional[str] = None
    roles: list[str] = []
    deactivated_at: Optional[datetime] = None
    last_login_at: Optional[datetime] = None


class UserCreate(BaseModel):
    """Pre-seed a person before their first sign-in.

    The auth.users trigger claims this row by email on first Google sign-in
    and re-points it at the real auth id, keeping the roles granted here.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    name: Optional[str] = None
    roles: list[str] = ["viewer"]

    @field_validator("roles")
    @classmethod
    def _roles_valid(cls, v: list[str]) -> list[str]:
        return _check_roles(v)


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    roles: Optional[list[str]] = None
    deactivated: Optional[bool] = None

    @field_validator("roles")
    @classmethod
    def _roles_valid(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        return _check_roles(v) if v is not None else None


def _check_roles(v: list[str]) -> list[str]:
    cleaned = [r.strip() for r in v if r and r.strip()]
    unknown = [r for r in cleaned if r not in ALL_ROLES]
    if unknown:
        raise ValueError(
            f"Unknown role(s): {', '.join(unknown)}. Valid: {', '.join(ALL_ROLES)}"
        )
    if not cleaned:
        raise ValueError("A user needs at least one role (use 'viewer' for read-only).")
    # Dedupe while preserving order so the stored array is stable.
    return list(dict.fromkeys(cleaned))


# ─── Distribution list ────────────────────────────────────────────────


class DistributionOut(BaseModel):
    id: str
    email: str
    name: Optional[str] = None
    active: bool = True


class DistributionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    name: Optional[str] = None
    active: bool = True


class DistributionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    active: Optional[bool] = None
