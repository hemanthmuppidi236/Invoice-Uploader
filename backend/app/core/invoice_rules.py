"""
Invoice field rules, copied verbatim from the BuilderTrend SOP.

Pure functions with no I/O, because these are the rules most likely to be
argued about later and they need to be readable and testable on their own.
Every one cites the SOP section it comes from.

The AI is asked to extract what the invoice *says*. These functions derive
what BuilderTrend *needs* from that. Keeping the two apart matters: if the
model returns a due date it read off the vendor's terms, we ignore it, because
SOP §4 says Ferrocrete's due date is always end of the month after the invoice
date regardless of what the vendor printed.
"""

import calendar
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

# SOP §5: White Cap yard invoices are never entered. Matched case-insensitively
# against the invoice's job-name text.
YARD_MARKERS = (
    "el cajon yard",
    "sd yard",
    "san diego yard",
)

# SOP §6: never select a BuilderTrend vendor record with this marker.
DO_NOT_SELECT = "do not select"


# ─── Due date (SOP §4) ────────────────────────────────────────────────


def due_date_for(invoice_date: date) -> date:
    """End of the month AFTER the invoice date. Vendor terms are ignored.

    SOP §4: "End of the month after the invoice date (9/2 → 10/31). Ignore
    vendor terms."

    December rolls to January of the next year, which is the case a naive
    `month + 1` gets wrong.
    """
    year = invoice_date.year + (1 if invoice_date.month == 12 else 0)
    month = 1 if invoice_date.month == 12 else invoice_date.month + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, last_day)


# ─── Bill number (SOP §4) ─────────────────────────────────────────────


def bill_no_for(invoice_no: Optional[str]) -> Optional[str]:
    """Last 4 digits of the invoice number, suffixes ignored.

    SOP §4: "Last 4 digits of invoice # (ignore suffixes: 278461-1 → 8461)".

    "Ignore suffixes" means the trailing `-1` is not part of the number, so we
    take the FIRST run of digits and use its last four — not the last four
    characters of the whole string, which would give "61-1", and not the last
    four digits overall, which would give "4611".

    Returns None when there are no digits at all, so the caller flags it
    rather than typing something wrong into BuilderTrend.
    """
    if not invoice_no:
        return None

    runs = re.findall(r"\d+", invoice_no)
    if not runs:
        return None

    # The invoice number proper is the FIRST digit run, per the SOP's worked
    # example. A vendor prefix like "INV-278461-1" still resolves to 278461
    # because letters break the run.
    primary = runs[0]

    # Two narrow exceptions where the first run is plainly not the number.
    # Both only ever promote a LATER, LONGER run, so the documented
    # "278461-1 → 8461" case is untouched.
    if len(runs) > 1:
        longer = max(runs[1:], key=len)
        # A leading four-digit year is a date prefix: "2026-278461".
        looks_like_year = bool(re.fullmatch(r"(19|20)\d{2}", primary))
        # A one-to-three digit lead is a branch or division code: "9-278461".
        too_short = len(primary) < 4
        if (looks_like_year or too_short) and len(longer) > len(primary):
            primary = longer

    return primary[-4:]


# ─── Yard invoices (SOP §5) ───────────────────────────────────────────


def is_yard_invoice(*text_fields: Optional[str]) -> bool:
    """True when any field names a White Cap yard rather than a job.

    SOP §5: yard invoices are skipped, not entered. Getting this wrong in
    either direction is bad — entering one puts a bill on the wrong job, and
    skipping a real job invoice loses a payable — so the match is against
    specific known yard names, not a generic "yard" substring. A job legitimately
    called "Yard Street Lofts" must not trip it.
    """
    haystack = " ".join(t.lower() for t in text_fields if t)
    return any(marker in haystack for marker in YARD_MARKERS)


def is_do_not_select(bt_name: Optional[str]) -> bool:
    """True for a BuilderTrend vendor record marked '** DO NOT SELECT **'."""
    return bool(bt_name) and DO_NOT_SELECT in bt_name.lower()


# ─── Money ────────────────────────────────────────────────────────────

CENT = Decimal("0.01")


def to_money(value) -> Optional[Decimal]:
    """Coerce a model-supplied number to 2dp Decimal, or None if unusable.

    The AI returns JSON numbers, which arrive as float. Rounding here — once,
    on the way in — keeps every later comparison exact, so a split that sums
    correctly on the invoice does not fail the approval check on a float
    artifact.
    """
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return None


