"""
Did the AI get it right? (prompt §12)

"Every AI suggestion and every human edit is stored. Add a small `/metrics`
page showing AI acceptance rate by cost code and vendor."

The question underneath is whether to keep trusting the suggestion, and that
is answered by comparing what the model proposed against what survived to
approval — the one comparison where a human has definitely looked. An edit
made and then reverted does not count; an approval does.

Three breakdowns, and the third is the one that decides things:

  **By vendor** — where the model is reliable. White Cap is always 3015, so
  it should be near perfect; ready-mix is the hard case §8 exists for.

  **By cost code** — which codes get confused for each other. A code with a
  low rate usually means its `element_keywords` need work, which is a fix
  somebody can actually make.

  **By confidence band** — whether the confidence number means anything. If
  "high" is accepted no more often than "low", the number is decoration, and
  every UI decision resting on it (the colour of the pill, whether a reviewer
  looks twice) is resting on nothing.

Counted per invoice, not per cost row. A reviewer who accepts the suggested
code and then splits the invoice across two codes has accepted the
suggestion; counting rows would score that as 50%.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .invoice_rules import confidence_band
from .supabase_client import get_service_client

log = logging.getLogger(__name__)

# Only these have been through a person. Anything earlier is a suggestion
# nobody has judged yet, and counting it would measure the model against
# itself.
JUDGED_STATUSES = ("approved", "uploaded", "filed")


@dataclass
class Bucket:
    """Acceptance for one vendor, code, or confidence band."""

    key: str
    label: str
    total: int = 0
    accepted: int = 0

    @property
    def rate(self) -> Optional[float]:
        """None rather than 0.0 when there is nothing to judge — a vendor
        with no approved invoices has no accuracy, and rendering it as 0%
        would read as the model failing."""
        return (self.accepted / self.total) if self.total else None


@dataclass
class MetricsReport:
    generated_at: datetime
    window_days: int
    judged: int = 0
    accepted: int = 0
    by_vendor: list[Bucket] = field(default_factory=list)
    by_cost_code: list[Bucket] = field(default_factory=list)
    by_confidence: list[Bucket] = field(default_factory=list)
    unsuggested: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def rate(self) -> Optional[float]:
        return (self.accepted / self.judged) if self.judged else None


def _sb():
    return get_service_client()


def build_report(*, window_days: int = 90) -> MetricsReport:
    """Acceptance over the last `window_days`, by vendor, code, and band."""
    since = datetime.now(timezone.utc) - timedelta(days=window_days)
    report = MetricsReport(
        generated_at=datetime.now(timezone.utc), window_days=window_days
    )

    invoices = (
        _sb()
        .table("invoices")
        .select("id,vendor_id,project_id,approved_at,status")
        .in_("status", list(JUDGED_STATUSES))
        .gte("approved_at", since.isoformat())
        .limit(5000)
        .execute()
        .data
        or []
    )
    if not invoices:
        report.notes.append(
            f"No invoices have been approved in the last {window_days} days, "
            "so there is nothing to score the AI against yet."
        )
        return report

    ids = [i["id"] for i in invoices]
    suggestions = _latest_suggestion_per_invoice(ids)
    final_codes = _final_codes_per_invoice(ids)
    vendors = _name_map("vendors", "invoice_name")
    codes = _name_map("cost_codes", "code")

    vendor_buckets: dict[str, Bucket] = {}
    code_buckets: dict[str, Bucket] = {}
    band_buckets: dict[str, Bucket] = {
        band: Bucket(key=band, label=band.title())
        for band in ("high", "medium", "low", "none")
    }

    for invoice in invoices:
        suggestion = suggestions.get(invoice["id"])
        if not suggestion or not suggestion.get("cost_code_id"):
            # Flagged, hand-coded, or ingested before the AI was configured.
            # Excluded rather than counted as a miss: the model never made a
            # claim, so scoring it against one would be inventing a failure.
            report.unsuggested += 1
            continue

        suggested_code = suggestion["cost_code_id"]
        kept = final_codes.get(invoice["id"], set())
        # Accepted means the suggested code survived to approval, even if the
        # reviewer added a second code alongside it. They agreed with the
        # read and then split the money; that is not a miss.
        was_accepted = suggested_code in kept

        report.judged += 1
        report.accepted += int(was_accepted)

        _tally(
            vendor_buckets,
            invoice.get("vendor_id"),
            vendors.get(invoice.get("vendor_id") or "", "Unknown vendor"),
            was_accepted,
        )
        # Bucketed by what the AI SAID, not by what was kept. The question is
        # "when it says 3002, is it right", and bucketing by the final code
        # would answer a different one.
        _tally(
            code_buckets,
            suggested_code,
            codes.get(suggested_code, "Unknown code"),
            was_accepted,
        )
        _tally_bucket(
            band_buckets[confidence_band(_as_float(suggestion.get("confidence")))],
            was_accepted,
        )

    report.by_vendor = _ranked(vendor_buckets)
    report.by_cost_code = _ranked(code_buckets)
    report.by_confidence = [
        band_buckets[b] for b in ("high", "medium", "low", "none")
        if band_buckets[b].total
    ]

    _add_notes(report)
    return report


def _add_notes(report: MetricsReport) -> None:
    """The reading, not just the numbers.

    A table of percentages invites everyone to find their own story in it.
    These are the two readings that should change what somebody does.
    """
    if report.unsuggested:
        report.notes.append(
            f"{report.unsuggested} approved invoice(s) had no AI suggestion "
            "to score — flagged, coded by hand, or ingested before the AI "
            "was configured. They are excluded rather than counted as misses."
        )

    bands = {b.key: b for b in report.by_confidence}
    high, low = bands.get("high"), bands.get("low")
    if high and low and high.total >= 10 and low.total >= 10:
        if high.rate is not None and low.rate is not None:
            if high.rate <= low.rate + 0.05:
                report.notes.append(
                    f"High-confidence suggestions are accepted "
                    f"{high.rate:.0%} of the time and low-confidence ones "
                    f"{low.rate:.0%}. The confidence number is not "
                    "separating good answers from bad ones, so anything "
                    "resting on it — the colour of the pill, whether a "
                    "reviewer looks twice — is resting on nothing."
                )
            else:
                report.notes.append(
                    f"Confidence is doing its job: high-confidence "
                    f"suggestions are accepted {high.rate:.0%} of the time "
                    f"against {low.rate:.0%} for low."
                )

    weak = [
        b for b in report.by_cost_code
        if b.total >= 5 and b.rate is not None and b.rate < 0.5
    ]
    if weak:
        names = ", ".join(b.label for b in weak[:3])
        report.notes.append(
            f"Suggested and then rejected more often than not: {names}. "
            "That usually means the element keywords on those codes need "
            "work, which is a fix somebody can make under Admin → Cost codes."
        )


def _tally(buckets: dict, key: Optional[str], label: str, accepted: bool) -> None:
    if not key:
        return
    bucket = buckets.setdefault(key, Bucket(key=key, label=label))
    _tally_bucket(bucket, accepted)


def _tally_bucket(bucket: Bucket, accepted: bool) -> None:
    bucket.total += 1
    bucket.accepted += int(accepted)


def _ranked(buckets: dict) -> list[Bucket]:
    """Worst first, among buckets with enough volume to mean anything.

    A single invoice at 0% is noise, and sorting it to the top would bury the
    vendor with forty invoices at 60% — the one actually worth fixing.
    """
    return sorted(
        buckets.values(),
        key=lambda b: (b.total < 5, b.rate if b.rate is not None else 1.0, -b.total),
    )


def _latest_suggestion_per_invoice(ids: list[str]) -> dict[str, dict]:
    """Suggestions are append-only (a retry adds a row), so the newest one is
    the claim that was standing when the invoice was approved."""
    rows = (
        _sb()
        .table("invoice_suggestions")
        .select("invoice_id,cost_code_id,confidence,created_at")
        .in_("invoice_id", ids)
        .order("created_at", desc=True)
        .limit(20000)
        .execute()
        .data
        or []
    )
    out: dict[str, dict] = {}
    for row in rows:
        out.setdefault(row["invoice_id"], row)
    return out


def _final_codes_per_invoice(ids: list[str]) -> dict[str, set]:
    rows = (
        _sb()
        .table("invoice_costs")
        .select("invoice_id,cost_code_id")
        .in_("invoice_id", ids)
        .limit(20000)
        .execute()
        .data
        or []
    )
    out: dict[str, set] = defaultdict(set)
    for row in rows:
        out[row["invoice_id"]].add(row["cost_code_id"])
    return out


def _name_map(table: str, name_column: str) -> dict[str, str]:
    rows = (
        _sb().table(table).select(f"id,{name_column}").limit(2000).execute().data or []
    )
    return {r["id"]: r.get(name_column) or "" for r in rows}


def _as_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
