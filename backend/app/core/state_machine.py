"""
The invoice state machine (prompt §6).

    ingested ─ai─▶ suggested ─assign─▶ assigned ─reviewed─▶ pending_approval
                                          ▲                       │
                                          └──── reject ───────────┘
                                                                  │ approve
                                                                  ▼
                                      filed ◀─file─ uploaded ◀─upload─ approved

    any state ─▶ flagged    (needs a person)
    any state ─▶ void       (admin or accountant, with a reason)

One guard, used by every transition, rather than five near-identical copies.
§6 requires each transition to validate the current state (409 on mismatch),
stamp the actor and a UTC time, and write audit_log — so those three things
happen in exactly one place, and a new transition cannot forget one of them.

Preconditions live here too, next to the transition they gate. The important
one is §5: the cost split must sum to the invoice total before approval. It is
checked at BOTH mark-reviewed and approve. No current path can unbalance a
split in between — PATCH is refused once the invoice is past review — but
`approved` is the one state the Chrome session reads, so the invariant is
re-asserted at that gate rather than inherited from a check that ran earlier.

No transition sends an immediate email. §14: "Approvers get the same daily
digest as reviewers, no immediate pings." The two scheduled digests are the
only notifications, which is why there is no notify hook being called below.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional

from . import audit
from .auth import Actor
from .invoice_rules import costs_balance
from .supabase_client import get_service_client

log = logging.getLogger(__name__)


class TransitionError(Exception):
    """A transition was refused. Carries the HTTP status the caller should use.

    409 for a state mismatch — the invoice moved since the page was loaded.
    422 for a failed precondition — the invoice is in the right state but not
    ready, and the message says what is missing.
    403 for a permission problem specific to this record, as opposed to the
    role check the route already did.
    """

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Transition:
    """One edge of the state machine."""

    action: str
    from_states: tuple[str, ...]
    to_state: str
    # Returns an error message when the invoice is not ready, or None.
    precondition: Optional[Callable[[dict], Optional[str]]] = None


def _sb():
    return get_service_client()


# ─── Preconditions ────────────────────────────────────────────────────


def _cost_rows(invoice_id: str) -> list[dict]:
    return (
        _sb()
        .table("invoice_costs")
        .select("cost_code_id,amount")
        .eq("invoice_id", invoice_id)
        .execute()
        .data
        or []
    )


def require_balanced_costs(invoice: dict) -> Optional[str]:
    """§5: invoice_costs must sum to invoices.amount.

    Reported with the actual gap rather than a bare "does not balance", so
    the reviewer can tell a rounding artifact from a misread line.
    """
    amount = invoice.get("amount")
    if amount is None:
        return "The invoice amount is unknown, so the cost split cannot be checked."

    rows = _cost_rows(invoice["id"])
    if not rows:
        return "No cost code has been assigned yet."

    amount_dec = Decimal(str(amount))
    balanced, diff = costs_balance(
        amount_dec, [Decimal(str(r["amount"])) for r in rows]
    )
    if not balanced:
        direction = "over" if diff > 0 else "under"
        return (
            f"The cost rows are {direction} the invoice total by "
            f"${abs(diff):,.2f}. They have to sum to ${amount_dec:,.2f} exactly."
        )
    return None


def require_ready_for_review(invoice: dict) -> Optional[str]:
    """Everything BuilderTrend needs before a person certifies the invoice.

    This is the certification gate: past here, the next human decision is
    "approve", and after that the Chrome session types these values into
    BuilderTrend. A missing job or vendor at that point is a bill on the
    wrong record, so it is caught here while someone is still looking at it.
    """
    missing: list[str] = []
    if not invoice.get("project_id"):
        missing.append("a project")
    if not invoice.get("vendor_id"):
        missing.append("a vendor")
    if invoice.get("amount") is None:
        missing.append("an amount")
    if not invoice.get("invoice_date"):
        missing.append("an invoice date")
    if not invoice.get("invoice_no"):
        missing.append("an invoice number")

    if missing:
        return (
            "This invoice still needs " + _join(missing) + " before it can be "
            "marked reviewed."
        )

    if not invoice.get("approver_id"):
        return (
            "Pick an approver before marking this reviewed — otherwise nobody "
            "is asked to approve it."
        )

    return require_balanced_costs(invoice)


def require_reviewed(invoice: dict) -> Optional[str]:
    """§12: nothing reaches BuilderTrend without a human `reviewed` stamp."""
    if not invoice.get("reviewed_at"):
        return (
            "This invoice has not been reviewed. A person has to review it "
            "before it can be approved."
        )
    # Re-asserted rather than inherited. `approved` is the only state the
    # Chrome session reads, so this is the last gate before a number reaches
    # BuilderTrend, and the check costs one query.
    return require_balanced_costs(invoice)


def _join(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


# ─── The transitions ──────────────────────────────────────────────────

ASSIGN = Transition(
    action="assigned",
    # `flagged` is included because Linda routinely clears a flag by setting
    # the project or vendor and handing it straight to a reviewer; forcing an
    # unflag first would be a second click for no gain.
    from_states=("ingested", "suggested", "flagged"),
    to_state="assigned",
)

MARK_REVIEWED = Transition(
    action="reviewed",
    from_states=("assigned",),
    to_state="pending_approval",
    precondition=require_ready_for_review,
)

APPROVE = Transition(
    action="approved",
    from_states=("pending_approval",),
    to_state="approved",
    precondition=require_reviewed,
)

REJECT = Transition(
    action="rejected",
    from_states=("pending_approval",),
    to_state="assigned",
)

TRANSITIONS = {t.action: t for t in (ASSIGN, MARK_REVIEWED, APPROVE, REJECT)}


# ─── Applying a transition ────────────────────────────────────────────


def apply(
    *,
    invoice: dict,
    transition: Transition,
    actor: Actor,
    extra_fields: Optional[dict] = None,
    diff: Optional[dict] = None,
) -> dict:
    """Validate, stamp, write audit_log, and return the updated row.

    Raises TransitionError with the right HTTP status when the state does not
    match or a precondition fails. Nothing is written in either case.
    """
    invoice_id = invoice["id"]
    current = invoice["status"]

    if current not in transition.from_states:
        raise TransitionError(
            _mismatch_message(transition, current),
            status_code=409,
        )

    if transition.precondition:
        problem = transition.precondition(invoice)
        if problem:
            raise TransitionError(problem, status_code=422)

    now = datetime.now(timezone.utc).isoformat()
    update: dict = {"status": transition.to_state}
    update.update(_stamps(transition.action, now, actor))
    if extra_fields:
        update.update(extra_fields)

    result = _sb().table("invoices").update(update).eq("id", invoice_id).execute()
    if not result.data:
        raise TransitionError(
            "The update did not return a row. The invoice may have been "
            "deleted while you were working.",
            status_code=409,
        )

    audit.log_transition(
        invoice_id=invoice_id,
        action=transition.action,
        from_status=current,
        to_status=transition.to_state,
        actor=actor,
        diff=diff,
    )
    log.info(
        "invoice %s: %s → %s by %s",
        invoice_id,
        current,
        transition.to_state,
        actor.label,
    )
    return result.data[0]


def _stamps(action: str, now: str, actor: Actor) -> dict:
    """Which timestamp and actor column this action writes."""
    if action == "assigned":
        return {"assigned_at": now, "assigned_by": actor.user_id}
    if action == "reviewed":
        return {"reviewed_at": now}
    if action == "approved":
        return {"approved_at": now}
    if action == "rejected":
        # The review stamp is cleared: the invoice is going back for another
        # look, and leaving it set would let it be approved again without a
        # second review, since require_reviewed only checks the stamp exists.
        return {"rejected_at": now, "reviewed_at": None}
    return {}


def _mismatch_message(transition: Transition, current: str) -> str:
    """A 409 that says what actually happened, not just that it failed.

    A state mismatch almost always means someone else acted while this page
    was open, so the message names the current state and what to do.
    """
    expected = _join(list(transition.from_states))
    return (
        f"This invoice is {current}, but {transition.action.replace('_', ' ')} "
        f"needs it to be {expected}. Someone may have acted on it since you "
        "opened this page — reload to see where it is now."
    )


# ─── Record-level permission checks ───────────────────────────────────


def require_assigned_reviewer(invoice: dict, actor: Actor) -> None:
    """Only the assigned reviewer marks an invoice reviewed.

    Admins and accountants are allowed through: Linda manages the queue and is
    herself a reviewer, and an admin needs to be able to unstick anything. Any
    OTHER project engineer is refused — the audit trail has to record that the
    person who was asked is the person who certified it.
    """
    user = actor.user
    if user is None:
        raise TransitionError(
            "The Chrome session cannot review invoices. Nothing reaches "
            "BuilderTrend without a human review stamp.",
            status_code=403,
        )
    if user.is_admin or user.is_accountant:
        return
    if invoice.get("reviewer_id") == user.id:
        return
    raise TransitionError(
        "This invoice is assigned to someone else for review. Ask accounting "
        "to reassign it if it should be yours.",
        status_code=403,
    )


def require_assigned_approver(invoice: dict, actor: Actor) -> None:
    """Only the assigned approver approves or rejects.

    Admins are allowed through so nothing can get permanently stuck. Another
    approver is refused even though they hold the role: the queue is
    "pending MY approval", and letting any approver clear any invoice would
    make the approver field decorative and the audit trail misleading about
    who was actually asked. If the assigned approver is unavailable,
    accounting reassigns — which is recorded.
    """
    user = actor.user
    if user is None:
        raise TransitionError(
            "The Chrome session cannot approve invoices. §12 requires a human "
            "approval stamp before anything reaches BuilderTrend.",
            status_code=403,
        )
    if user.is_admin:
        return
    if not user.is_approver:
        raise TransitionError(
            "Approving requires the approver role.",
            status_code=403,
        )
    if invoice.get("approver_id") == user.id:
        return
    raise TransitionError(
        "This invoice is waiting on a different approver. Ask accounting to "
        "reassign it if you should be the one approving.",
        status_code=403,
    )
