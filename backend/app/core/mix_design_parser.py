"""
Mix design submittal parsing (prompt §7.0).

At project onboarding Linda or the admin uploads the concrete supplier's mix
design submittal. Its yardage sheet maps each mix number to a PSI and the
building elements that mix serves — e.g. `4018045` = Columns, Basement Walls,
Retaining Walls, Misc Walls at 4000 psi. That table is what lets the AI later
turn a mix number printed on a ready-mix invoice into a cost code (§8.2).

This module reads the PDF with the Claude API, returns the parsed rows for
human confirmation, and proposes a cost code per row from
`cost_codes.element_keywords`. Nothing is written until the PE confirms —
prompt §7.0 requires the parsed table be shown before saving, and §7.0 also
forbids the AI from guessing concrete codes without a confirmed mix design.
"""

import logging
from typing import Optional

from pydantic import BaseModel

from .claude_client import pdf_block, structured_call, text_block
from .supabase_client import get_service_client

log = logging.getLogger(__name__)


# ─── Output schema ────────────────────────────────────────────────────


class ParsedMixRow(BaseModel):
    """One row of the yardage sheet."""

    mix_no: str
    psi: Optional[int] = None
    element_use: list[str] = []
    pump_line: Optional[str] = None

    # The AI's proposed element-to-cost-code mapping. `proposed_code` must be
    # a verbatim code from the candidate list; anything else is discarded by
    # resolve_proposals() rather than created (prompt §12).
    proposed_code: Optional[str] = None
    confidence: Optional[float] = None
    rationale: Optional[str] = None


class MixDesignParse(BaseModel):
    revision: Optional[str] = None
    project_hint: Optional[str] = None
    supplier_hint: Optional[str] = None
    rows: list[ParsedMixRow] = []
    # Anything the reader should check by eye: an unreadable row, a mix with
    # no element listed, two rows claiming the same mix number.
    notes: list[str] = []


# ─── Prompt ───────────────────────────────────────────────────────────

SYSTEM = """\
You read concrete mix design submittals for Ferrocrete Builders, a concrete \
subcontractor, and extract their yardage sheets.

A yardage sheet lists each approved mix by number, alongside the design \
strength in psi and the building elements that mix is approved for. One mix \
commonly serves several elements. Some sheets also note the pump line size \
the mix is batched for (3" line, 4" line), which narrows which elements it \
can actually be placed in.

Your output feeds a cost-coding step: later, when a ready-mix invoice arrives \
carrying a mix number, the accounting app looks that number up in this table \
to decide which cost code the concrete belongs to. So the element list per \
mix matters as much as the mix number.

Read the whole document, including revision tables and notes, not just the \
first page that looks like a yardage sheet. Extract what the sheet says. \
Where the sheet is ambiguous or unreadable, say so in `notes` and leave the \
field null rather than inferring a value — a reviewer confirms this table \
before it is saved, and a plausible guess is harder to catch than a blank.\
"""

MAPPING_INSTRUCTIONS = """\
For each mix, also propose the cost code its concrete should be billed to, \
chosen verbatim from the candidate list below.

A mix design row is ready-mix concrete being placed, so the right code is \
almost always from the `3002 - Concrete Ready Mix` family, picked by element: \
columns to Concrete Columns, shear/basement/retaining/misc walls to Concrete \
Walls, slab-on-grade and curbs and pads to Concrete SOG, elevated and podium \
decks and topping slabs to Concrete Decks, footings and grade beams and piers \
and pile caps to Concrete Footings, mat pours to Concrete Mat Slab, rat/mud \
slabs to Concrete Rat Slab, shotcrete to Concrete Shotcrete.

When one mix serves elements that map to different codes, pick the code for \
the element the sheet lists first or emphasizes, set `confidence` below 0.6, \
and name the competing elements in `rationale`. The reviewer resolves the \
split — do not average or invent a combined code.

Set `proposed_code` to null if no candidate fits. Never return a code that is \
not in the list, and never return a bare base number like "3002".

Give `confidence` as 0.0 to 1.0: above 0.85 when the sheet names the element \
plainly and one code clearly covers it, 0.6 to 0.85 when you are reading \
through an abbreviation or an implied element, below 0.6 when the mix spans \
several codes or the element is a guess.

Candidate cost codes:
%(codes)s\
"""


# ─── Parsing ──────────────────────────────────────────────────────────


