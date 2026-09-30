"""
AI invoice extraction and cost-code suggestion (prompt §8).

One Claude call per invoice. It reads the PDF and returns the §8 JSON: who the
vendor is, which project it belongs to, the header fields, the line items, and
one or more cost code rows with a confidence, a rationale, and alternatives.

The hard part is not extraction, it is the cost code. A mix number like
`4018045` legitimately serves columns, basement walls, retaining walls, and
misc walls — four different codes. Prompt §8 gives an authority order for
resolving that, and this module's job is to hand the model everything each
level of that order needs:

  1. An explicit comment written on the invoice. Wins outright.
  2. The mix number, matched against this project's confirmed mix designs.
  3. The description text, when the mix number is missing or unknown.
  4. The vendor's default code, for non-concrete vendors.

Plus the tiebreakers: what phase the project is in (inferred from the last 30
days of approved invoices), the pour quantity, and the pump line size.

Nothing here writes to the database. The caller decides whether the result
becomes a `suggested` invoice or a `flagged` one.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from pydantic import BaseModel

from .claude_client import pdf_block, structured_call, text_block
from .config import settings
from .invoice_rules import next_phases, phase_of
from .supabase_client import get_service_client

log = logging.getLogger(__name__)


# ─── Output schema (prompt §8) ────────────────────────────────────────


class ExtractedLine(BaseModel):
    ticket: Optional[str] = None
    prod_num: Optional[str] = None
    description: Optional[str] = None
    qty: Optional[float] = None
    uom: Optional[str] = None
    unit_price: Optional[float] = None
    gross: Optional[float] = None


class CostCodeAlternative(BaseModel):
    code: str
    why: str


class SuggestedCostCode(BaseModel):
    code: str
    amount: Optional[float] = None
    confidence: Optional[float] = None
    rationale: Optional[str] = None
    alternatives: list[CostCodeAlternative] = []


class ProjectMatch(BaseModel):
    project_no: Optional[str] = None
    confidence: Optional[float] = None
    evidence: Optional[str] = None


class InvoiceExtraction(BaseModel):
    """Mirrors the §8 output schema exactly, field for field."""

    vendor_name_on_invoice: Optional[str] = None
    vendor_bt_name: Optional[str] = None
    project_match: ProjectMatch = ProjectMatch()
    invoice_no: Optional[str] = None
    bill_no: Optional[str] = None
    invoice_date: Optional[str] = None       # "YYYY-MM-DD"
    amount: Optional[float] = None
    is_credit: bool = False
    comment_hint: Optional[str] = None
    mix_nos: list[str] = []
    job_hint: Optional[str] = None
    lines: list[ExtractedLine] = []
    cost_codes: list[SuggestedCostCode] = []
    flags: list[str] = []


# ─── Prompt ───────────────────────────────────────────────────────────

SYSTEM = """\
You read vendor invoices for Ferrocrete Builders, a concrete subcontractor, \
and prepare them for entry into BuilderTrend as bills.

Two things matter, in this order.

First, read the invoice accurately: vendor, invoice number, date, total, and \
the line items. Report what the document says. Where a field is missing or \
illegible, return null and name it in `flags` — a reviewer sees this before \
anything is entered, and a blank field is easy to fix while a plausible wrong \
one is not.

Second, decide which cost code the money belongs to. This is the judgement \
call the whole workflow exists for, and it is genuinely ambiguous on \
ready-mix invoices: one concrete mix is commonly approved for several \
different building elements, and those elements bill to different codes. The \
sections below give you the evidence and the order of authority for resolving \
it. Follow that order rather than picking whichever code looks most typical.

Report your confidence honestly. A low confidence with a clear rationale is \
more useful than a high one you cannot justify — low confidence routes the \
invoice to a person who knows the job, which is the correct outcome when the \
evidence is thin. Nothing you return is ever entered automatically.\
"""

AUTHORITY_ORDER = """\
# Choosing the cost code

Work down this list. Stop at the first level that gives you an answer, and \
say which level you used in `rationale`.

1. **An explicit cost code written on the invoice.** Someone in accounting or \
   the field sometimes writes the code directly on the PDF, often by hand — \
   for example "Cost code:3002 column". If you find one, it wins outright: \
   use it, set confidence at or above 0.9, and put the exact text you read in \
   `comment_hint`. A human already made this decision.

