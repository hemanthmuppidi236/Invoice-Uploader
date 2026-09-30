"""
Generate migrations/002_seed_cost_codes.sql from a BuilderTrend cost code export.

Usage:
    python scripts/gen_cost_code_seed.py ~/Downloads/BTCostCodes_20260929.xlsx

The export has a "Costs" sheet with two columns, CostCategory and CostCode,
both formatted "<number> - <description>". We split that into `base_code` +
`description`, keep the full string as `code` (that is literally what the
BuilderTrend Costs-row dropdown shows), and attach element keywords.

Element keywords are the whole point of this table: prompt §8.2/§8.3 has the
AI match a mix design's element uses and an invoice line's description text
against them to pick a code. The 3002/3008/3009 families are already named by
element ("Concrete Columns", "SOG Pump"), so most keywords fall straight out
of the description; ELEMENT_KEYWORDS below adds the synonyms a vendor actually
prints on an invoice ("slab on grade", "shear wall", "gunite").

Re-run this whenever BuilderTrend's cost code list changes, then apply the
generated SQL as a new numbered migration. It is idempotent: the INSERT uses
ON CONFLICT (code) DO UPDATE, so re-applying refreshes descriptions and
keywords without disturbing ids that invoice_costs rows already point at.
"""

import re
import sys
from pathlib import Path

import openpyxl

# ─── Element keyword map ──────────────────────────────────────────────
# Keyed by the description part of the code (case-insensitive, after the
# "<number> - " prefix is stripped). Terms are lowercase; matching is done
# case-insensitively by the AI prompt builder.

