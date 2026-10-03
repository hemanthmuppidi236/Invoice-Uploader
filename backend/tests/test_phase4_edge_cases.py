"""
The §13 edge cases, end to end.

"Before each gate, do a senior-engineer review pass: ... edge cases (credit
memos, multi-page invoices, zero-amount invoices, same invoice re-uploaded to
Drive)."

Each of these is handled somewhere in the code and asserted somewhere in the
suite, but scattered across the module that happens to own it. The spec names
them as a checklist, so this is the checklist: one place that fails loudly if
any of the four regress, whichever module causes it.
"""

from decimal import Decimal

import pytest

from app.core import duplicates, intake, invoice_rules, pdf_split
from app.core.auth import Actor
from tests.fakes import FakeClient, FakeTable
from tests.helpers import make_pdf


D = Decimal
AGENT = Actor(is_agent=True)


# ─── Credit memos ─────────────────────────────────────────────────────


def test_a_credit_memo_is_stored_negative_whatever_the_pdf_printed():
    """SOP §5: enter it as a normal bill with the negative amount. Vendors
    are inconsistent about whether the printed total carries the minus, so
    the sign is made canonical on the way in rather than trusted."""
    assert invoice_rules.signed_amount(D("500.00"), True) == D("-500.00")
    assert invoice_rules.signed_amount(D("-500.00"), True) == D("-500.00")
    assert invoice_rules.signed_amount(D("500.00"), False) == D("500.00")


def test_a_credit_memo_is_not_a_near_duplicate_of_what_it_reverses():
    """A -$500 credit and a $500 invoice are opposites. Treating them as
    near-duplicates would stop every credit memo in the queue."""
    assert duplicates.amounts_match(D("-500.00"), D("500.00")) is False


def test_a_credit_balances_against_a_negative_split():
    balanced, diff = invoice_rules.costs_balance(D("-500.00"), [D("-500.00")])
    assert balanced and diff == 0


def test_a_credit_is_filed_with_a_minus_not_parentheses():
    """The vendor folder is browsed by humans looking for an amount, and
    -$500.00 reads as a credit at a glance where ($500.00) looks like a
    filename quirk."""
    from datetime import date

    from app.core import filing

    name = filing.build_filename(
        convention="MM-DD-YY $amount",
        invoice_date=date(2026, 9, 14),
        amount=D("-500.00"),
    )
    assert name == "09-14-26 -$500.00.pdf"


def test_a_credit_memos_share_of_the_bill_title_is_measured_by_size(monkeypatch):
    """Summing signed amounts would let a credit line cancel out the very
    code it belongs to, and the Bill Title would land on the wrong base."""
    from app.api import invoices as api
    from app.schemas.invoices import UploadQueueCost

    title, _ = api._bill_title(
        [
            UploadQueueCost(cost_code="3015 - HW", base_code="3015", amount=D("-900")),
            UploadQueueCost(cost_code="3002 - Walls", base_code="3002", amount=D("100")),
        ]
    )
    assert title == "3015"


# ─── Multi-page invoices ──────────────────────────────────────────────


def test_a_multi_page_bundle_becomes_one_invoice_per_page():
    """SOP §5. Read as one document it does not fail — it succeeds, with one
    confident five-figure total on one wrong job."""
    assert len(pdf_split.split_pages(make_pdf(12))) == 12


def test_each_split_page_is_a_standalone_pdf():
    """The rest of the pipeline must not be able to tell a split page from a
    file that arrived on its own, or there are two extraction paths."""
    for part in pdf_split.split_pages(make_pdf(3)):
        assert pdf_split.page_count(part) == 1


def test_pages_of_one_file_are_distinguished_by_source_page(db_invoices, monkeypatch):
    monkeypatch.setattr(intake, "apply_extraction", lambda *a, **k: "suggested")
    intake._process_file(
        {"id": "wc-1", "name": "combined.pdf"},
        source_label="White Cap",
        actor=intake.SYSTEM_ACTOR,
    )
    rows = db_invoices.tables["invoices"].rows
    assert {(r["source_file_id"], r["source_page"]) for r in rows} == {
        ("wc-1", 1),
        ("wc-1", 2),
        ("wc-1", 3),
    }


# ─── Zero-amount invoices ─────────────────────────────────────────────


def test_a_zero_amount_invoice_balances_against_a_zero_split():
    balanced, _ = invoice_rules.costs_balance(D("0.00"), [D("0.00")])
    assert balanced


