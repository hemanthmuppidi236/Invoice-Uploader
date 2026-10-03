"""
Duplicate detection (prompt §7.2, hardened in Phase 4).

The two errors cost wildly different amounts, and that asymmetry decides
every judgement call under test here.

A **missed** duplicate is a vendor paid twice. It raises no error; it turns
up in a reconciliation weeks later, or never.

A **false** duplicate is one invoice sitting in /flagged until Linda presses
"not a duplicate". Thirty seconds.

So the bias is toward flagging — but not so loose that the queue fills with
noise, because a queue nobody trusts is a queue nobody reads, which converts
the cheap error back into the expensive one.

The Phase 1 version matched on exact equality and was defeated by the most
ordinary thing that happens to these documents: a re-scan.
"""

from decimal import Decimal

import pytest

from app.core import duplicates
from tests.fakes import FakeClient, FakeTable


D = Decimal


# ─── Amounts ──────────────────────────────────────────────────────────


def test_the_documented_gap_is_closed():
    """The exact case left open at the end of Phase 1: "a re-scan that reads
    2294.25 as 2294.26 slips through both tests"."""
    assert duplicates.amounts_match(D("2294.25"), D("2294.26")) is True


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("2294.25", "2294.25", True),     # identical
        ("2294.25", "2294.28", True),     # three cents
        ("2294.25", "2294.95", True),     # 70c — still inside one misread digit
        ("2294.25", "2304.25", False),    # $10 is a dollar digit, not a misread
        ("40000.00", "40018.00", True),   # 0.045% on a large pour
        ("100.00", "135.00", False),      # 35% is not a scanning error
    ],
)
def test_amounts_match_within_scanning_tolerance(a, b, expected):
    """The rule models a misread in the cents and dimes. A misread cannot
    move a total by more than 99 cents without also changing a dollar digit,
    and an error that large should be caught by the invoice number instead.

    Erring loose is cheap here: an amount match alone scores 2 and never
    flags on its own, so a generous tolerance costs a second signal rather
    than a false flag."""
    assert duplicates.amounts_match(D(a), D(b)) is expected


def test_a_credit_memo_is_not_a_near_match_for_the_invoice_it_reverses():
    """SOP §5 has White Cap issuing both. A -$500 credit and a $500 invoice
    are opposites, and flagging them as near-duplicates would make every
    credit memo stop in the queue."""
    assert duplicates.amounts_match(D("-500.00"), D("500.00")) is False
    assert duplicates.amounts_match(D("-500.00"), D("-500.01")) is True


def test_a_missing_amount_never_matches():
    assert duplicates.amounts_match(None, D("100.00")) is False
    assert duplicates.amounts_match(D("100.00"), None) is False


# ─── Invoice numbers ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("278461-1", "2784611"),
        ("INV-278461", "278461"),
        ("278461 1", "2784611"),
        ("  278461  ", "278461"),
        ("CREDIT MEMO", None),
        (None, None),
    ],
)
def test_invoice_numbers_normalise_to_their_digits(raw, expected):
    assert duplicates.normalize_invoice_no(raw) == expected


def test_a_suffixed_reissue_matches_the_original():
    """SOP §4 shows this vendor numbering revisions with a suffix:
    278461-1 is a re-issue of 278461, not a separate bill."""
    assert duplicates.invoice_numbers_match("278461", "278461-1") is True


def test_formatting_differences_do_not_create_a_new_invoice():
    assert duplicates.invoice_numbers_match("INV-278461", "278461") is True


def test_short_numbers_do_not_match_by_coincidence():
    """Containment is what catches a suffix, and it is also what would make
    `12` match `1123`. Half the queue would flag."""
    assert duplicates.invoice_numbers_match("12", "1123") is False


def test_genuinely_different_numbers_do_not_match():
    assert duplicates.invoice_numbers_match("278461", "991234") is False


# ─── Dates ────────────────────────────────────────────────────────────


def test_a_date_read_a_day_out_still_matches():
    assert duplicates.dates_near("2026-09-14", "2026-09-15") is True


def test_a_week_apart_is_a_different_invoice():
    assert duplicates.dates_near("2026-09-14", "2026-09-22") is False


# ─── Scoring ──────────────────────────────────────────────────────────


def _candidate(**over):
    base = {
        "id": "inv-old",
        "invoice_no": "278461",
        "amount": "2294.25",
        "invoice_date": "2026-09-14",
        "status": "approved",
        "created_at": "2026-09-15T00:00:00+00:00",
    }
    base.update(over)
    return base


def _score(**kw):
    return duplicates.score_candidate(
        candidate=kw.pop("candidate", _candidate()),
        invoice_no=kw.pop("invoice_no", "278461"),
        amount=kw.pop("amount", D("2294.25")),
        invoice_date=kw.pop("invoice_date", "2026-09-14"),
        **kw,
    )


def test_an_identical_pdf_is_proof_not_evidence():
    """It still holds when every extracted field came out different because
    the second scan was worse."""
    match = _score(
        candidate=_candidate(
            pdf_sha256="abc", invoice_no="99999", amount="1.00",
            invoice_date="2020-01-01",
        ),
        pdf_sha256="abc",
    )
    assert match.is_strong
    assert "byte-for-byte" in match.reasons[0]