ELEMENT_KEYWORDS: dict[str, list[str]] = {
    # ── 3002 Concrete Ready Mix — by element ──
    "concrete columns": ["column", "columns", "col"],
    "concrete decks": [
        "deck", "decks", "elevated deck", "elevated slab", "podium deck",
        "podium slab", "topping slab", "suspended slab",
    ],
    "concrete footings": [
        "footing", "footings", "ftg", "grade beam", "grade beams", "pier",
        "piers", "pile cap", "pile caps", "cap", "caps", "spread footing",
        "continuous footing", "pile", "piles",
    ],
    "concrete mat slab": ["mat slab", "mat foundation", "mat pour", "mat"],
    "concrete misc": [
        "misc", "miscellaneous", "pit", "pits", "elevator pit", "sump",
        "site concrete", "equipment pad", "thrust block",
    ],
    "concrete rat slab": ["rat slab", "mud slab", "seal slab", "mudmat"],
    "concrete ready mix": [],  # generic parent; never a first choice
    "concrete shotcrete": ["shotcrete", "gunite", "shot crete"],
    "concrete sog": [
        "sog", "slab on grade", "slab-on-grade", "slab on ground",
        "curb", "curbs", "pad", "pads", "sidewalk", "flatwork",
    ],
    "concrete walls": [
        "wall", "walls", "shear wall", "shear walls", "basement wall",
        "basement walls", "retaining wall", "retaining walls", "misc wall",
        "misc walls", "stem wall", "core wall", "tilt wall",
    ],

    # ── 3003 Concrete — labor, materials, rentals, services ──
    "concrete": [],
    "concrete consultants & engineering": [
        "testing", "inspection", "special inspection", "consultant",
        "engineering", "lab", "cylinder", "break test", "cefali",
    ],
    "concrete labor": ["labor", "placement labor", "crew"],
    "column rental": ["column form", "sonotube", "sono tube", "column rental"],
    "reinforcing steel": [
        "rebar", "reinforcing steel", "reinforcement", "dowel", "dowels",
        "wwf", "welded wire", "mesh", "epoxy rebar",
    ],
    "concrete finishing": [
        "finishing", "finish", "trowel", "broom finish", "hard trowel",
        "sack", "rub", "grind",
    ],
    "shotcrete placement": ["shotcrete placement", "nozzle", "nozzleman"],
    "equipment rental & crane": [
        "crane", "equipment rental", "forklift", "telehandler", "boom lift",
        "skid steer", "generator",
    ],
    "studrails": ["studrail", "stud rail", "shear stud", "decon"],
    "lumber (purchased)": [
        "lumber", "plywood", "form lumber", "dimensional lumber", "2x4",
        "plyform", "shoring lumber", "form ply",
    ],
    "wall form rental": [
        "wall form", "form rental", "formwork", "gang form", "panel form",
        "efco", "symons", "doka", "peri",
    ],
    "sand & gravel on sog": [
        "sand", "gravel", "base rock", "aggregate base", "class 2 base",
        "crushed rock", "3/4 rock",
    ],
    "hardware & misc": [
        "hardware", "misc", "fasteners", "supplies", "tie wire", "chair",
        "chairs", "anchor", "anchors", "form oil", "nails", "screws",
        "blade", "tool", "white cap", "consumables", "safety",
    ],
    "shoring & falsework": [
        "shoring", "falsework", "shore", "shores", "post shore", "reshore",
        "scaffold frame",
    ],
    "caissons": ["caisson", "caissons", "drilled pier", "drilled shaft"],
    "construction waste & debris": [
        "dumpster", "debris", "waste", "disposal", "haul off", "bin",
    ],
    "concrete general conditions": [
        "general conditions", "temp power", "temp fence", "supervision",
    ],

    # ── 3008 Pumping — by element ──
    "pumping": ["pump", "pumping", "concrete pump"],
    "column pump": ["column pump", "pump column"],
    "deck pump": ["deck pump", "pump deck", "boom pump deck"],
    "footing pump": ["footing pump", "pump footing"],
    "mat slab pump": ["mat slab pump", "pump mat slab"],
    "misc/pits pump": ["misc pump", "pit pump", "pump misc"],
    "placing boom rental": ["placing boom", "boom rental", "placer"],
    "rat slab pump": ["rat slab pump", "pump rat slab"],
    "sog pump": ["sog pump", "pump sog", "slab pump"],
    "wall pump": ["wall pump", "pump wall"],

    # ── 3009 Trailer Pumping — by element ──
    "trailer pumping": ["trailer pump", "line pump", "trailer pumping"],
    "trailer columns": ["trailer pump column", "line pump column"],
    "trailer decks": ["trailer pump deck", "line pump deck"],
    "trailer footings": ["trailer pump footing", "line pump footing"],
    "trailer mat slab": ["trailer pump mat slab"],
    "trailer misc": ["trailer pump misc"],
    "trailer rat slab": ["trailer pump rat slab"],
    "trailer sog": ["trailer pump sog", "line pump sog"],
    "trailer walls": ["trailer pump wall", "line pump wall"],

    # ── 4000 Masonry ──
    "masonry": ["masonry"],
    "cmu": ["cmu", "block", "masonry block", "concrete block", "grout"],
    "brick & vaneer": ["brick", "veneer", "vaneer", "stone veneer"],

    # ── A handful outside concrete that plausibly land on a Ferrocrete bill ──
    "earthwork": ["earthwork", "grading", "excavation", "backfill", "import soil"],
    "earth shoring": ["soldier pile", "tieback", "lagging", "earth shoring"],
    "dewatering": ["dewatering", "well point", "sump pump rental"],
    "site preparation": ["site prep", "clear and grub", "demo prep"],
    "structural steel framing": ["structural steel", "embeds", "embed plate", "steel beam"],
    "expansion control": ["expansion joint", "control joint", "joint filler"],
    "rough carpentry": ["rough carpentry", "framing lumber", "blocking"],
    "waterproofing": ["waterproofing", "membrane", "vapor barrier", "visqueen", "damp proofing"],
    "fireproofing": ["fireproofing", "intumescent", "spray fireproofing"],
    "surveying": ["survey", "surveying", "layout", "staking"],
    "quality control services": ["quality control", "qc", "testing agency"],
    "construction equipment": ["equipment", "machine rental"],
    "portable toilets": ["portable toilet", "porta potty", "restroom rental"],
    "traffic control rental": ["traffic control", "k-rail", "cones", "flagging"],
    "parking stripping": ["striping", "stripping", "parking stripe"],
}

