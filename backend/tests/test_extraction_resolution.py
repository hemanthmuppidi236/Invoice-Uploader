"""
Resolution of an AI extraction into real ids (prompt §7.2).

These functions decide whether an invoice routes for review or gets flagged,
and the flag code decides which triage bucket Linda finds it in. They are
pure given a context dict, so they are tested directly.
"""

from app.core.invoice_extractor import (
    CostCodeAlternative,
    InvoiceExtraction,
    ProjectMatch,
    SuggestedCostCode,
    parse_invoice_date,
    resolve_costs,
    resolve_project,
    resolve_vendor,
)

from datetime import date


COLUMNS = {
    "id": "cc-columns",
    "code": "3002 - Concrete Columns",
    "base_code": "3002",
    "description": "Concrete Columns",
    "element_keywords": ["column"],
}
WALLS = {
    "id": "cc-walls",
    "code": "3002 - Concrete Walls",
    "base_code": "3002",
    "description": "Concrete Walls",
    "element_keywords": ["wall", "shear wall"],
}
HARDWARE = {
    "id": "cc-hardware",
    "code": "3015 - Hardware & Misc",
    "base_code": "3015",
    "description": "Hardware & Misc",
    "element_keywords": ["hardware"],
}

CALPORTLAND = {
    "id": "v-cal",
    "invoice_name": "CalPortland",
    "bt_name": "Catalina Pacific",
    "aliases": ["Cal Portland", "CPC"],
    "default_cost_code_id": None,
    "is_concrete_supplier": True,
}
WHITECAP = {
    "id": "v-wc",
    "invoice_name": "White Cap",
    "bt_name": "White Cap Construction & Industrial Supply",
    "aliases": ["WhiteCap"],
    "default_cost_code_id": "cc-hardware",
    "is_concrete_supplier": False,
}

A_STREET = {
    "id": "p-astreet",
    "project_no": "25-20",
    "name": "A Street Flats",
    "address": "2935 A Street",
    "status": "old",
    "onboarded_at": "2026-09-01T00:00:00Z",
    "pe_user_id": "u-pe",
}
NEW_JOB = {
    "id": "p-new",
    "project_no": "26-04",
    "name": "Beacon",
    "address": None,
    "status": "active",
    "onboarded_at": "2026-09-01T00:00:00Z",
    "pe_user_id": "u-pe",
}
NOT_ONBOARDED = {
    "id": "p-raw",
    "project_no": "25-21",
    "name": "Melodia",
    "address": None,
    "status": "old",
    "onboarded_at": None,
    "pe_user_id": None,
}


def ctx():
    codes = [COLUMNS, WALLS, HARDWARE]
    return {
        "projects": [A_STREET, NEW_JOB, NOT_ONBOARDED],
        "vendors": [CALPORTLAND, WHITECAP],
        "codes": codes,
        "mix_rows": [],
        "recent": [],
        "code_labels": {c["id"]: c["code"] for c in codes},
        "code_ids": {c["code"]: c["id"] for c in codes},
    }


# ─── Vendor ───────────────────────────────────────────────────────────


def test_vendor_matches_on_the_buildertrend_name():
    e = InvoiceExtraction(
        vendor_name_on_invoice="CalPortland", vendor_bt_name="Catalina Pacific"
    )
    vendor, problem = resolve_vendor(e, ctx())
    assert vendor["id"] == "v-cal"
    assert problem is None


def test_vendor_matches_on_the_letterhead_name_alone():
    """The model may read the letterhead without knowing the BT mapping."""
    e = InvoiceExtraction(vendor_name_on_invoice="CalPortland")
    vendor, problem = resolve_vendor(e, ctx())
    assert vendor["id"] == "v-cal"
    assert problem is None


def test_vendor_matches_on_an_alias():
    e = InvoiceExtraction(vendor_name_on_invoice="Cal Portland")
    vendor, problem = resolve_vendor(e, ctx())
    assert vendor["id"] == "v-cal"


def test_vendor_match_is_case_and_whitespace_insensitive():
    e = InvoiceExtraction(vendor_bt_name="  catalina pacific  ")
    vendor, _ = resolve_vendor(e, ctx())
    assert vendor["id"] == "v-cal"


def test_unknown_vendor_is_a_problem_not_a_new_vendor():
    """Prompt §12: the app never creates a Sub/Vendor."""
    e = InvoiceExtraction(vendor_name_on_invoice="Some New Supplier")
    vendor, problem = resolve_vendor(e, ctx())
    assert vendor is None
    assert "Some New Supplier" in problem
    assert "never creates" in problem


