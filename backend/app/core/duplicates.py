"""
Duplicate detection (prompt §7.2, hardened in Phase 4).

The cost of the two errors is wildly asymmetric, and that asymmetry decides
every judgement call in here.

A **missed** duplicate is a vendor paid twice. It does not surface as an
error; it surfaces weeks later in a reconciliation, or not at all.

A **false** duplicate is one invoice sitting in `/flagged` until Linda looks
at it, presses "not a duplicate", and moves on. Thirty seconds.

So this errs toward flagging. What it must not do is flag so loosely that the
queue fills with noise, because a queue nobody trusts is a queue nobody
reads — which converts the cheap error back into the expensive one. The
resolution is to make every match carry its *reason*, so the person deciding
can see what fired and judge it in a glance rather than opening two PDFs.

The original Phase 1 version matched on exact equality: same vendor and
invoice number, or same vendor, amount and date. Both are defeated by the
most ordinary thing that happens to these documents — a re-scan. OCR reads
`2294.25` as `2294.26`, or `278461-1` as `2784611`, and the second copy sails
through as a new payable.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from .supabase_client import get_service_client

log = logging.getLogger(__name__)

# What counts as "the same amount, read twice".
#
# The failure being modelled is an OCR misread in the cents and dimes — the
# documented case is 2294.25 coming back as 2294.26. A misread cannot move
# the total by more than 99 cents without also changing a dollar digit, and a
# dollar-digit error is large enough that the invoice number check is what
# should catch it instead. So: a dollar, scaling very slightly for large
# invoices where there are simply more digits to misread.
#
# Erring loose here is cheap. An amount match alone scores 2 and never flags
# on its own — it needs the invoice number or the date as well — so a
# too-generous tolerance costs a second signal, not a false flag.
ABSOLUTE_TOLERANCE = Decimal("1.00")
RELATIVE_TOLERANCE = Decimal("0.0005")

# Vendors date re-issues a day or two apart, and a scan can misread the day.
DATE_WINDOW_DAYS = 3

# Statuses that mean "this record is settled and should not match". Voiding
# is how a real duplicate gets resolved, so matching against voided records
# would make the flag permanent and unclearable.
SETTLED = ("void",)


@dataclass
class DuplicateMatch:
    """A candidate, and why it matched.

    `reasons` is the part that matters. "Possible duplicate" alone sends
    someone to open two PDFs; "same invoice number, and the amount is 1 cent
    apart" is usually decidable without opening either.
    """

    invoice: dict
    score: int
    reasons: list[str]

    @property
    def is_strong(self) -> bool:
        """Confident enough to flag on its own."""
        return self.score >= 3


def amounts_match(a: Optional[Decimal], b: Optional[Decimal]) -> bool:
    """Within OCR tolerance, not exactly equal.

    Signs must agree: a $500 credit memo and a $500 invoice are opposites,
    not near-duplicates, and SOP §5 has White Cap issuing both.
    """
    if a is None or b is None:
        return False
    if (a < 0) != (b < 0):
        return False
    gap = abs(a - b)
    allowed = max(ABSOLUTE_TOLERANCE, abs(a) * RELATIVE_TOLERANCE)
    return gap <= allowed


def normalize_invoice_no(value: Optional[str]) -> Optional[str]:
    """Strip a vendor's formatting down to the digits that identify the bill.

    `278461-1`, `278461 1`, and `INV-278461` are the same invoice wearing
    three different coats. Comparing the raw strings says they are three
    invoices.
    """
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    return digits or None


def invoice_numbers_match(a: Optional[str], b: Optional[str]) -> bool:
    """Equal after normalisation, or one contained in the other.

    Containment catches the suffix case — `278461` against `278461-1` — which
    SOP §4 shows is how this vendor numbers revisions. Guarded by a length
    floor: short numbers are contained in each other by coincidence, and
    `12` matching `1123` would flag half the queue.
    """
    na, nb = normalize_invoice_no(a), normalize_invoice_no(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if min(len(na), len(nb)) < 5:
        return False
    return na in nb or nb in na


def dates_near(a, b, *, days: int = DATE_WINDOW_DAYS) -> bool:
    a, b = _as_date(a), _as_date(b)
    if a is None or b is None:
        return False
    return abs((a - b).days) <= days


def _as_date(value) -> Optional[date]:
    if value is None or isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def score_candidate(
    *,
    candidate: dict,
    invoice_no: Optional[str],
    amount: Optional[Decimal],
    invoice_date,
    pdf_sha256: Optional[str] = None,
) -> DuplicateMatch:
    """How much this candidate looks like the invoice being checked.

    Weighted by how hard each signal is to produce by coincidence. Two
    invoices from one vendor sharing an amount and a date happen; two sharing
    an invoice number essentially do not.
    """
    score = 0
    reasons: list[str] = []

    # Byte-identical PDF. Not evidence, proof — the same file was ingested
    # twice, however it got here.
    if pdf_sha256 and candidate.get("pdf_sha256") == pdf_sha256:
        return DuplicateMatch(
            invoice=candidate,
            score=10,
            reasons=["the PDF is byte-for-byte the same file"],
        )

    if invoice_numbers_match(invoice_no, candidate.get("invoice_no")):
        score += 3
        if normalize_invoice_no(invoice_no) == normalize_invoice_no(
            candidate.get("invoice_no")
        ):
            reasons.append(f"same invoice number ({candidate.get('invoice_no')})")
        else:
            reasons.append(
                f"invoice number {candidate.get('invoice_no')!r} is the same "
                "bill with a different suffix"
            )

    candidate_amount = (
        Decimal(str(candidate["amount"])) if candidate.get("amount") is not None else None
    )
    if amount is not None and candidate_amount is not None:
        if amount == candidate_amount:
            score += 2
            reasons.append(f"same amount (${amount:,.2f})")
        elif amounts_match(amount, candidate_amount):
            score += 2
            reasons.append(
                f"amount is ${abs(amount - candidate_amount):,.2f} apart "
                f"(${amount:,.2f} vs ${candidate_amount:,.2f}) — within what "
                "a re-scan changes"
            )

    if invoice_date and candidate.get("invoice_date"):
        if _as_date(invoice_date) == _as_date(candidate["invoice_date"]):
            score += 1
            reasons.append("same invoice date")
        elif dates_near(invoice_date, candidate["invoice_date"]):
            score += 1
            reasons.append(
                f"dated within {DATE_WINDOW_DAYS} days "
                f"({str(candidate['invoice_date'])[:10]})"
            )

    return DuplicateMatch(invoice=candidate, score=score, reasons=reasons)


def find_duplicate(
    *,
    invoice_id: str,
    vendor_id: Optional[str],
    invoice_no: Optional[str],
    amount: Optional[Decimal],
    invoice_date,
    pdf_sha256: Optional[str] = None,
) -> Optional[DuplicateMatch]:
    """The best duplicate candidate for this invoice, or None.

    Scoped to one vendor. Two vendors legitimately use the same invoice
    numbers — most start at 1 — so comparing across them would flag
    constantly and teach everyone to ignore the flag.
    """
    if not vendor_id:
        return None

    # One query, then score in memory. The alternative is a query per rule,
    # which is three round trips and makes a combined rule ("number matches
    # AND amount is close") impossible to express.
    window_start = None
    if invoice_date:
        anchor = _as_date(invoice_date)
        if anchor:
            # Wide enough to catch a re-scan dated a few days out, narrow
            # enough that an annual repeat of the same round number does not
            # get dragged in.
            window_start = anchor - timedelta(days=90)

    query = (
        get_service_client()
        .table("invoices")
        .select(
            "id,invoice_no,amount,invoice_date,status,created_at,pdf_sha256,"
            "source_file_id,source_page"
        )
        .eq("vendor_id", vendor_id)
        .neq("id", invoice_id)
    )
    for settled in SETTLED:
        query = query.neq("status", settled)
    if window_start:
        query = query.gte("invoice_date", str(window_start))

    candidates = query.limit(500).execute().data or []
    if not candidates:
        return None

    scored = [
        score_candidate(
            candidate=c,
            invoice_no=invoice_no,
            amount=amount,
            invoice_date=invoice_date,
            pdf_sha256=pdf_sha256,
        )
        for c in candidates
    ]
    # Highest score wins; ties go to the oldest record, which is the one the
    # new invoice would be duplicating rather than the other way round.
    scored.sort(key=lambda m: (-m.score, str(m.invoice.get("created_at") or "")))

    best = scored[0]
    if not best.is_strong:
        return None

    log.info(
        "duplicate candidate for %s: %s (score %d)",
        invoice_id,
        best.invoice["id"],
        best.score,
    )
    return best


def describe(match: DuplicateMatch) -> str:
    """The flag detail a person reads in `/flagged`.

    Leads with what matched, because that is what decides it. A reviewer who
    can see "same invoice number, same amount" does not need to open
    anything."""
    other = match.invoice
    reasons = "; ".join(match.reasons) or "the vendor, amount and date line up"
    return (
        f"Looks like invoice {other.get('invoice_no') or '(no number)'}, "
        f"already recorded on {str(other.get('invoice_date'))[:10]} for "
        f"${Decimal(str(other.get('amount') or 0)):,.2f} and currently "
        f"{other.get('status')}. Matched because {reasons}. "
        "Open both; if they are genuinely different bills, mark not a "
        "duplicate."
    )