def test_an_invoice_number_alone_is_enough_to_flag():
    """Two invoices from one vendor sharing an amount and a date happen. Two
    sharing an invoice number essentially do not."""
    match = _score(amount=D("9999.00"), invoice_date="2026-01-01")
    assert match.is_strong


def test_amount_and_date_together_are_enough_to_flag():
    """The re-scan case where the number was read differently — which is
    exactly what a unique constraint on the number misses."""
    match = _score(invoice_no="27846X")
    assert match.is_strong


def test_a_shared_date_alone_does_not_flag():
    """One vendor bills a project repeatedly. Flagging on the date alone
    would put most of the queue in /flagged and teach everyone to skip it."""
    match = _score(invoice_no="999999", amount=D("55.00"))
    assert not match.is_strong


def test_the_reason_says_what_actually_matched():
    """"Possible duplicate" alone sends someone to open two PDFs. "Same
    invoice number, amount 1 cent apart" is decidable without opening
    either."""
    match = _score(amount=D("2294.26"))
    joined = " ".join(match.reasons)
    assert "same invoice number" in joined
    assert "$0.01 apart" in joined


# ─── The query ────────────────────────────────────────────────────────


@pytest.fixture
def db(monkeypatch):
    client = FakeClient()
    client.tables["invoices"] = FakeTable("invoices", [])
    monkeypatch.setattr(duplicates, "get_service_client", lambda: client)
    return client


def _existing(db, **over):
    row = {"vendor_id": "v-calportland", **_candidate(), **over}
    db.tables["invoices"].rows.append(row)
    return row


def _find(**kw):
    return duplicates.find_duplicate(
        invoice_id=kw.pop("invoice_id", "inv-new"),
        vendor_id=kw.pop("vendor_id", "v-calportland"),
        invoice_no=kw.pop("invoice_no", "278461"),
        amount=kw.pop("amount", D("2294.25")),
        invoice_date=kw.pop("invoice_date", "2026-09-14"),
        **kw,
    )


def test_a_rescan_with_a_misread_cent_is_caught(db):
    """End to end, the case Phase 1 documented as open."""
    _existing(db)
    match = _find(amount=D("2294.26"), invoice_no="2784611")
    assert match is not None
    assert match.invoice["id"] == "inv-old"


def test_a_voided_invoice_is_never_a_duplicate(db):
    """Voiding is how a real duplicate gets resolved, so matching against
    voided records would make the flag permanent and unclearable."""
    _existing(db, status="void")
    assert _find() is None


def test_an_invoice_never_matches_itself(db):
    _existing(db, id="inv-new")
    assert _find(invoice_id="inv-new") is None


def test_no_vendor_means_no_check(db):
    """Vendor is the scope. Without it the comparison is against every
    invoice in the system, where shared numbers are routine."""
    _existing(db)
    assert _find(vendor_id=None) is None


def test_the_same_number_from_a_different_vendor_is_not_a_duplicate(db):
    """Most vendors start numbering at 1. Comparing across them would flag
    constantly."""
    _existing(db, vendor_id="v-whitecap")
    assert _find() is None


def test_the_oldest_match_wins_a_tie(db):
    """The new invoice is duplicating the earlier record, not the other way
    round, so that is the one to link to."""
    _existing(db, id="inv-older", created_at="2026-09-01T00:00:00+00:00")
    _existing(db, id="inv-newer", created_at="2026-09-20T00:00:00+00:00")
    match = _find()
    assert match.invoice["id"] == "inv-older"


def test_nothing_similar_returns_nothing(db):
    _existing(db, invoice_no="111111", amount="42.00", invoice_date="2026-03-02")
    assert _find() is None


def test_the_description_names_the_other_invoice_and_the_reason(db):
    _existing(db)
    text = duplicates.describe(_find())
    assert "278461" in text
    assert "2,294.25" in text
    assert "Matched because" in text
    assert "mark not a duplicate" in text


def test_an_identical_pdf_outranks_the_date_window(db):
    """The window and the vendor scope are heuristics; the hash is proof.
    Filtering proof through a heuristic means a re-scan of something from
    last year, or one ingested before its vendor resolved, is missed — and
    missed is the expensive direction."""
    _existing(
        db,
        id="inv-ancient",
        invoice_date="2020-01-01",
        invoice_no="ILLEGIBLE",
        amount="1.00",
        pdf_sha256="deadbeef",
    )
    match = _find(pdf_sha256="deadbeef")
    assert match is not None
    assert match.invoice["id"] == "inv-ancient"


def test_an_identical_pdf_is_found_even_with_no_vendor_resolved(db):
    """Intake flags unknown-vendor invoices, and the same file arriving twice
    while the vendor is still unmapped is exactly when a human is least
    likely to notice."""
    _existing(db, id="inv-old", vendor_id=None, pdf_sha256="deadbeef")
    match = _find(vendor_id=None, pdf_sha256="deadbeef")
    assert match is not None


def test_an_identical_pdf_that_was_voided_does_not_match(db):
    """Voiding is how a real duplicate is resolved. Matching a voided record
    would make the flag unclearable."""
    _existing(db, id="inv-void", status="void", pdf_sha256="deadbeef")
    assert _find(pdf_sha256="deadbeef") is None
