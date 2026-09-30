"""
The §7.0 gate. A project that slips past this starts routing invoices with no
PE to review them or no mix design to score concrete against.
"""

from app.api.projects import onboarding_blockers, onboarding_status


PE = "11111111-1111-1111-1111-111111111111"


def _project(**over):
    base = {
        "id": "p1",
        "pe_user_id": PE,
        "has_concrete_supplier": True,
        "onboarded_at": None,
    }
    base.update(over)
    return base


def _live_row(cost_code_id=None):
    return {"superseded_at": None, "cost_code_id": cost_code_id}


def test_pe_and_mix_design_passes():
    assert onboarding_blockers(_project(), [_live_row("cc1")]) == []


def test_missing_pe_blocks():
    blockers = onboarding_blockers(_project(pe_user_id=None), [_live_row("cc1")])
    assert len(blockers) == 1
    assert "project engineer" in blockers[0]


def test_concrete_project_without_mix_design_blocks():
    blockers = onboarding_blockers(_project(), [])
    assert len(blockers) == 1
    assert "mix design" in blockers[0].lower()


def test_non_concrete_project_needs_no_mix_design():
    assert onboarding_blockers(_project(has_concrete_supplier=False), []) == []


def test_only_superseded_rows_counts_as_no_mix_design():
    """A superseded revision is history, not a live mapping."""
    superseded = [{"superseded_at": "2026-09-01T00:00:00Z", "cost_code_id": "cc1"}]
    blockers = onboarding_blockers(_project(), superseded)
    assert len(blockers) == 1
    assert "mix design" in blockers[0].lower()


def test_unmapped_rows_do_not_block_but_are_reported():
    """Prompt §7.0 gates on 'a mix design is confirmed', not on every row
    being mapped — a submittal listing mixes the job never pours should not
    hold onboarding hostage. The count is surfaced instead."""
    rows = [_live_row("cc1"), _live_row(None), _live_row(None)]
    assert onboarding_blockers(_project(), rows) == []
    status = onboarding_status(_project(), rows)
    assert status.mix_design_rows == 3
    assert status.mix_design_rows_mapped == 1


def test_both_blockers_reported_together():
    blockers = onboarding_blockers(_project(pe_user_id=None), [])
    assert len(blockers) == 2


def test_status_reflects_onboarded_stamp():
    assert onboarding_status(_project(), [_live_row("cc1")]).onboarded is False
    stamped = _project(onboarded_at="2026-09-29T00:00:00Z")
    assert onboarding_status(stamped, [_live_row("cc1")]).onboarded is True
