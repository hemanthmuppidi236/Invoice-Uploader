"""
Splitting a combined White Cap file (SOP §5, prompt Phase 4).

White Cap sends one PDF covering many jobs, and the SOP is unambiguous: one
page is one invoice. A twelve-page bundle is twelve payables on up to twelve
different jobs. Reading it as a single document does not fail — it succeeds,
producing one confident five-figure total on one wrong job, which is the
kind of error that gets paid before anyone notices.

The properties that matter: every page becomes its own invoice, re-polling
the same file finds them all rather than duplicating any, one bad page does
not cost the other eleven, and a yard page never becomes a payable at all.
"""

import pytest

from app.core import intake, pdf_split
from tests.fakes import FakeClient, FakeTable
from tests.helpers import make_pdf


# ─── The mechanical split ─────────────────────────────────────────────


def test_each_page_becomes_its_own_pdf():
    parts = pdf_split.split_pages(make_pdf(5))
    assert len(parts) == 5
    assert all(pdf_split.page_count(p) == 1 for p in parts)
    assert all(p.startswith(b"%PDF") for p in parts)


def test_page_order_is_preserved():
    """`source_page` is half the uniqueness key that makes re-polling
    idempotent, so page 3 has to be page 3 on every run."""
    data = make_pdf(4)
    first = pdf_split.split_pages(data)
    second = pdf_split.split_pages(data)
    assert first == second


def test_an_unreadable_file_says_so_rather_than_raising_a_stack_trace():
    with pytest.raises(pdf_split.PdfSplitError) as e:
        pdf_split.split_pages(b"this is not a pdf at all")
    assert "could not be read" in str(e.value)


def test_an_absurd_page_count_is_refused():
    """A bundle this size is a scanning accident, not a vendor statement.
    Without the cap, one malformed file turns a poll run into hundreds of
    Claude calls before anybody looks at it."""
    with pytest.raises(pdf_split.PdfSplitError) as e:
        pdf_split.split_pages(make_pdf(5), max_pages=3)
    assert "5 pages" in str(e.value)


def test_an_empty_pdf_is_refused():
    with pytest.raises(pdf_split.PdfSplitError):
        pdf_split.split_pages(make_pdf(0))


# ─── Reading the page before the model does ───────────────────────────


@pytest.mark.parametrize(
    "line,expected",
    [
        ("CUSTOMER JOB NO.: BUNGALOWS OF TORRANCE", "BUNGALOWS OF TORRANCE"),
        ("CUSTOMER JOB NO. DEL AMO APARTMENTS", "DEL AMO APARTMENTS"),
        ("Customer Job No: A Street Flats", "A Street Flats"),
        ("CUSTOMER  JOB  NO  -  EL CAJON YARD", "EL CAJON YARD"),
    ],
)
def test_the_customer_job_no_line_is_pulled_out(line, expected):
    """SOP §5 names this field as what identifies the job. Handing it over as
    a stated hint beats leaving the model to find the right line among twelve
    similar ones — but it stays a hint: resolve_project still has to match
    it, and an unmatched job flags rather than guesses."""
    assert pdf_split.customer_job_no(f"WHITE CAP\n{line}\nTOTAL 447.46") == expected


def test_a_page_with_no_job_line_returns_nothing():
    assert pdf_split.customer_job_no("WHITE CAP\nTOTAL 447.46") is None


def test_a_page_with_no_text_layer_is_not_an_error():
    """White Cap PDFs are often scans. No text is the normal case, not a
    failure — the model reads the image instead."""
    assert pdf_split.page_text(make_pdf(1)) == ""
    assert pdf_split.page_text(b"not a pdf") == ""


# ─── Through intake ───────────────────────────────────────────────────


@pytest.fixture
def db(monkeypatch):
    client = FakeClient()
    client.tables["invoices"] = FakeTable("invoices", [])
    monkeypatch.setattr(intake, "_sb", lambda: client)

    from app.core import audit

    monkeypatch.setattr(audit, "get_service_client", lambda: client)
    monkeypatch.setattr(intake.storage, "upload_bytes", lambda *a, **k: "p.pdf")
    monkeypatch.setattr(intake.drive, "download", lambda _f: make_pdf(3))
    monkeypatch.setattr(pdf_split, "page_text", lambda _b: "")
    return client


def _rows(db):
    return db.tables["invoices"].rows


ENTRY = {"id": "drive-wc", "name": "White Cap 09-23.pdf"}


