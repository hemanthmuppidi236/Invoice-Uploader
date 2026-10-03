"""
AI acceptance metrics (prompt §12).

"Every AI suggestion and every human edit is stored. Add a small /metrics
page showing AI acceptance rate by cost code and vendor."

The question underneath is whether to keep trusting the suggestion. The
answers that would mislead somebody into the wrong conclusion are what these
tests pin: counting an invoice the model never made a claim about as a miss,
scoring a split invoice as half-wrong, and letting a single-invoice vendor at
0% outrank the vendor with forty invoices at 60%.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.core import metrics
from tests.fakes import FakeClient, FakeTable


RECENT = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
OLD = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()


@pytest.fixture
def db(monkeypatch):
    client = FakeClient()
    for name in ("invoices", "invoice_suggestions", "invoice_costs"):
        client.tables[name] = FakeTable(name, [])
    client.tables["vendors"] = FakeTable(
        "vendors",
        [
            {"id": "v-cal", "invoice_name": "CalPortland"},
            {"id": "v-wc", "invoice_name": "White Cap"},
        ],
    )
    client.tables["cost_codes"] = FakeTable(
        "cost_codes",
        [
            {"id": "c-walls", "code": "3002 - Concrete Walls"},
            {"id": "c-sog", "code": "3008 - Slab on Grade"},
            {"id": "c-hw", "code": "3015 - Hardware & Misc"},
        ],
    )
    monkeypatch.setattr(metrics, "_sb", lambda: client)
    return client


def _invoice(db, inv_id, *, vendor="v-cal", approved=RECENT, status="approved"):
    db.tables["invoices"].rows.append(
        {"id": inv_id, "vendor_id": vendor, "approved_at": approved, "status": status}
    )


def _suggested(db, inv_id, code, *, confidence=0.9, at=RECENT):
    db.tables["invoice_suggestions"].rows.append(
        {
            "invoice_id": inv_id,
            "cost_code_id": code,
            "confidence": confidence,
            "created_at": at,
        }
    )


def _kept(db, inv_id, *codes):
    for code in codes:
        db.tables["invoice_costs"].rows.append(
            {"invoice_id": inv_id, "cost_code_id": code}
        )


# ─── What counts as accepted ──────────────────────────────────────────


def test_a_suggestion_that_survived_to_approval_counts_as_accepted(db):
    _invoice(db, "i1")
    _suggested(db, "i1", "c-walls")
    _kept(db, "i1", "c-walls")

    report = metrics.build_report()
    assert report.judged == 1
    assert report.accepted == 1
    assert report.rate == 1.0


def test_a_suggestion_the_reviewer_replaced_counts_as_a_miss(db):
    _invoice(db, "i1")
    _suggested(db, "i1", "c-walls")
    _kept(db, "i1", "c-sog")

    report = metrics.build_report()
    assert report.judged == 1
    assert report.accepted == 0


def test_a_split_invoice_is_not_scored_as_half_right(db):
    """The reviewer agreed with the read and then split the money across two
    codes. Counting cost rows would score that 50%; counting invoices gets it
    right."""
    _invoice(db, "i1")
    _suggested(db, "i1", "c-walls")
    _kept(db, "i1", "c-walls", "c-sog")

    report = metrics.build_report()
    assert report.judged == 1
    assert report.accepted == 1


def test_an_invoice_with_no_suggestion_is_excluded_not_counted_as_a_miss(db):
    """Flagged, hand-coded, or ingested before the AI was configured. The
    model never made a claim, so scoring it against one invents a failure."""
    _invoice(db, "i1")
    _kept(db, "i1", "c-walls")

    report = metrics.build_report()
    assert report.judged == 0
    assert report.unsuggested == 1
    assert any("excluded rather than counted as misses" in n for n in report.notes)


def test_only_the_newest_suggestion_is_judged(db):
    """Suggestions are append-only — a retry adds a row — so the newest is
    the claim that was standing when the invoice was approved."""
    _invoice(db, "i1")
    _suggested(db, "i1", "c-sog", at="2026-01-01T00:00:00+00:00")
    _suggested(db, "i1", "c-walls", at="2026-09-01T00:00:00+00:00")
    _kept(db, "i1", "c-walls")

    report = metrics.build_report()
    assert report.accepted == 1


def test_unapproved_invoices_are_not_scored(db):
    """Nobody has judged them yet, so counting them would measure the model
    against itself."""
    _invoice(db, "i1", status="assigned")
    _suggested(db, "i1", "c-walls")
    _kept(db, "i1", "c-walls")

    report = metrics.build_report()
    assert report.judged == 0


def test_invoices_outside_the_window_are_not_scored(db):
    _invoice(db, "i1", approved=OLD)
    _suggested(db, "i1", "c-walls")
    _kept(db, "i1", "c-walls")

    assert metrics.build_report(window_days=90).judged == 0


# ─── Breakdowns ───────────────────────────────────────────────────────


def test_acceptance_is_broken_down_by_vendor(db):
    _invoice(db, "i1", vendor="v-wc")
    _suggested(db, "i1", "c-hw")
    _kept(db, "i1", "c-hw")
    _invoice(db, "i2", vendor="v-cal")
    _suggested(db, "i2", "c-walls")
    _kept(db, "i2", "c-sog")

    by_vendor = {b.label: b for b in metrics.build_report().by_vendor}
    assert by_vendor["White Cap"].rate == 1.0
    assert by_vendor["CalPortland"].rate == 0.0


def test_codes_are_bucketed_by_what_the_ai_said_not_what_was_kept(db):
    """The question is "when it says 3002, is it right". Bucketing by the
    final code answers a different one."""
    _invoice(db, "i1")
    _suggested(db, "i1", "c-walls")
    _kept(db, "i1", "c-sog")

    labels = {b.label for b in metrics.build_report().by_cost_code}
    assert labels == {"3002 - Concrete Walls"}


def test_confidence_bands_are_reported(db):
    _invoice(db, "i1")
    _suggested(db, "i1", "c-walls", confidence=0.95)
    _kept(db, "i1", "c-walls")
    _invoice(db, "i2")
    _suggested(db, "i2", "c-walls", confidence=0.2)
    _kept(db, "i2", "c-sog")

    bands = {b.key: b for b in metrics.build_report().by_confidence}
    assert bands["high"].rate == 1.0
    assert bands["low"].rate == 0.0


def test_a_confidence_number_that_predicts_nothing_is_called_out(db):
    """If high is accepted no more often than low, the number is decoration
    — and every UI decision resting on it is resting on nothing."""
    for n in range(12):
        _invoice(db, f"hi{n}")
        _suggested(db, f"hi{n}", "c-walls", confidence=0.95)
        _kept(db, f"hi{n}", "c-walls" if n < 6 else "c-sog")
    for n in range(12):
        _invoice(db, f"lo{n}")
        _suggested(db, f"lo{n}", "c-walls", confidence=0.2)
        _kept(db, f"lo{n}", "c-walls" if n < 6 else "c-sog")

    notes = " ".join(metrics.build_report().notes)
    assert "resting on nothing" in notes


def test_a_confidence_number_that_works_says_so(db):
    for n in range(12):
        _invoice(db, f"hi{n}")
        _suggested(db, f"hi{n}", "c-walls", confidence=0.95)
        _kept(db, f"hi{n}", "c-walls")
    for n in range(12):
        _invoice(db, f"lo{n}")
        _suggested(db, f"lo{n}", "c-walls", confidence=0.2)
        _kept(db, f"lo{n}", "c-sog")

    notes = " ".join(metrics.build_report().notes)
    assert "Confidence is doing its job" in notes


def test_a_weak_cost_code_is_named_with_the_fix(db):
    for n in range(6):
        _invoice(db, f"i{n}")
        _suggested(db, f"i{n}", "c-walls")
        _kept(db, f"i{n}", "c-sog")

    notes = " ".join(metrics.build_report().notes)
    assert "3002 - Concrete Walls" in notes
    assert "element keywords" in notes


def test_a_single_invoice_at_zero_does_not_outrank_a_real_problem(db):
    """Sorting noise to the top buries the vendor actually worth fixing."""
    _invoice(db, "one-off", vendor="v-wc")
    _suggested(db, "one-off", "c-hw")
    _kept(db, "one-off", "c-sog")
    for n in range(10):
        _invoice(db, f"cal{n}", vendor="v-cal")
        _suggested(db, f"cal{n}", "c-walls")
        _kept(db, f"cal{n}", "c-walls" if n < 6 else "c-sog")

    assert metrics.build_report().by_vendor[0].label == "CalPortland"


# ─── Nothing to report ────────────────────────────────────────────────


def test_an_empty_system_says_so_rather_than_showing_zero_percent(db):
    report = metrics.build_report()
    assert report.judged == 0
    assert report.rate is None
    assert any("nothing to score" in n for n in report.notes)


def test_a_bucket_with_no_data_has_no_rate(db):
    """0% would read as the model failing. None reads as no data."""
    assert metrics.Bucket(key="x", label="X").rate is None
