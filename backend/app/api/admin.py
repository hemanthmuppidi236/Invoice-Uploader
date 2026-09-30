"""
Reference data API (prompt §10 /admin).

Role split follows §3: `accountant` manages reference data (cost codes,
vendors, distribution list); `admin` additionally manages users. Reads need
only authentication, so a PE can populate the reviewer/approver dropdowns and
the cost code picker without write access.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status

from ..core import audit
from ..core.auth import Actor, CurrentUser, as_actor, get_current_user, require_role
from ..core.supabase_client import get_service_client
from ..schemas.admin import (
    CostCodeCreate,
    CostCodeOut,
    CostCodeUpdate,
    DistributionCreate,
    DistributionOut,
    DistributionUpdate,
    UserCreate,
    UserOut,
    UserUpdate,
    VendorCreate,
    VendorOut,
    VendorUpdate,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

REF_DATA_ROLES = ("admin", "accountant")


def _sb():
    return get_service_client()


def _cost_code_labels() -> dict[str, str]:
    res = _sb().table("cost_codes").select("id,code").execute()
    return {r["id"]: r["code"] for r in (res.data or [])}


def _user_out(row: dict) -> UserOut:
    return UserOut(
        id=row["id"],
        email=row["email"],
        name=row.get("name"),
        roles=list(row.get("role") or []),
        deactivated_at=row.get("deactivated_at"),
        last_login_at=row.get("last_login_at"),
    )


# ─── Cost codes ───────────────────────────────────────────────────────


@router.get("/cost-codes", response_model=list[CostCodeOut])
def list_cost_codes(
    active_only: bool = True,
    search: Optional[str] = None,
    user: CurrentUser = Depends(get_current_user),
):
    q = _sb().table("cost_codes").select("*")
    if active_only:
        q = q.eq("active", True)
    if search:
        q = q.ilike("code", f"%{search}%")
    return (q.order("code").execute().data) or []


@router.post(
    "/cost-codes", response_model=CostCodeOut, status_code=status.HTTP_201_CREATED
)
def create_cost_code(
    payload: CostCodeCreate,
    actor: Actor = Depends(as_actor),
    _: CurrentUser = Depends(require_role(*REF_DATA_ROLES)),
):
    """Add a cost code that already exists in BuilderTrend.

    This does not create anything in BuilderTrend — prompt §12 forbids that.
    It records a code someone added there so the app can select it.
    """
    existing = (
        _sb()
        .table("cost_codes")
        .select("id")
        .eq("code", payload.code)
        .limit(1)
        .execute()
    )
    if existing.data:
        raise HTTPException(
            status_code=409, detail=f"Cost code {payload.code!r} already exists."
        )

    created = _sb().table("cost_codes").insert(payload.model_dump()).execute()
    if not created.data:
        raise HTTPException(status_code=502, detail="Insert returned no row.")

    audit.record(
        entity_type="cost_code",
        entity_id=created.data[0]["id"],
        action="created",
        actor=actor,
        diff={"code": payload.code},
    )
    return created.data[0]


@router.patch("/cost-codes/{code_id}", response_model=CostCodeOut)
def update_cost_code(
    code_id: str,
    payload: CostCodeUpdate,
    actor: Actor = Depends(as_actor),
    _: CurrentUser = Depends(require_role(*REF_DATA_ROLES)),
):
    before = _sb().table("cost_codes").select("*").eq("id", code_id).limit(1).execute()
    if not before.data:
        raise HTTPException(status_code=404, detail="Cost code not found")

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="No fields to update.")

    updated = _sb().table("cost_codes").update(changes).eq("id", code_id).execute()
    audit.log_edit(
        entity_type="cost_code",
        entity_id=code_id,
        before=before.data[0],
        after=changes,
        actor=actor,
    )
    return updated.data[0]


# ─── Vendors ──────────────────────────────────────────────────────────


@router.get("/vendors", response_model=list[VendorOut])
def list_vendors(
    active_only: bool = True,
    user: CurrentUser = Depends(get_current_user),
):
    q = _sb().table("vendors").select("*")
    if active_only:
        q = q.eq("active", True)
    rows = (q.order("invoice_name").execute().data) or []
    labels = _cost_code_labels()
    return [
        VendorOut(
            **{k: r.get(k) for k in VendorOut.model_fields if k in r},
            default_cost_code=labels.get(r.get("default_cost_code_id") or ""),
        )
        for r in rows
    ]


@router.post("/vendors", response_model=VendorOut, status_code=status.HTTP_201_CREATED)
def create_vendor(
    payload: VendorCreate,
    actor: Actor = Depends(as_actor),
    _: CurrentUser = Depends(require_role(*REF_DATA_ROLES)),
):
    """Record a vendor that already exists in BuilderTrend.

    Prompt §12 and SOP §6 both forbid creating a Sub/Vendor from the app or
    the agent. `bt_name` must be the record as BuilderTrend already spells it.
    """
    existing = (
        _sb()
        .table("vendors")
        .select("id")
        .eq("invoice_name", payload.invoice_name)
        .limit(1)
        .execute()
    )
    if existing.data:
        raise HTTPException(
            status_code=409,
            detail=f"Vendor {payload.invoice_name!r} already exists.",
        )

    if payload.default_cost_code_id:
        _require_cost_code(payload.default_cost_code_id)

    created = _sb().table("vendors").insert(payload.model_dump()).execute()
    if not created.data:
        raise HTTPException(status_code=502, detail="Insert returned no row.")

    audit.record(
        entity_type="vendor",
        entity_id=created.data[0]["id"],
        action="created",
        actor=actor,
        diff={"invoice_name": payload.invoice_name, "bt_name": payload.bt_name},
    )
    labels = _cost_code_labels()
    row = created.data[0]
    return VendorOut(
        **{k: row.get(k) for k in VendorOut.model_fields if k in row},
        default_cost_code=labels.get(row.get("default_cost_code_id") or ""),
    )


@router.patch("/vendors/{vendor_id}", response_model=VendorOut)
def update_vendor(
    vendor_id: str,
    payload: VendorUpdate,
    actor: Actor = Depends(as_actor),
    _: CurrentUser = Depends(require_role(*REF_DATA_ROLES)),
):
    before = _sb().table("vendors").select("*").eq("id", vendor_id).limit(1).execute()
    if not before.data:
        raise HTTPException(status_code=404, detail="Vendor not found")

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="No fields to update.")

    if changes.get("default_cost_code_id"):
        _require_cost_code(changes["default_cost_code_id"])

    if "invoice_name" in changes:
        clash = (
            _sb()
            .table("vendors")
            .select("id")
            .eq("invoice_name", changes["invoice_name"])
            .neq("id", vendor_id)
            .limit(1)
            .execute()
        )
        if clash.data:
            raise HTTPException(
                status_code=409,
                detail=f"Vendor {changes['invoice_name']!r} already exists.",
            )

    updated = _sb().table("vendors").update(changes).eq("id", vendor_id).execute()
    audit.log_edit(
        entity_type="vendor",
        entity_id=vendor_id,
        before=before.data[0],
        after=changes,
        actor=actor,
    )
    labels = _cost_code_labels()
    row = updated.data[0]
    return VendorOut(
        **{k: row.get(k) for k in VendorOut.model_fields if k in row},
        default_cost_code=labels.get(row.get("default_cost_code_id") or ""),
    )


def _require_cost_code(cost_code_id: str) -> None:
    res = (
        _sb()
        .table("cost_codes")
        .select("id,active")
        .eq("id", cost_code_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        raise HTTPException(
            status_code=422, detail=f"Unknown cost_code_id: {cost_code_id}"
        )
    if not res.data[0].get("active"):
        raise HTTPException(
            status_code=422,
            detail="That cost code is inactive and cannot be a vendor default.",
        )


# ─── Users ────────────────────────────────────────────────────────────


@router.get("/users", response_model=list[UserOut])
def list_users(
    include_deactivated: bool = False,
    user: CurrentUser = Depends(get_current_user),
):
    """The roster. Readable by any signed-in user, because the review and
    approval screens need reviewer/approver dropdowns."""
    q = _sb().table("app_users").select("*")
    if not include_deactivated:
        q = q.is_("deactivated_at", "null")
    rows = (q.order("email").execute().data) or []
    return [_user_out(r) for r in rows]


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    actor: Actor = Depends(as_actor),
    _: CurrentUser = Depends(require_role("admin")),
):
    """Pre-seed a person with roles before their first sign-in."""
    email = str(payload.email).lower()
    existing = (
        _sb().table("app_users").select("id").eq("email", email).limit(1).execute()
    )
    if existing.data:
        raise HTTPException(status_code=409, detail=f"{email} already exists.")

    # uuid_generate_v4() is the column default in Postgres, but supabase-py
    # inserts an explicit payload, so generate the placeholder id here. The
    # auth.users trigger re-points it on first sign-in.
    created = (
        _sb()
        .table("app_users")
        .insert(
            {
                "id": str(uuid.uuid4()),
                "email": email,
                "name": payload.name,
                "role": payload.roles,
            }
        )
        .execute()
    )
    if not created.data:
        raise HTTPException(status_code=502, detail="Insert returned no row.")

    audit.record(
        entity_type="app_user",
        entity_id=created.data[0]["id"],
        action="created",
        actor=actor,
        diff={"email": email, "roles": payload.roles},
    )
    return _user_out(created.data[0])


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: str,
    payload: UserUpdate,
    actor: Actor = Depends(as_actor),
    admin: CurrentUser = Depends(require_role("admin")),
):
    before = _sb().table("app_users").select("*").eq("id", user_id).limit(1).execute()
    if not before.data:
        raise HTTPException(status_code=404, detail="User not found")
    row = before.data[0]

    changes: dict = {}
    if payload.name is not None:
        changes["name"] = payload.name
    if payload.roles is not None:
        changes["role"] = payload.roles
    if payload.deactivated is not None:
        changes["deactivated_at"] = (
            datetime.now(timezone.utc).isoformat() if payload.deactivated else None
        )

    if not changes:
        raise HTTPException(status_code=422, detail="No fields to update.")

    # Guard against locking the app out of its own user management: the last
    # active admin cannot drop their own admin role or deactivate themselves.
    losing_admin = "role" in changes and "admin" not in changes["role"]
    self_deactivating = changes.get("deactivated_at") and user_id == admin.id
    if (losing_admin and user_id == admin.id) or self_deactivating:
        others = (
            _sb()
            .table("app_users")
            .select("id")
            .contains("role", ["admin"])
            .is_("deactivated_at", "null")
            .neq("id", user_id)
            .execute()
        )
        if not others.data:
            raise HTTPException(
                status_code=409,
                detail=(
                    "You are the only active admin. Grant admin to someone "
                    "else before removing your own access."
                ),
            )

    updated = _sb().table("app_users").update(changes).eq("id", user_id).execute()
    audit.log_edit(
        entity_type="app_user",
        entity_id=user_id,
        before=row,
        after=changes,
        actor=actor,
    )
    return _user_out(updated.data[0])


# ─── Distribution list ────────────────────────────────────────────────


@router.get("/distribution-list", response_model=list[DistributionOut])
def list_distribution(
    active_only: bool = True,
    user: CurrentUser = Depends(get_current_user),
):
    q = _sb().table("distribution_list").select("*")
    if active_only:
        q = q.eq("active", True)
    return (q.order("email").execute().data) or []


@router.post(
    "/distribution-list",
    response_model=DistributionOut,
    status_code=status.HTTP_201_CREATED,
)
def add_distribution(
    payload: DistributionCreate,
    actor: Actor = Depends(as_actor),
    _: CurrentUser = Depends(require_role(*REF_DATA_ROLES)),
):
    email = str(payload.email).lower()
    existing = (
        _sb()
        .table("distribution_list")
        .select("id")
        .eq("email", email)
        .limit(1)
        .execute()
    )
    if existing.data:
        raise HTTPException(status_code=409, detail=f"{email} is already on the list.")

    created = (
        _sb()
        .table("distribution_list")
        .insert({"email": email, "name": payload.name, "active": payload.active})
        .execute()
    )
    audit.record(
        entity_type="distribution_list",
        entity_id=created.data[0]["id"],
        action="created",
        actor=actor,
        diff={"email": email},
    )
    return created.data[0]


@router.patch("/distribution-list/{entry_id}", response_model=DistributionOut)
def update_distribution(
    entry_id: str,
    payload: DistributionUpdate,
    actor: Actor = Depends(as_actor),
    _: CurrentUser = Depends(require_role(*REF_DATA_ROLES)),
):
    before = (
        _sb()
        .table("distribution_list")
        .select("*")
        .eq("id", entry_id)
        .limit(1)
        .execute()
    )
    if not before.data:
        raise HTTPException(status_code=404, detail="Entry not found")

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="No fields to update.")

    updated = (
        _sb().table("distribution_list").update(changes).eq("id", entry_id).execute()
    )
    audit.log_edit(
        entity_type="distribution_list",
        entity_id=entry_id,
        before=before.data[0],
        after=changes,
        actor=actor,
    )
    return updated.data[0]


@router.delete("/distribution-list/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_distribution(
    entry_id: str,
    actor: Actor = Depends(as_actor),
    _: CurrentUser = Depends(require_role(*REF_DATA_ROLES)),
):
    before = (
        _sb()
        .table("distribution_list")
        .select("*")
        .eq("id", entry_id)
        .limit(1)
        .execute()
    )
    if not before.data:
        raise HTTPException(status_code=404, detail="Entry not found")

    _sb().table("distribution_list").delete().eq("id", entry_id).execute()
    audit.record(
        entity_type="distribution_list",
        entity_id=entry_id,
        action="deleted",
        actor=actor,
        diff={"email": before.data[0].get("email")},
    )
    return None