def signed_amount(amount: Optional[Decimal], is_credit: bool) -> Optional[Decimal]:
    """Normalize a credit memo to a negative amount.

    SOP §5: "Credit memos (negative, 'REFERENCE INVOICE#') → enter as a normal
    bill with the negative amount". Vendors are inconsistent about whether the
    printed total already carries the minus sign, so store the sign canonically
    here: a credit is always negative, a normal bill always positive.
    """
    if amount is None:
        return None
    magnitude = abs(amount)
    return -magnitude if is_credit else magnitude


def costs_balance(
    amount: Optional[Decimal], cost_amounts: list[Optional[Decimal]]
) -> tuple[bool, Decimal]:
    """Whether the cost split sums to the invoice total, and by how much it is off.

    Prompt §5 makes this a hard precondition for approval. Returns
    (balanced, difference) where difference is (sum of rows) - amount, so the
    UI can say "over by $12.50" rather than just "does not balance".
    """
    if amount is None:
        return False, Decimal("0.00")
    total = sum((c for c in cost_amounts if c is not None), Decimal("0.00"))
    total = total.quantize(CENT, rounding=ROUND_HALF_UP)
    diff = (total - amount).quantize(CENT, rounding=ROUND_HALF_UP)
    return diff == Decimal("0.00"), diff


def allocate_proportionally(
    total: Decimal, weights: list[Decimal]
) -> list[Decimal]:
    """Split `total` across `weights`, with the remainder on the largest share.

    Prompt §8: on a split invoice, "tax and fees allocated proportionally".
    Rounding each share independently loses or gains cents, and the result has
    to sum to the total exactly or approval is blocked — so the drift is put
    on the biggest row, where it is least visible as a percentage.
    """
    if not weights:
        return []
    weight_sum = sum(weights)
    if weight_sum == 0:
        # Degenerate but real: several zero-quantity lines. Split evenly.
        even = (total / len(weights)).quantize(CENT, rounding=ROUND_HALF_UP)
        shares = [even] * len(weights)
    else:
        shares = [
            (total * w / weight_sum).quantize(CENT, rounding=ROUND_HALF_UP)
            for w in weights
        ]

    drift = total - sum(shares)
    if drift != 0:
        largest = max(range(len(shares)), key=lambda i: abs(shares[i]))
        shares[largest] += drift
    return shares


# ─── Confidence banding (prompt §8) ───────────────────────────────────


def confidence_band(
    confidence: Optional[float], high: float = 0.85, low: float = 0.60
) -> str:
    """'high' | 'medium' | 'low' | 'none'. Thresholds come from config."""
    if confidence is None:
        return "none"
    if confidence >= high:
        return "high"
    if confidence >= low:
        return "medium"
    return "low"


# ─── Pour sequence (prompt §8 phase inference) ────────────────────────

# The order concrete goes in on a building. Used to infer the current phase
# from what was most recently approved on the same project, and prefer the
# element that comes next when one mix serves several.
POUR_SEQUENCE: list[tuple[str, tuple[str, ...]]] = [
    ("foundations", ("footing", "pile", "caisson", "grade beam", "pier", "cap")),
    ("subgrade", ("rat slab", "mud slab", "mat slab")),
    ("slab_or_basement", ("sog", "slab on grade", "basement wall", "retaining wall")),
    ("vertical", ("column", "shear wall", "wall", "core wall")),
    ("elevated", ("deck", "elevated slab", "podium")),
    ("finishes", ("topping slab", "curb", "pad", "sidewalk", "site concrete")),
]


def phase_of(element_or_description: str) -> Optional[str]:
    """Which pour phase a described element belongs to, if recognisable."""
    text = (element_or_description or "").lower()
    if not text:
        return None
    # Longest keyword first so "basement wall" is not claimed by "wall".
    best: Optional[tuple[int, str]] = None
    for phase, keywords in POUR_SEQUENCE:
        for kw in keywords:
            if kw in text and (best is None or len(kw) > best[0]):
                best = (len(kw), phase)
    return best[1] if best else None


def next_phases(current: Optional[str]) -> list[str]:
    """The phases that plausibly come next, current phase included first.

    A project does not finish one phase before starting the next — columns on
    level 2 pour while decks on level 1 cure — so the current phase stays a
    candidate rather than being ruled out.
    """
    names = [p for p, _ in POUR_SEQUENCE]
    if current is None or current not in names:
        return names
    idx = names.index(current)
    return names[idx:] + names[:idx]