def test_unreadable_vendor_is_a_problem():
    vendor, problem = resolve_vendor(InvoiceExtraction(), ctx())
    assert vendor is None
    assert "could not be read" in problem


# ─── Project ──────────────────────────────────────────────────────────


def test_project_matches_on_number():
    e = InvoiceExtraction(project_match=ProjectMatch(project_no="25-20", confidence=0.95))
    project, flag, _ = resolve_project(e, ctx())
    assert project["id"] == "p-astreet"
    assert flag is None


def test_project_matches_on_name():
    e = InvoiceExtraction(
        project_match=ProjectMatch(project_no="A Street Flats", confidence=0.9)
    )
    project, flag, _ = resolve_project(e, ctx())
    assert project["id"] == "p-astreet"
    assert flag is None


def test_a_new_job_is_flagged_and_not_routed():
    """Prompt §4.1: only `old` projects are in scope."""
    e = InvoiceExtraction(project_match=ProjectMatch(project_no="26-04", confidence=0.99))
    project, flag, problem = resolve_project(e, ctx())
    assert project["id"] == "p-new"
    assert flag == "new_project"
    assert "separate workflow" in problem


def test_a_project_without_onboarding_is_flagged():
    e = InvoiceExtraction(project_match=ProjectMatch(project_no="25-21", confidence=0.99))
    project, flag, problem = resolve_project(e, ctx())
    assert flag == "project_not_onboarded"
    assert "project engineer" in problem


def test_a_weak_match_is_treated_as_no_match():
    """A confident-looking guess on the wrong job is the expensive failure."""
    e = InvoiceExtraction(
        project_match=ProjectMatch(project_no="25-20", confidence=0.3, evidence="guessed")
    )
    project, flag, problem = resolve_project(e, ctx())
    assert project is None
    assert flag == "unknown_project"
    assert "30%" in problem


def test_a_project_not_in_the_list_is_flagged_unknown():
    e = InvoiceExtraction(project_match=ProjectMatch(project_no="99-99", confidence=0.99))
    project, flag, problem = resolve_project(e, ctx())
    assert project is None
    assert flag == "unknown_project"
    assert "99-99" in problem


def test_no_project_read_at_all_is_flagged_unknown():
    project, flag, _ = resolve_project(InvoiceExtraction(), ctx())
    assert project is None
    assert flag == "unknown_project"


def test_a_match_with_no_confidence_reported_is_accepted():
    """Absent confidence is not low confidence — the model just did not say."""
    e = InvoiceExtraction(project_match=ProjectMatch(project_no="25-20"))
    project, flag, _ = resolve_project(e, ctx())
    assert project["id"] == "p-astreet"
    assert flag is None


# ─── Cost codes ───────────────────────────────────────────────────────


def test_a_suggested_code_resolves_to_its_id():
    e = InvoiceExtraction(
        cost_codes=[
            SuggestedCostCode(
                code="3002 - Concrete Columns", amount=1000.0, confidence=0.9
            )
        ]
    )
    rows, warnings = resolve_costs(e, ctx())
    assert len(rows) == 1
    assert rows[0].cost_code_id == "cc-columns"
    assert rows[0].code == "3002 - Concrete Columns"
    assert warnings == []


def test_an_invented_code_is_dropped_not_created():
    """Prompt §12: the app never creates a cost code."""
    e = InvoiceExtraction(
        cost_codes=[SuggestedCostCode(code="3002 - Concrete Balconies", amount=500.0)]
    )
    rows, warnings = resolve_costs(e, ctx())
    assert rows == []
    assert any("not a known cost code" in w for w in warnings)


def test_a_bare_base_number_is_not_accepted_as_a_code():
    """BuilderTrend's Costs row needs the full sub-code, not '3002'."""
    e = InvoiceExtraction(cost_codes=[SuggestedCostCode(code="3002", amount=500.0)])
    rows, warnings = resolve_costs(e, ctx())
    assert rows == []
    assert warnings


def test_code_resolution_tolerates_case_drift():
    e = InvoiceExtraction(
        cost_codes=[SuggestedCostCode(code="3002 - concrete columns", amount=1.0)]
    )
    rows, _ = resolve_costs(e, ctx())
    assert len(rows) == 1
    assert rows[0].cost_code_id == "cc-columns"