def test_one_combined_file_becomes_one_invoice_per_page(db, monkeypatch):
    monkeypatch.setattr(intake, "apply_extraction", lambda *a, **k: "suggested")

    outcome = intake._process_file(
        ENTRY, source_label="White Cap", actor=intake.SYSTEM_ACTOR
    )

    assert outcome["outcome"] == "split"
    assert outcome["pages"] == 3
    assert len(_rows(db)) == 3
    assert {r["source_page"] for r in _rows(db)} == {1, 2, 3}
    # One Drive file behind all three, which is what lets a re-poll recognise
    # every page instead of creating three more.
    assert {r["source_file_id"] for r in _rows(db)} == {"drive-wc"}


def test_repolling_the_same_combined_file_creates_nothing(db, monkeypatch):
    """The failure this prevents is twelve duplicate payables every fifteen
    minutes, which is what the poll interval would otherwise produce."""
    monkeypatch.setattr(intake, "apply_extraction", lambda *a, **k: "suggested")

    intake._process_file(ENTRY, source_label="White Cap", actor=intake.SYSTEM_ACTOR)
    second = intake._process_file(
        ENTRY, source_label="White Cap", actor=intake.SYSTEM_ACTOR
    )

    assert second["outcome"] == "existing"
    assert len(_rows(db)) == 3


def test_one_bad_page_does_not_abandon_the_others(db, monkeypatch):
    """Each page is a separate payable. Losing eleven because page two threw
    would be the expensive mistake."""
    calls = {"n": 0}

    def flaky(invoice_id, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("model timeout")
        return "suggested"

    monkeypatch.setattr(intake, "apply_extraction", flaky)

    outcome = intake._process_file(
        ENTRY, source_label="White Cap", actor=intake.SYSTEM_ACTOR
    )
    assert outcome["pages"] == 3
    assert len(_rows(db)) == 3


def test_a_yard_page_never_becomes_a_payable(db, monkeypatch):
    """SOP §5: "Skip yard invoices — don't enter them." Caught before the
    Claude call when the page has a text layer, because a yard page costs
    nothing to recognise and should not cost a model call either."""
    monkeypatch.setattr(
        pdf_split, "page_text", lambda _b: "WHITE CAP\nCUSTOMER JOB NO.: EL CAJON YARD"
    )
    called = {"n": 0}

    def counted(*a, **k):
        called["n"] += 1
        return "suggested"

    monkeypatch.setattr(intake, "apply_extraction", counted)

    intake._process_file(ENTRY, source_label="White Cap", actor=intake.SYSTEM_ACTOR)

    assert all(r["flag_code"] == "yard" for r in _rows(db))
    assert called["n"] == 0


def test_the_job_line_is_handed_to_the_extractor_as_a_hint(db, monkeypatch):
    monkeypatch.setattr(
        pdf_split,
        "page_text",
        lambda _b: "WHITE CAP\nCUSTOMER JOB NO.: BUNGALOWS OF TORRANCE",
    )
    seen = []
    monkeypatch.setattr(
        intake,
        "apply_extraction",
        lambda invoice_id, **kw: seen.append(kw.get("project_hint")) or "suggested",
    )

    intake._process_file(ENTRY, source_label="White Cap", actor=intake.SYSTEM_ACTOR)
    assert seen == ["BUNGALOWS OF TORRANCE"] * 3


def test_an_unsplittable_file_flags_rather_than_vanishing(db, monkeypatch):
    monkeypatch.setattr(intake.drive, "download", lambda _f: b"not a pdf")

    outcome = intake._process_file(
        ENTRY, source_label="White Cap", actor=intake.SYSTEM_ACTOR
    )
    assert outcome["outcome"] == "flagged"
    assert _rows(db)[0]["flag_code"] == "unreadable"
    assert len(_rows(db)) == 1


def test_an_already_split_page_in_a_job_subfolder_is_left_alone(db, monkeypatch):
    """A file inside White Cap/[Job Name]/ was split by hand. Splitting it
    again would turn one invoice into one invoice plus a layer of confusion."""
    monkeypatch.setattr(intake.drive, "download", lambda _f: make_pdf(2))
    monkeypatch.setattr(intake, "apply_extraction", lambda *a, **k: "suggested")

    outcome = intake._process_file(
        {
            "id": "wc-split",
            "name": "278461.pdf",
            "parent_folder_name": "Bungalows of Torrance",
        },
        source_label="White Cap",
        actor=intake.SYSTEM_ACTOR,
    )
    assert outcome["outcome"] == "suggested"
    assert len(_rows(db)) == 1