def test_a_zero_amount_invoice_warns_at_upload_but_does_not_block(monkeypatch):
    """Somebody reviewed and approved it. Odd is not impossible, and
    blocking here would strand an approved invoice with no way forward."""
    from app.api import invoices as api

    monkeypatch.setattr(
        api.storage, "signed_url", lambda b, p, expires_in=3600: "https://signed"
    )
    item = api._to_queue_item(
        {
            "id": "i1",
            "invoice_no": "278461",
            "bill_no": "8461",
            "invoice_date": "2026-09-14",
            "due_date": "2026-10-31",
            "amount": D("0.00"),
            "pdf_storage_path": "x.pdf",
            "created_at": "2026-09-15T00:00:00+00:00",
        },
        project={
            "id": "p", "project_no": "25-20", "name": "A St", "bt_job_id": "1",
            "drive_folder_name": "A St", "quirks": {}, "notes": None,
            "status": "old",
        },
        vendor={
            "id": "v", "invoice_name": "CalPortland", "bt_name": "Catalina Pacific",
            "drive_folder_name": "CalPortland", "notes": None, "active": True,
        },
        cost_rows=[{"cost_code_id": "c1", "amount": D("0.00")}],
        codes={
            "c1": {
                "id": "c1", "code": "3002 - Walls", "base_code": "3002",
                "description": "Walls", "active": True,
            }
        },
    )
    assert any("$0.00" in w for w in item.warnings)
    assert item.blockers == []


def test_a_zero_amount_bucket_reports_no_rate_rather_than_zero_percent():
    """Zero percent reads as the model failing. None reads as no data."""
    from app.core.metrics import Bucket

    assert Bucket(key="k", label="L").rate is None


# ─── The same invoice arriving twice ──────────────────────────────────


def test_the_same_drive_file_is_never_ingested_twice(db_invoices, monkeypatch):
    """The poll re-reads the same folder every fifteen minutes."""
    monkeypatch.setattr(intake.drive, "download", lambda _f: make_pdf(1))
    monkeypatch.setattr(intake, "apply_extraction", lambda *a, **k: "suggested")

    first = intake._process_file(
        {"id": "f1", "name": "a.pdf"},
        source_label="Invoice Uploads",
        actor=intake.SYSTEM_ACTOR,
    )
    second = intake._process_file(
        {"id": "f1", "name": "a.pdf"},
        source_label="Invoice Uploads",
        actor=intake.SYSTEM_ACTOR,
    )
    assert first["outcome"] == "suggested"
    assert second["outcome"] == "existing"
    assert len(db_invoices.tables["invoices"].rows) == 1


def test_the_same_pdf_uploaded_twice_creates_one_invoice(db_invoices, monkeypatch):
    monkeypatch.setattr(intake.settings, "anthropic_api_key", None)
    pdf = make_pdf(1)
    intake.ingest_upload(filename="a.pdf", pdf_bytes=pdf, actor=AGENT)
    again = intake.ingest_upload(filename="renamed.pdf", pdf_bytes=pdf, actor=AGENT)
    assert again["outcome"] == "existing"
    assert len(db_invoices.tables["invoices"].rows) == 1


def test_a_rescan_of_an_invoice_already_in_the_system_is_flagged(monkeypatch):
    """A different Drive file and a worse scan, so none of the Phase 1 exact
    matches fire. This is the case that was documented as open."""
    client = FakeClient()
    client.tables["invoices"] = FakeTable(
        "invoices",
        [
            {
                "id": "old",
                "vendor_id": "v-cal",
                "invoice_no": "278461",
                "amount": "2294.25",
                "invoice_date": "2026-09-14",
                "status": "approved",
                "created_at": "2026-09-15T00:00:00+00:00",
            }
        ],
    )
    monkeypatch.setattr(duplicates, "get_service_client", lambda: client)

    match = duplicates.find_duplicate(
        invoice_id="new",
        vendor_id="v-cal",
        invoice_no="278461-1",        # suffix added by the re-scan
        amount=D("2294.26"),          # a cent misread
        invoice_date="2026-09-15",    # a day out
    )
    assert match is not None
    assert match.invoice["id"] == "old"


def test_the_identical_pdf_is_recognised_across_arrival_routes(monkeypatch):
    """Once off Drive, once as an email attachment somebody uploaded. Every
    extracted field could differ; the bytes do not."""
    client = FakeClient()
    client.tables["invoices"] = FakeTable(
        "invoices",
        [
            {
                "id": "old",
                "vendor_id": "v-cal",
                "invoice_no": "ILLEGIBLE",
                "amount": "1.00",
                "invoice_date": "2020-01-01",
                "status": "approved",
                "pdf_sha256": "deadbeef",
                "created_at": "2026-09-15T00:00:00+00:00",
            }
        ],
    )
    monkeypatch.setattr(duplicates, "get_service_client", lambda: client)

    match = duplicates.find_duplicate(
        invoice_id="new",
        vendor_id="v-cal",
        invoice_no="278461",
        amount=D("2294.25"),
        invoice_date="2026-09-14",
        pdf_sha256="deadbeef",
    )
    assert match is not None
    assert "byte-for-byte" in match.reasons[0]


@pytest.fixture
def db_invoices(monkeypatch):
    client = FakeClient()
    client.tables["invoices"] = FakeTable("invoices", [])
    monkeypatch.setattr(intake, "_sb", lambda: client)

    from app.core import audit

    monkeypatch.setattr(audit, "get_service_client", lambda: client)
    monkeypatch.setattr(intake.storage, "upload_bytes", lambda *a, **k: "p.pdf")
    monkeypatch.setattr(intake.drive, "download", lambda _f: make_pdf(3))
    monkeypatch.setattr(pdf_split, "page_text", lambda _b: "")
    return client