def test_a_split_across_two_codes_keeps_both():
    e = InvoiceExtraction(
        cost_codes=[
            SuggestedCostCode(code="3002 - Concrete Columns", amount=600.0),
            SuggestedCostCode(code="3002 - Concrete Walls", amount=400.0),
        ]
    )
    rows, warnings = resolve_costs(e, ctx())
    assert [r.cost_code_id for r in rows] == ["cc-columns", "cc-walls"]
    assert warnings == []


def test_the_same_code_suggested_twice_is_collapsed_to_one():
    """UNIQUE(invoice_id, cost_code_id) would otherwise 500 on insert."""
    e = InvoiceExtraction(
        cost_codes=[
            SuggestedCostCode(code="3002 - Concrete Columns", amount=600.0),
            SuggestedCostCode(code="3002 - Concrete Columns", amount=400.0),
        ]
    )
    rows, warnings = resolve_costs(e, ctx())
    assert len(rows) == 1
    assert any("more than once" in w for w in warnings)


def test_alternatives_resolve_so_a_one_click_chip_cannot_be_broken():
    """§10 renders alternatives as one-click chips. A chip pointing at a code
    that does not exist would fail on click, after the reviewer committed."""
    e = InvoiceExtraction(
        cost_codes=[
            SuggestedCostCode(
                code="3002 - Concrete Columns",
                amount=1000.0,
                confidence=0.7,
                alternatives=[
                    CostCodeAlternative(
                        code="3002 - Concrete Walls",
                        why="mix 6011000 is the 6000 psi shear wall mix",
                    ),
                    CostCodeAlternative(code="Not A Real Code", why="nonsense"),
                ],
            )
        ]
    )
    rows, warnings = resolve_costs(e, ctx())
    assert len(rows[0].alternatives) == 1
    assert rows[0].alternatives[0]["cost_code_id"] == "cc-walls"
    assert "6011000" in rows[0].alternatives[0]["why"]
    assert any("Alternative code" in w for w in warnings)


def test_no_suggestion_at_all_warns_so_the_reviewer_is_told():
    rows, warnings = resolve_costs(InvoiceExtraction(), ctx())
    assert rows == []
    assert any("must pick one" in w for w in warnings)


def test_the_gate_scenario_comment_wins_mix_becomes_the_alternative():
    """Prompt §8 worked example, and the Phase 1 gate: the invoice comment
    says 3002 column while mix 6011000 is the shear-wall mix. The comment is
    the suggestion at reduced confidence; the mix-derived code is first in
    alternatives."""
    e = InvoiceExtraction(
        vendor_name_on_invoice="CalPortland",
        vendor_bt_name="Catalina Pacific",
        project_match=ProjectMatch(project_no="25-20", confidence=0.95),
        comment_hint="Cost code:3002 column",
        mix_nos=["6011000"],
        cost_codes=[
            SuggestedCostCode(
                code="3002 - Concrete Columns",
                amount=2294.25,
                confidence=0.7,
                rationale="Handwritten comment on the invoice reads 'Cost code:3002 column'.",
                alternatives=[
                    CostCodeAlternative(
                        code="3002 - Concrete Walls",
                        why="Mix 6011000 is the 6000 psi shear wall mix, which conflicts with the comment.",
                    )
                ],
            )
        ],
    )
    c = ctx()
    vendor, vendor_problem = resolve_vendor(e, c)
    project, flag, _ = resolve_project(e, c)
    rows, warnings = resolve_costs(e, c)

    assert vendor["bt_name"] == "Catalina Pacific"
    assert vendor_problem is None
    assert project["project_no"] == "25-20"
    assert flag is None
    assert rows[0].code == "3002 - Concrete Columns"
    assert 0.6 <= rows[0].confidence < 0.85, "conflict should be medium confidence"
    assert rows[0].alternatives[0]["code"] == "3002 - Concrete Walls"
    assert warnings == []


# ─── Date parsing ─────────────────────────────────────────────────────


def test_iso_date_parses():
    assert parse_invoice_date("2026-09-02") == date(2026, 9, 2)


def test_us_date_formats_are_tolerated():
    assert parse_invoice_date("09/02/2026") == date(2026, 9, 2)
    assert parse_invoice_date("9/2/26") == date(2026, 9, 2)
    assert parse_invoice_date("09-02-2026") == date(2026, 9, 2)


def test_an_unparseable_date_is_none_not_today():
    """The due date is derived from this, so a default would silently produce
    a wrong due date instead of a flag."""
    assert parse_invoice_date("last Tuesday") is None
    assert parse_invoice_date("") is None
    assert parse_invoice_date(None) is None
