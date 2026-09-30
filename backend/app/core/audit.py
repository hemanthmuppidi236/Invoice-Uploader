"""
Audit log service. Every state transition and every human field edit goes
through here (prompt §6, §7.4, §12).

Two entry points:
  - `log_transition(...)` for a status change — stamps from_status/to_status
  - `log_edit(...)`       for a field edit — stamps a field-level diff

Conventions:
  - entity_type matches the table name, singular: 'invoice', 'project',
    'mix_design', 'cost_code', 'vendor', 'app_user'
  - action is a past-tense verb: 'ingested', 'suggested', 'assigned',
    'reviewed', 'approved', 'rejected', 'uploaded', 'filed', 'flagged',
    'unflagged', 'voided', 'updated', 'onboarded'

Failures are logged and swallowed. Audit logging must never break a user's
request — but it must also never silently vanish, so a failure is logged at
ERROR with the payload so it can be reconstructed from Render logs.
"""

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from .auth import Actor
from .supabase_client import get_service_client

log = logging.getLogger(__name__)


def record(
    *,
    entity_type: str,
    entity_id: str,
    action: str,
    actor: Optional[Actor] = None,
    invoice_id: Optional[str] = None,
    from_status: Optional[str] = None,
    to_status: Optional[str] = None,
    diff: Optional[dict] = None,
) -> None:
    """Insert one audit_log row."""
    payload = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "invoice_id": invoice_id or (entity_id if entity_type == "invoice" else None),
        "actor_id": actor.user_id if actor else None,
        "actor_label": actor.label if actor else None,
        "action": action,
        "from_status": from_status,
        "to_status": to_status,
        "diff": _coerce(diff),
    }
    try:
        get_service_client().table("audit_log").insert(payload).execute()
    except Exception as e:
        log.error("audit_log insert failed (%s): %s", e, payload)


def log_transition(
    *,
    invoice_id: str,
    action: str,
    from_status: str,
    to_status: str,
    actor: Actor,
    diff: Optional[dict] = None,
) -> None:
    """Record an invoice status transition."""
    record(
        entity_type="invoice",
        entity_id=invoice_id,
        action=action,
        actor=actor,
        invoice_id=invoice_id,
        from_status=from_status,
        to_status=to_status,
        diff=diff,
    )


def log_edit(
    *,
    entity_type: str,
    entity_id: str,
    before: dict,
    after: dict,
    actor: Actor,
    invoice_id: Optional[str] = None,
    action: str = "updated",
) -> Optional[dict]:
    """Record a field-level edit, diffed.

    Returns the computed diff (or None when nothing actually changed, in which
    case no row is written). Prompt §7.4 wants these diffs specifically so we
    can later measure how often the AI was right — an edit that changed
    nothing would pollute that measurement.
    """
    diff = field_diff(before, after)
    if not diff:
        return None
    record(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor=actor,
        invoice_id=invoice_id,
        diff=diff,
    )
    return diff


def field_diff(before: dict, after: dict) -> dict:
    """Compute a {field: {from, to}} diff over the keys present in `after`.

    Only keys the caller actually submitted are compared, so a PATCH that
    omits a field never shows up as a change to NULL.
    """
    out: dict[str, dict[str, Any]] = {}
    for key, new_value in after.items():
        old_value = before.get(key)
        if _normalize(old_value) != _normalize(new_value):
            out[key] = {"from": _jsonable(old_value), "to": _jsonable(new_value)}
    return out


def _normalize(v: Any) -> Any:
    """Compare values the way a reviewer would perceive them.

    Decimal('100.00') and 100.0 are the same amount; '' and None are both
    empty. Without this, re-saving an unchanged form writes phantom diffs.
    """
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, str):
        return v.strip()
    return v


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _coerce(d: Optional[dict]) -> Optional[dict]:
    """Make a diff JSON-serializable for JSONB storage."""
    if d is None:
        return None
    return {k: _jsonable(v) if not isinstance(v, dict) else _coerce(v) for k, v in d.items()}