# Categories whose codes must never receive a vendor bill. These are seeded so
# the table mirrors BuilderTrend exactly, but marked inactive so they cannot be
# picked — a supplier invoice belongs to a cost code, not to a bid line or to
# a retention ledger account.
INACTIVE_CATEGORIES = {"Bids", "General", "Buildertrend Default"}

CODE_RE = re.compile(r"^([0-9][0-9.]*)\s*-\s*(.*)$")


def norm(s: str) -> str:
    """Normalize a cell: collapse non-breaking spaces and trim."""
    return " ".join(str(s).replace("\xa0", " ").split())


def sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def sql_text_array(items: list[str]) -> str:
    if not items:
        return "ARRAY[]::TEXT[]"
    return "ARRAY[" + ", ".join(sql_str(i) for i in items) + "]::TEXT[]"


def main(xlsx_path: str) -> None:
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb["Costs"]

    seen: set[str] = set()
    rows: list[tuple[str, str | None, str, str, list[str], bool]] = []
    unmapped: list[str] = []

    for category_cell, code_cell in ws.iter_rows(min_row=2, values_only=True):
        if not code_cell:
            continue
        category = norm(category_cell) if category_cell else ""
        code = norm(code_cell)
        if code in seen:
            continue
        seen.add(code)

        m = CODE_RE.match(code)
        if m:
            base_code: str | None = m.group(1)
            description = m.group(2).strip()
        else:
            # Non-numeric code such as "Site CMU" or "Retention".
            base_code = None
            description = code

        keywords = ELEMENT_KEYWORDS.get(description.lower())
        if keywords is None:
            keywords = []
            if category not in INACTIVE_CATEGORIES:
                unmapped.append(code)

        active = category not in INACTIVE_CATEGORIES
        rows.append((code, base_code, category, description, keywords, active))

    out = Path(__file__).resolve().parent.parent / "migrations" / "002_seed_cost_codes.sql"
    with out.open("w") as f:
        f.write(f"""-- ╔═══════════════════════════════════════════════════════════════════════╗
-- ║  Migration 002: Seed cost_codes from the BuilderTrend export           ║
-- ║                                                                        ║
-- ║  GENERATED FILE — do not hand-edit.                                    ║
-- ║  Source:    {Path(xlsx_path).name:<59}║
-- ║  Generator: scripts/gen_cost_code_seed.py                              ║
-- ║  Rows:      {len(rows):<59}║
-- ║                                                                        ║
-- ║  Idempotent: ON CONFLICT (code) DO UPDATE refreshes descriptions and   ║
-- ║  keywords in place, so re-applying never orphans an invoice_costs row. ║
-- ║                                                                        ║
-- ║  `active = FALSE` marks codes that exist in BuilderTrend but must      ║
-- ║  never receive a vendor bill: bid categories and the retention ledger  ║
-- ║  accounts. They are seeded for completeness and hidden from pickers.   ║
-- ╚═══════════════════════════════════════════════════════════════════════╝

INSERT INTO cost_codes (code, base_code, category, description, element_keywords, active)
VALUES
""")
        lines = []
        for code, base_code, category, description, keywords, active in rows:
            lines.append(
                "    ({}, {}, {}, {}, {}, {})".format(
                    sql_str(code),
                    sql_str(base_code) if base_code else "NULL",
                    sql_str(category) if category else "NULL",
                    sql_str(description),
                    sql_text_array(keywords),
                    "TRUE" if active else "FALSE",
                )
            )
        f.write(",\n".join(lines))
        f.write("""
ON CONFLICT (code) DO UPDATE SET
    base_code        = EXCLUDED.base_code,
    category         = EXCLUDED.category,
    description      = EXCLUDED.description,
    element_keywords = EXCLUDED.element_keywords,
    active           = EXCLUDED.active,
    updated_at       = NOW();
""")

    print(f"wrote {out} ({len(rows)} codes, {sum(1 for r in rows if not r[5])} inactive)")
    with_kw = sum(1 for r in rows if r[4])
    print(f"  {with_kw} codes carry element keywords")
    if unmapped:
        print(f"  {len(unmapped)} active codes have no keywords (expected for "
              f"non-concrete trades); first 10: {unmapped[:10]}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1])
