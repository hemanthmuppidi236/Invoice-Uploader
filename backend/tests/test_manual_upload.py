"""
The manual upload path (prompt §7.1, the non-Drive door).

The Drive poll is the normal way an invoice enters. This is the other one,
and the thing it must not become is a second workflow: everything after the
PDF reaches Storage has to be the same pipeline, or there are two sets of
rules about how an invoice gets coded and only one of them is tested.

The property worth the most here is idempotency. The poll keys on the Drive
file id; an upload has no file id, so the bytes are the identity. Getting
that wrong does not produce an error — it produces a second payable for an
invoice that is already in the queue, and nobody finds that out until the
vendor is paid twice.
"""

import hashlib

import pytest

from app.core import intake
from app.core.auth import Actor, CurrentUser
from app.core.invoice_rules import UPLOAD_SOURCE_PREFIX, is_uploaded_source
from tests.fakes import FakeClient, FakeTable


PDF = b"%PDF-1.4 a scanned CalPortland invoice"
LINDA = Actor(
    user=CurrentUser(id="u-linda", email="linda@f.com", roles=["accountant"])
)


@pytest.fixture
def db(monkeypatch):
    client = FakeClient()
    client.tables["invoices"] = FakeTable("invoices", [])
    monkeypatch.setattr(intake, "_sb", lambda: client)

    from app.core import audit

    monkeypatch.setattr(audit, "get_service_client", lambda: client)
    monkeypatch.setattr(
        intake.storage, "upload_bytes", lambda bucket, path, data, **kw: path
    )
    # The AI is off, which is a legitimate configuration — the invoice should
    # still land, stored and visible.
    monkeypatch.setattr(intake.settings, "anthropic_api_key", None)
    return client


def _rows(db):
    return db.tables["invoices"].rows


# ─── Idempotency ──────────────────────────────────────────────────────


def test_the_same_pdf_twice_does_not_create_a_second_payable(db):
    """The failure this prevents is a double payment, which shows up as money
    leaving the company rather than as an error in a log."""
    first = intake.ingest_upload(
        filename="invoice.pdf", pdf_bytes=PDF, actor=LINDA
    )
    second = intake.ingest_upload(
        filename="invoice-copy.pdf", pdf_bytes=PDF, actor=LINDA
    )

    assert second["outcome"] == "existing"
    assert second["invoice_id"] == first["invoice_id"]
    assert len(_rows(db)) == 1


def test_a_different_pdf_is_a_different_invoice(db):
    a = intake.ingest_upload(filename="a.pdf", pdf_bytes=PDF, actor=LINDA)
    b = intake.ingest_upload(
        filename="b.pdf", pdf_bytes=PDF + b" second", actor=LINDA
    )
    assert a["invoice_id"] != b["invoice_id"]
    assert len(_rows(db)) == 2


def test_the_identity_is_the_content_not_the_filename(db):
    """Renaming the attachment before re-sending it is the ordinary case, not
    an edge case. Keying on the filename would let it through."""
    intake.ingest_upload(filename="scan001.pdf", pdf_bytes=PDF, actor=LINDA)
    result = intake.ingest_upload(
        filename="CalPortland Sept.pdf", pdf_bytes=PDF, actor=LINDA
    )
    assert result["outcome"] == "existing"


def test_the_source_id_is_a_hash_of_the_bytes(db):
    intake.ingest_upload(filename="a.pdf", pdf_bytes=PDF, actor=LINDA)
    expected = UPLOAD_SOURCE_PREFIX + hashlib.sha256(PDF).hexdigest()
    assert _rows(db)[0]["source_file_id"] == expected


def test_a_concurrent_duplicate_insert_reads_back_the_winner(db, monkeypatch):
    """Two people forwarding the same attachment at the same moment. The
    unique index decides it; the loser must return the winner's invoice
    rather than a 500."""
    intake.ingest_upload(filename="a.pdf", pdf_bytes=PDF, actor=LINDA)

    original_select = db.tables["invoices"].select
    calls = {"n": 0}

    def racy_select(*a):
        # Miss on the first lookup, as if the other insert had not committed
        # yet, so the insert below is the one that collides.
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeTable("invoices", []).select(*a)
        return original_select(*a)

    monkeypatch.setattr(db.tables["invoices"], "select", racy_select)
    monkeypatch.setattr(
        db.tables["invoices"],
        "insert",
        lambda payload: (_ for _ in ()).throw(
            Exception('duplicate key value violates unique constraint "x" 23505')
        ),
    )

    result = intake.ingest_upload(filename="a.pdf", pdf_bytes=PDF, actor=LINDA)
    assert result["outcome"] == "existing"


# ─── The row it creates ───────────────────────────────────────────────


def test_the_row_is_claimed_before_the_pdf_is_stored(db, monkeypatch):
    """Same ordering as the poll. The unique index is what makes this
    idempotent, so it has to be claimed before any slow work."""
    order = []
    monkeypatch.setattr(
        intake.storage,
        "upload_bytes",
        lambda bucket, path, data, **kw: order.append("store") or path,
    )
    original_insert = db.tables["invoices"].insert

    def tracked_insert(payload):
        order.append("insert")
        return original_insert(payload)

    monkeypatch.setattr(db.tables["invoices"], "insert", tracked_insert)

    intake.ingest_upload(filename="a.pdf", pdf_bytes=PDF, actor=LINDA)
    assert order == ["insert", "store"]


def test_the_upload_is_marked_as_hand_entered(db):
    intake.ingest_upload(filename="a.pdf", pdf_bytes=PDF, actor=LINDA)
    row = _rows(db)[0]
    assert is_uploaded_source(row["source_file_id"])
    assert row["source_path"] == "Uploaded by hand/a.pdf"
    assert row["source_filename"] == "a.pdf"


def test_who_uploaded_it_is_in_the_audit_trail(db):
    """A second door into the ledger has to say who opened it."""
    intake.ingest_upload(filename="a.pdf", pdf_bytes=PDF, actor=LINDA)
    entries = db.tables["audit_log"].rows
    assert entries
    assert entries[0]["actor_label"] == "linda@f.com"
    assert entries[0]["diff"]["source"] == "manual upload"


def test_no_ai_key_flags_with_a_reason_rather_than_stalling(db):
    """Not having a Claude key is a legitimate configuration. The invoice is
    stored and visible either way; what it must not be is stuck in `ingested`
    with nothing on screen explaining why."""
    result = intake.ingest_upload(filename="a.pdf", pdf_bytes=PDF, actor=LINDA)
    assert result["outcome"] == "flagged"
    row = _rows(db)[0]
    assert row["flag_code"] == "ai_error"
    assert "ANTHROPIC_API_KEY" in row["flag_detail"]
    assert row["pdf_storage_path"]


def test_a_storage_failure_flags_rather_than_vanishing(db, monkeypatch):
    def boom(bucket, path, data, **kw):
        raise RuntimeError("bucket not found")

    monkeypatch.setattr(intake.storage, "upload_bytes", boom)
    result = intake.ingest_upload(filename="a.pdf", pdf_bytes=PDF, actor=LINDA)

    assert result["outcome"] == "flagged"
    assert _rows(db)[0]["flag_code"] == "unreadable"
    # The row survives, so the failure is visible in /flagged rather than
    # being a PDF that was dropped in and never appeared.
    assert len(_rows(db)) == 1


def test_an_uploaded_invoice_is_not_a_drive_invoice(db):
    assert is_uploaded_source("upload:abc") is True
    assert is_uploaded_source("1AbCdEfGhIjK") is False
    assert is_uploaded_source(None) is False