2. **The mix number on the line items**, matched against this project's \
   confirmed mix designs below. Each mix maps to one or more building \
   elements, and each element maps to a cost code. Put every mix number you \
   find in `mix_nos` whether or not you could match it.

3. **The description text**, when the mix number is missing or is not in the \
   list. Phrases like "6000 PSI @ 28 DAYS ADVA", "SHOTCRETE", or "TOPPING \
   SLAB" identify the element directly.

4. **The vendor's default cost code**, for anything that is not ready-mix \
   concrete — hardware, rebar, lumber, pump service. The vendor list below \
   gives the default. Use it unless the invoice itself says otherwise.

## When one mix serves several elements

This is the common case, not the exception. Use these tiebreakers together:

- **Where the project is now.** The recent-pours section below says what was \
  most recently approved on this job. Concrete goes in a sequence: footings \
  and piles, then rat or mat slabs, then slab-on-grade and basement walls, \
  then columns and shear walls, then elevated decks, then topping slabs and \
  site concrete. Prefer the element that plausibly comes next. Levels overlap \
  — columns on one floor pour while the deck below cures — so the current \
  phase stays a candidate.
- **Quantity.** Small pours, under roughly 15 cubic yards, lean toward \
  columns and walls. Large pours lean toward slabs, decks, and footings.
- **Pump line size.** A mix batched for a 3" line is going somewhere a 4" \
  line cannot reach, which narrows the element list per the yardage sheet.

## When the comment and the mix disagree

Keep the comment as your suggestion, but lower `confidence` to between 0.6 \
and 0.8, and put the mix-derived code FIRST in `alternatives` with a one-line \
note explaining the conflict. The reviewer needs to see both readings. Do not \
silently pick one.

## Rules that are not negotiable

- Every `code` you return, in `cost_codes` and in `alternatives`, must be \
  copied verbatim from the candidate list. Never invent a code, never \
  abbreviate one, and never return a bare base number like "3002".
- `cost_codes` amounts must sum to the invoice total. If the lines clearly \
  belong to different elements, return one entry per element and allocate tax \
  and fees proportionally.
- If you cannot identify the vendor or the project with reasonable \
  confidence, say so in `flags` and leave the field null. Do not guess at \
  either — a bill on the wrong job is worse than a bill that waits.\