def load_candidate_codes() -> list[dict]:
    """Active cost codes, concrete families first.

    Ordering matters: the model reads the list top to bottom, and a mix design
    row is ready-mix concrete, so the ready-mix and pumping families belong at
    the front. The rest stay available because a submittal occasionally covers
    grout or CMU.
    """
    sb = get_service_client()
    res = (
        sb.table("cost_codes")
        .select("code,description,element_keywords,base_code,category")
        .eq("active", True)
        .order("code")
        .execute()
    )
    rows = res.data or []

    priority = {"3002": 0, "3008": 1, "3009": 2, "4000": 3, "4001": 3, "4002": 3}
    rows.sort(key=lambda r: (priority.get(r.get("base_code") or "", 9), r["code"]))
    return rows


def format_candidate_codes(codes: list[dict]) -> str:
    lines = []
    for c in codes:
        keywords = c.get("element_keywords") or []
        suffix = f"  [{', '.join(keywords)}]" if keywords else ""
        lines.append(f"- {c['code']}{suffix}")
    return "\n".join(lines)


def parse_submittal(
    pdf_bytes: bytes,
    *,
    project_no: Optional[str] = None,
    project_name: Optional[str] = None,
) -> tuple[MixDesignParse, dict]:
    """Parse a mix design submittal PDF.

    Returns (parse, meta). Raises ClaudeNotConfigured / ClaudeOutputInvalid /
    the SDK's typed errors — the caller surfaces the message to the uploader
    rather than saving a half-parsed table.
    """
    codes = load_candidate_codes()
    if not codes:
        raise RuntimeError(
            "No active cost codes are loaded. Apply migration 002 before "
            "onboarding a project."
        )

    context = ""
    if project_no or project_name:
        context = (
            f"\nThis submittal is being onboarded for project "
            f"{project_no or ''} {project_name or ''}".rstrip() + ". "
            "Confirm in `project_hint` whether the document agrees; say so in "
            "`notes` if it names a different job.\n"
        )

    instruction = (
        "Extract the yardage sheet from this mix design submittal."
        + context
        + "\n"
        + MAPPING_INSTRUCTIONS % {"codes": format_candidate_codes(codes)}
    )

    parse, meta = structured_call(
        output_model=MixDesignParse,
        system=SYSTEM,
        content=[pdf_block(pdf_bytes), text_block(instruction)],
    )

    log.info(
        "parsed mix design: %d rows, revision=%s, %d notes",
        len(parse.rows),
        parse.revision,
        len(parse.notes),
    )
    return parse, meta


# ─── Proposal resolution ──────────────────────────────────────────────


def resolve_proposals(parse: MixDesignParse) -> tuple[list[dict], list[str]]:
    """Turn parsed rows into confirmable dicts with cost_code_id resolved.

    Returns (rows, warnings). A `proposed_code` that does not match a real
    cost code is dropped and reported — the app never creates a cost code
    (prompt §12), and silently keeping an invented string would let it reach
    BuilderTrend as a bad Costs row.
    """
    sb = get_service_client()
    res = sb.table("cost_codes").select("id,code").eq("active", True).execute()
    by_code = {r["code"]: r["id"] for r in (res.data or [])}
    # Case-insensitive fallback: the model occasionally shifts the casing of a
    # description while copying the code verbatim otherwise.
    by_code_ci = {k.lower(): v for k, v in by_code.items()}

    warnings: list[str] = []
    seen: set[str] = set()
    rows: list[dict] = []

    for row in parse.rows:
        mix_no = (row.mix_no or "").strip()
        if not mix_no:
            warnings.append("Dropped a row with no mix number.")
            continue
        if mix_no in seen:
            warnings.append(
                f"Mix {mix_no} appeared more than once; kept the first row."
            )
            continue
        seen.add(mix_no)

        cost_code_id = None
        if row.proposed_code:
            code = row.proposed_code.strip()
            cost_code_id = by_code.get(code) or by_code_ci.get(code.lower())
            if not cost_code_id:
                warnings.append(
                    f"Mix {mix_no}: proposed code {code!r} is not a known "
                    f"cost code and was dropped. Pick one manually."
                )

        rows.append(
            {
                "mix_no": mix_no,
                "psi": row.psi,
                "element_use": [e.strip() for e in row.element_use if e and e.strip()],
                "pump_line": (row.pump_line or "").strip() or None,
                "cost_code_id": cost_code_id,
                "proposed_code": row.proposed_code,
                "confidence": row.confidence,
                "rationale": row.rationale,
            }
        )

    if not rows:
        warnings.append(
            "No mix rows were extracted. Check that the PDF contains a "
            "yardage sheet and is not a scan too low-resolution to read."
        )

    return rows, warnings
