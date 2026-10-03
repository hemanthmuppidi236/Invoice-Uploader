"""
Intake idempotency and duplicate detection (prompt §7.1, §7.2).

The poll runs every 15 minutes over a folder whose files are NOT moved until
Phase 3, so it re-reads the same PDFs constantly. Re-ingesting one would
create a second payable for the same invoice — the failure mode that shows up
as a double payment, not as an error. This is the property most worth testing
and least likely to be noticed if it breaks.

Uses a small fake in place of the Supabase client: the real chain is fluent
(`.table().select().eq().limit().execute()`), and faking it is cheaper than
standing up Postgres for what is ultimately a branch on "did we see this
file id before".
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.core import intake


from tests.fakes import FakeClient, FakeQuery, FakeResult, FakeTable  # noqa: F401


@pytest.fixture
def db(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(intake, "_sb", lambda: client)
    # audit writes through its own client; point it at the same fake so
    # logging does not reach the network.
    from app.core import audit

    monkeypatch.setattr(audit, "get_service_client", lambda: client)
    return client


# ─── Idempotency ──────────────────────────────────────────────────────


def test_a_file_already_ingested_is_recognised_not_reingested(db, monkeypatch):
    """The same Drive file on a second poll must not create a second row."""
    db.tables["invoices"] = FakeTable(
        "invoices",
        [{"id": "inv-1", "source_file_id": "drive-abc", "source_page": 1,
          "status": "suggested"}],
    )

    outcome = intake._process_file(
        {"id": "drive-abc", "name": "CalPortland 278461.pdf"},
        source_label="Invoice Uploads",
        actor=intake.SYSTEM_ACTOR,
    )

    assert outcome["outcome"] == "existing"
    assert outcome["invoice_id"] == "inv-1"
    assert len(db.tables["invoices"].rows) == 1, "must not insert a second row"


def test_a_new_file_claims_its_row_before_the_slow_work(db, monkeypatch):
    """The row is inserted BEFORE the download. Two overlapping cron firings
    would otherwise both download and both insert, because the uniqueness
    guarantee is the row, not the file."""
    order: list[str] = []

    def fake_download(_file_id):
        order.append("download")
        raise RuntimeError("network down")

    monkeypatch.setattr(intake.drive, "download", fake_download)
    db.tables["invoices"] = FakeTable("invoices", [])

    outcome = intake._process_file(
        {"id": "drive-new", "name": "invoice.pdf"},
        source_label="Invoice Uploads",
        actor=intake.SYSTEM_ACTOR,
    )

    rows = db.tables["invoices"].rows
    assert len(rows) == 1, "the row must exist even though the download failed"
    assert rows[0]["source_file_id"] == "drive-new"
    assert outcome["outcome"] == "flagged"
    assert order == ["download"]


def test_a_failed_download_flags_rather_than_vanishing(db, monkeypatch):
    """§7.1: fail loudly. A silently skipped invoice is an unpaid vendor."""
    monkeypatch.setattr(
        intake.drive, "download", lambda _f: (_ for _ in ()).throw(RuntimeError("403"))
    )
    db.tables["invoices"] = FakeTable("invoices", [])

    intake._process_file(
        {"id": "drive-x", "name": "x.pdf"},
        source_label="Invoice Uploads",
        actor=intake.SYSTEM_ACTOR,
    )

    row = db.tables["invoices"].rows[0]
    assert row["status"] == "flagged"
    assert row["flag_code"] == "unreadable"
    assert "403" in row["flag_detail"]


def test_a_combined_white_cap_file_is_split_not_misread(db, monkeypatch):
    """A 12-page multi-job bundle read as one invoice would produce one
    confident five-figure total on one wrong job. SOP §5: one page is one
    invoice. The per-page behaviour is covered in test_white_cap_split.py;
    this pins that intake reaches the splitter at all."""
    from tests.helpers import make_pdf

    monkeypatch.setattr(intake.drive, "download", lambda _f: make_pdf(3))
    monkeypatch.setattr(intake.storage, "upload_bytes", lambda *a, **k: "path.pdf")
    monkeypatch.setattr(intake, "apply_extraction", lambda *a, **k: "suggested")
    db.tables["invoices"] = FakeTable("invoices", [])

    outcome = intake._process_file(
        {"id": "drive-wc", "name": "White Cap combined.pdf"},
        source_label="White Cap",
        actor=intake.SYSTEM_ACTOR,
    )

    assert outcome["outcome"] == "split"
    assert outcome["pages"] == 3
    # One row per page, sharing the file id — the (source_file_id,
    # source_page) key from migration 001 is what makes re-polling re-find
    # every page rather than duplicating any of them.
    rows = db.tables["invoices"].rows
    assert len(rows) == 3
    assert {r["source_page"] for r in rows} == {1, 2, 3}
    assert {r["source_file_id"] for r in rows} == {"drive-wc"}


def test_a_single_page_white_cap_file_skips_the_splitter(db, monkeypatch):
    """A one-page White Cap file is just an invoice. Routing it through the
    splitter would add a layer to debug through for no gain."""
    from tests.helpers import make_pdf

    monkeypatch.setattr(intake.drive, "download", lambda _f: make_pdf(1))
    monkeypatch.setattr(intake.storage, "upload_bytes", lambda *a, **k: "path.pdf")
    monkeypatch.setattr(intake, "apply_extraction", lambda *a, **k: "suggested")
    db.tables["invoices"] = FakeTable("invoices", [])

    outcome = intake._process_file(
        {"id": "drive-wc-1", "name": "one page.pdf"},
        source_label="White Cap",
        actor=intake.SYSTEM_ACTOR,
    )
    assert outcome["outcome"] == "suggested"
    assert len(db.tables["invoices"].rows) == 1


def test_an_already_split_white_cap_invoice_is_processed_normally(db, monkeypatch):
    """A file inside White Cap/[Job Name]/ is one invoice and goes through
    extraction like any other."""
    monkeypatch.setattr(intake.drive, "download", lambda _f: b"%PDF-1.4 fake")
    monkeypatch.setattr(intake.storage, "upload_bytes", lambda *a, **k: "path.pdf")
    monkeypatch.setattr(intake, "apply_extraction", lambda *a, **k: "suggested")
    db.tables["invoices"] = FakeTable("invoices", [])

    outcome = intake._process_file(
        {
            "id": "drive-wc-split",
            "name": "278461.pdf",
            "parent_folder_name": "A Street Flats",
        },
        source_label="White Cap",
        actor=intake.SYSTEM_ACTOR,
    )

    assert outcome["outcome"] == "suggested"
    row = db.tables["invoices"].rows[0]
    assert row["source_path"] == "White Cap/A Street Flats/278461.pdf"


# ─── Flagging (§7.2) ──────────────────────────────────────────────────


def test_flagging_remembers_the_prior_status_so_unflag_can_restore_it(db):
    db.tables["invoices"] = FakeTable(
        "invoices", [{"id": "inv-1", "status": "assigned"}]
    )
    intake.flag_invoice(
        "inv-1", code="unreadable", detail="scan too dark",
        current_status="assigned",
    )
    row = db.tables["invoices"].rows[0]
    assert row["status"] == "flagged"
    assert row["status_before_flag"] == "assigned"
    assert row["flag_code"] == "unreadable"


def test_reflagging_does_not_overwrite_the_original_prior_status(db):
    """Otherwise a second flag would record 'flagged' as the state to
    restore, and unflag would leave the invoice stuck."""
    db.tables["invoices"] = FakeTable(
        "invoices",
        [{"id": "inv-1", "status": "flagged", "status_before_flag": "assigned"}],
    )
    intake.flag_invoice(
        "inv-1", code="other", detail="second flag", current_status="flagged",
    )
    assert db.tables["invoices"].rows[0]["status_before_flag"] == "assigned"


def test_a_very_long_error_is_truncated_rather_than_rejected_by_the_column(db):
    db.tables["invoices"] = FakeTable("invoices", [{"id": "inv-1", "status": "ingested"}])
    intake.flag_invoice(
        "inv-1", code="ai_error", detail="x" * 5000, current_status="ingested",
    )
    assert len(db.tables["invoices"].rows[0]["flag_detail"]) == 2000