"""


# ─── Context assembly ─────────────────────────────────────────────────


def _format_projects(projects: list[dict]) -> str:
    if not projects:
        return "(none loaded — flag the invoice as unknown_project)"
    lines = []
    for p in projects:
        bits = [f"{p['project_no']} — {p['name']}"]
        if p.get("address"):
            bits.append(f"at {p['address']}")
        if p.get("status") == "active":
            bits.append("[NEW JOB, separate workflow — flag, do not route]")
        if not p.get("onboarded_at"):
            bits.append("[not onboarded]")
        lines.append("- " + ", ".join(bits))
    return "\n".join(lines)


def _format_vendors(vendors: list[dict], code_labels: dict[str, str]) -> str:
    if not vendors:
        return "(none loaded — flag the invoice as unknown_vendor)"
    lines = []
    for v in vendors:
        bits = [f"{v['invoice_name']} → BuilderTrend: {v['bt_name']}"]
        if v.get("aliases"):
            bits.append(f"also printed as {', '.join(v['aliases'])}")
        default = code_labels.get(v.get("default_cost_code_id") or "")
        if default:
            bits.append(f"default code: {default}")
        if v.get("is_concrete_supplier"):
            bits.append("ready-mix supplier")
        lines.append("- " + "; ".join(bits))
    return "\n".join(lines)


def _format_mix_designs(rows: list[dict], code_labels: dict[str, str]) -> str:
    """The project's live mix designs.

    Rows with no cost code are shown but marked unmapped: prompt §7.0 says an
    unmapped mix must not be used to guess a concrete code, so the model is
    told that rather than being left to infer it from a missing field.
    """
    if not rows:
        return (
            "(no confirmed mix design for this project — do NOT guess a "
            "concrete cost code from a mix number; fall back to description "
            "text or the vendor default, and note it in `flags`)"
        )
    lines = []
    for r in rows:
        bits = [f"mix {r['mix_no']}"]
        if r.get("psi"):
            bits.append(f"{r['psi']} psi")
        if r.get("element_use"):
            bits.append("serves " + ", ".join(r["element_use"]))
        if r.get("pump_line"):
            bits.append(f"batched for {r['pump_line']}")
        code = code_labels.get(r.get("cost_code_id") or "")
        bits.append(f"→ {code}" if code else "→ UNMAPPED (do not use this mix to pick a code)")
        lines.append("- " + "; ".join(bits))
    return "\n".join(lines)


def _format_cost_codes(codes: list[dict]) -> str:
    lines = []
    for c in codes:
        keywords = c.get("element_keywords") or []
        suffix = f"  [{', '.join(keywords)}]" if keywords else ""
        lines.append(f"- {c['code']}{suffix}")
    return "\n".join(lines)


def _format_recent_pours(recent: list[dict], code_labels: dict[str, str]) -> str:
    """What was approved on this project lately, newest first.

    Drives the §8 phase inference. Also states the inferred phase explicitly
    rather than making the model derive it from dates, because the sequence is
    domain knowledge the prompt already carries.
    """
    if not recent:
        return (
            "(nothing approved on this project in the lookback window — no "
            "phase signal available, so weight quantity and pump line more "
            "heavily)"
        )

    lines = []
    phases_seen: list[str] = []
    for r in recent:
        code = code_labels.get(r.get("cost_code_id") or "") or "unknown code"
        when = r.get("approved_at") or ""
        lines.append(f"- {when[:10]}: {code}")
        p = phase_of(code)
        if p and p not in phases_seen:
            phases_seen.append(p)

    out = "\n".join(lines)
    if phases_seen:
        likely = next_phases(phases_seen[0])
        out += (
            f"\n\nMost recent phase: {phases_seen[0]}. "
            f"Likely next, in order: {', '.join(likely[:3])}."
        )
    return out


def _load_context(project_id: Optional[str]) -> dict:
    """Everything the model needs, in one pass of queries."""
    sb = get_service_client()

    projects = (
        sb.table("projects")
        .select("id,project_no,name,address,status,onboarded_at,pe_user_id")
        .is_("deleted_at", "null")
        .order("project_no")
        .execute()
        .data
    ) or []

    vendors = (
        sb.table("vendors")
        .select("id,invoice_name,bt_name,aliases,default_cost_code_id,is_concrete_supplier")
        .eq("active", True)
        .order("invoice_name")
        .execute()
        .data
    ) or []

    codes = (
        sb.table("cost_codes")
        .select("id,code,base_code,description,element_keywords")
        .eq("active", True)
        .execute()
        .data
    ) or []
    # Concrete families first: a ready-mix invoice's answer is almost always
    # in them, and the model reads the list top to bottom.
    priority = {"3002": 0, "3008": 1, "3009": 2, "3003": 3, "3005": 3, "3015": 3}
    codes.sort(key=lambda c: (priority.get(c.get("base_code") or "", 9), c["code"]))

    mix_rows: list[dict] = []
    recent: list[dict] = []
    if project_id:
        mix_rows = (
            sb.table("mix_designs")
            .select("mix_no,psi,element_use,pump_line,cost_code_id")
            .eq("project_id", project_id)
            .is_("superseded_at", "null")
            .order("mix_no")
            .execute()
            .data
        ) or []

        since = (
            datetime.now(timezone.utc)
            - timedelta(days=settings.phase_lookback_days)
        ).isoformat()
        approved = (
            sb.table("invoices")
            .select("id,approved_at")
            .eq("project_id", project_id)
            .not_.is_("approved_at", "null")
            .gte("approved_at", since)
            .order("approved_at", desc=True)
            .limit(40)
            .execute()
            .data
        ) or []
        if approved:
            cost_rows = (
                sb.table("invoice_costs")
                .select("invoice_id,cost_code_id")
                .in_("invoice_id", [a["id"] for a in approved])
                .execute()
                .data
            ) or []
            by_invoice: dict[str, list[str]] = {}
            for cr in cost_rows:
                by_invoice.setdefault(cr["invoice_id"], []).append(cr["cost_code_id"])
            for a in approved:
                for code_id in by_invoice.get(a["id"], []):
                    recent.append(
                        {"cost_code_id": code_id, "approved_at": a["approved_at"]}
                    )

    return {
        "projects": projects,
        "vendors": vendors,
        "codes": codes,
        "mix_rows": mix_rows,
        "recent": recent,
        "code_labels": {c["id"]: c["code"] for c in codes},
        "code_ids": {c["code"]: c["id"] for c in codes},
    }


def build_instruction(
    ctx: dict,
    *,
    source_filename: Optional[str] = None,
    project_hint: Optional[str] = None,
    vendor_hint: Optional[str] = None,
) -> str:
    """Assemble the user-turn instruction from the loaded context."""
    parts = [
        "Read this vendor invoice and return the structured result.",
    ]
    if source_filename:
        parts.append(
            f"\nThe file is named {source_filename!r}. Filenames are often "
            "wrong — read the document, and only use the name as a tiebreaker."
        )
    if project_hint:
        parts.append(
            f"\nIntake believes this belongs to project {project_hint}. "
            "Confirm it against the document and say so in "
            "`project_match.evidence`; disagree if the document disagrees."
        )
    if vendor_hint:
        parts.append(
            f"\nA person has already identified the vendor as {vendor_hint}. "
            "Treat that as settled and return it, unless the document plainly "
            "contradicts it — in which case say so in `flags`."
        )

    parts.append("\n" + AUTHORITY_ORDER)
    parts.append("\n# Projects\n" + _format_projects(ctx["projects"]))
    parts.append(
        "\n# Vendors (letterhead name → BuilderTrend name)\n"
        + _format_vendors(ctx["vendors"], ctx["code_labels"])
    )
    parts.append(
        "\n# This project's confirmed mix designs\n"
        + _format_mix_designs(ctx["mix_rows"], ctx["code_labels"])
    )
    parts.append(
        "\n# Recently approved pours on this project\n"
        + _format_recent_pours(ctx["recent"], ctx["code_labels"])
    )
    parts.append(
        "\n# Candidate cost codes (copy one of these verbatim)\n"
        + _format_cost_codes(ctx["codes"])
    )
    return "\n".join(parts)


# ─── The call ─────────────────────────────────────────────────────────


def extract_invoice(
    pdf_bytes: bytes,
    *,
    project_id: Optional[str] = None,
    vendor_id: Optional[str] = None,
    source_filename: Optional[str] = None,
    project_hint: Optional[str] = None,
) -> tuple[InvoiceExtraction, dict, dict]:
    """Read one invoice PDF.

    Returns (extraction, meta, ctx). `ctx` is handed back so the caller can
    resolve names to ids without re-querying, and so the resolution and the
    prompt provably used the same reference data.
    """
    ctx = _load_context(project_id)
    if not ctx["codes"]:
        raise RuntimeError(
            "No active cost codes are loaded. Apply migration 002 before "
            "running intake."
        )

    vendor_hint = None
    if vendor_id:
        known = next((v for v in ctx["vendors"] if v["id"] == vendor_id), None)
        if known:
            vendor_hint = known["invoice_name"]

    instruction = build_instruction(
        ctx,
        source_filename=source_filename,
        project_hint=project_hint,
        vendor_hint=vendor_hint,
    )

    extraction, meta = structured_call(
        output_model=InvoiceExtraction,
        system=SYSTEM,
        content=[pdf_block(pdf_bytes), text_block(instruction)],
    )
    log.info(
        "extracted invoice: vendor=%r project=%r amount=%s codes=%d flags=%s",
        extraction.vendor_name_on_invoice,
        extraction.project_match.project_no,
        extraction.amount,
        len(extraction.cost_codes),
        extraction.flags,
    )
    return extraction, meta, ctx


# ─── Resolution ───────────────────────────────────────────────────────


class ResolvedCost(BaseModel):
    cost_code_id: str
    code: str
    amount: Optional[float] = None
    confidence: Optional[float] = None
    rationale: Optional[str] = None
    alternatives: list[dict] = []


def resolve_vendor(
    extraction: InvoiceExtraction, ctx: dict
) -> tuple[Optional[dict], Optional[str]]:
    """Match the extracted vendor to a vendors row.

    Returns (vendor, problem). Matches on the BuilderTrend name first because
    that is the field the model was given a mapping for, then the letterhead
    name, then aliases. Never creates a vendor (prompt §12).
    """
    vendors = ctx["vendors"]
    bt = (extraction.vendor_bt_name or "").strip().lower()
    inv = (extraction.vendor_name_on_invoice or "").strip().lower()

    if not bt and not inv:
        return None, "The invoice's vendor could not be read."

    for v in vendors:
        if bt and v["bt_name"].strip().lower() == bt:
            return v, None
    for v in vendors:
        if inv and v["invoice_name"].strip().lower() == inv:
            return v, None
    for v in vendors:
        aliases = [a.strip().lower() for a in (v.get("aliases") or [])]
        if (inv and inv in aliases) or (bt and bt in aliases):
            return v, None

    named = extraction.vendor_name_on_invoice or extraction.vendor_bt_name
    return None, (
        f"Vendor {named!r} is not in the vendor list. Add the mapping under "
        "Admin, then retry — the app never creates a BuilderTrend vendor."
    )


def resolve_project(
    extraction: InvoiceExtraction, ctx: dict
) -> tuple[Optional[dict], Optional[str], Optional[str]]:
    """Match the extracted project.

    Returns (project, flag_code, problem). Distinguishes the three outcomes
    prompt §7.2 treats differently: no match, a match on a new job that is out
    of scope, and a match on a job nobody has onboarded.
    """
    wanted = (extraction.project_match.project_no or "").strip().lower()
    confidence = extraction.project_match.confidence

    if not wanted:
        return None, "unknown_project", (
            "The job on this invoice could not be matched to a project. "
            + (extraction.project_match.evidence or "")
        ).strip()

    match = next(
        (
            p
            for p in ctx["projects"]
            if p["project_no"].strip().lower() == wanted
            or p["name"].strip().lower() == wanted
        ),
        None,
    )
    if not match:
        return None, "unknown_project", (
            f"Project {extraction.project_match.project_no!r} is not in the "
            "project list. Add it under Projects, then retry."
        )

    # A confident-looking guess on the wrong job is the expensive failure, so
    # a weak match is treated as no match rather than accepted.
    if confidence is not None and confidence < settings.confidence_low:
        return None, "unknown_project", (
            f"Matched {match['project_no']} with only "
            f"{confidence:.0%} confidence. Evidence: "
            f"{extraction.project_match.evidence or 'none given'}."
        )

    if match.get("status") == "active":
        return match, "new_project", (
            f"{match['name']} is a new job handled by a separate workflow. "
            "This invoice is recorded but not routed."
        )

    if not match.get("onboarded_at"):
        return match, "project_not_onboarded", (
            f"{match['name']} has not been onboarded — it needs a project "
            "engineer and, for concrete jobs, a confirmed mix design."
        )

    return match, None, None


def resolve_costs(
    extraction: InvoiceExtraction, ctx: dict
) -> tuple[list[ResolvedCost], list[str]]:
    """Turn suggested codes into rows with real cost_code_ids.

    Returns (rows, warnings). A code the model invented is dropped, not
    created — prompt §12. Alternatives are resolved the same way, so a
    one-click chip on the review screen can never point at a code that does
    not exist.
    """
    code_ids: dict[str, str] = ctx["code_ids"]
    code_ids_ci = {k.lower(): v for k, v in code_ids.items()}

    rows: list[ResolvedCost] = []
    warnings: list[str] = []
    seen: set[str] = set()

    for entry in extraction.cost_codes:
        raw = (entry.code or "").strip()
        if not raw:
            warnings.append("A cost code entry had no code and was dropped.")
            continue

        code_id = code_ids.get(raw) or code_ids_ci.get(raw.lower())
        if not code_id:
            warnings.append(
                f"Suggested code {raw!r} is not a known cost code and was "
                "dropped. Pick one on the review screen."
            )
            continue
        if code_id in seen:
            warnings.append(
                f"Code {raw!r} was suggested more than once; kept the first."
            )
            continue
        seen.add(code_id)

        alts = []
        for alt in entry.alternatives:
            alt_raw = (alt.code or "").strip()
            alt_id = code_ids.get(alt_raw) or code_ids_ci.get(alt_raw.lower())
            if alt_id:
                alts.append({"cost_code_id": alt_id, "code": alt_raw, "why": alt.why})
            else:
                warnings.append(
                    f"Alternative code {alt_raw!r} is not a known cost code "
                    "and was dropped."
                )

        rows.append(
            ResolvedCost(
                cost_code_id=code_id,
                code=raw,
                amount=entry.amount,
                confidence=entry.confidence,
                rationale=entry.rationale,
                alternatives=alts,
            )
        )

    if not rows:
        warnings.append(
            "No usable cost code was suggested. The reviewer must pick one."
        )

    return rows, warnings


def parse_invoice_date(value: Optional[str]) -> Optional[date]:
    """Parse the model's YYYY-MM-DD, tolerating the two common US variants.

    A date that will not parse is left as None and flagged rather than
    defaulted to today: the due date is derived from it, so a wrong invoice
    date silently produces a wrong due date.
    """
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    log.warning("could not parse invoice_date %r", value)
    return None
