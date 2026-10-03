"""
Filing a processed invoice back to Drive (prompt §7.7).

Filing is the step that runs after the bill is already in BuilderTrend, so
every failure here is a failure in the one state where retrying blindly is
dangerous: file twice and the vendor folder gets two copies of the same
invoice; archive without filing and the original leaves the intake folder
with no copy anywhere. These tests pin the order and the failure handling.
"""

from datetime import date
from decimal import Decimal

import pytest

from app.core import filing
from tests.fakes import FakeClient, FakeTable


# ─── Naming convention inference (SOP §7) ─────────────────────────────


def test_an_empty_folder_falls_back_to_the_documented_default():
    convention, note = filing.detect_convention([])
    assert convention == filing.DEFAULT_CONVENTION
    # And says so, rather than silently choosing.
    assert note and "empty" in note.lower()


def test_the_folder_convention_is_read_not_assumed():
    """SOP §7 lists two real formats from two real folders. Neither is the
    house style; each folder is its own."""
    convention, note = filing.detect_convention(
        ["09-14-26 $2,600.00.pdf", "08-30-26 $1,100.00.pdf"]
    )
    assert convention == "MM-DD-YY $amount"
    assert note is None

    convention, note = filing.detect_convention(
        ["26 09-23 $447.46.pdf", "26 09-02 $1,204.00.pdf"]
    )
    assert convention == "YY MM-DD $amount"
    assert note is None


def test_a_mixed_folder_takes_the_majority_and_says_so():
    """First-match would let one stray file redefine a folder of twenty."""
    names = ["09-14-26 $1.00.pdf"] * 20 + ["26 09-23 $2.00.pdf"]
    convention, note = filing.detect_convention(names)
    assert convention == "MM-DD-YY $amount"
    assert note and "most common" in note


def test_an_unrecognised_folder_says_so_rather_than_guessing_quietly():
    convention, note = filing.detect_convention(
        ["CalPortland invoice sep.pdf", "scan0012.pdf"]
    )
    assert convention == filing.DEFAULT_CONVENTION
    assert note and "default" in note.lower()


# ─── Filename construction ────────────────────────────────────────────


def test_the_filename_matches_the_folders_convention():
    assert (
        filing.build_filename(
            convention="MM-DD-YY $amount",
            invoice_date=date(2026, 9, 14),
            amount=Decimal("2600.00"),
        )
        == "09-14-26 $2,600.00.pdf"
    )
    assert (
        filing.build_filename(
            convention="YY MM-DD $amount",
            invoice_date=date(2026, 9, 23),
            amount=Decimal("447.46"),
        )
        == "26 09-23 $447.46.pdf"
    )


def test_a_dollar_sign_survives_intact():
    """SOP §9: a `$` going through a shell unescaped turned $2,294.25 into
    ,294.25 on five invoices. Going through the Drive API means the filename
    is data, never a shell word."""
    name = filing.build_filename(
        convention="MM-DD-YY $amount",
        invoice_date=date(2026, 9, 24),
        amount=Decimal("2294.25"),
    )
    assert name == "09-24-26 $2,294.25.pdf"
    assert "$2,294.25" in name


def test_a_credit_memo_reads_as_a_credit_at_a_glance():
    name = filing.build_filename(
        convention="MM-DD-YY $amount",
        invoice_date=date(2026, 9, 14),
        amount=Decimal("-500.00"),
    )
    assert name == "09-14-26 -$500.00.pdf"


# ─── Collisions ───────────────────────────────────────────────────────


def test_a_collision_suffixes_rather_than_overwriting():
    """SOP §7 makes a same-name-different-size collision a stop-and-ask. The
    copy still gets filed, because a filed duplicate is recoverable and a
    lost invoice is not — but it is reported."""
    name, note = filing.disambiguate(
        "09-14-26 $2,600.00.pdf", ["09-14-26 $2,600.00.pdf"]
    )
    assert name == "09-14-26 $2,600.00.pdf".replace(".pdf", " (2).pdf")
    assert note and "already in the vendor folder" in note


def test_no_collision_means_no_suffix_and_no_noise():
    name, note = filing.disambiguate("09-14-26 $2,600.00.pdf", ["other.pdf"])
    assert name == "09-14-26 $2,600.00.pdf"
    assert note is None


# ─── The filing run ───────────────────────────────────────────────────


@pytest.fixture
def drive(monkeypatch):
    """A fake Drive that records what was asked of it."""

    class FakeDrive:
        def __init__(self):
            self.folders = {}          # (parent, name) -> id
            self.filenames = {}        # folder_id -> [names]
            self.uploaded = []
            self.moved = []

        def find_folder(self, parent_id, name):
            found = self.folders.get((parent_id, name))
            return {"id": found, "name": name} if found else None

        def list_filenames(self, folder_id):
            return list(self.filenames.get(folder_id, []))

        def upload_copy(self, *, folder_id, filename, data, **_):
            self.uploaded.append((folder_id, filename, len(data)))
            return {"id": "filed-1", "name": filename}

        def move(self, file_id, *, to_folder_id):
            self.moved.append((file_id, to_folder_id))
            return {"id": file_id}

    fake = FakeDrive()
    for name in ("find_folder", "list_filenames", "upload_copy", "move"):
        monkeypatch.setattr(filing.drive, name, getattr(fake, name))
    # Filing asks the database whether this Drive file backs other invoices
    # that are not filed yet (SOP §5 combined files). Default: it does not.
    client = FakeClient()
    client.tables["invoices"] = FakeTable("invoices", [])
    monkeypatch.setattr(filing, "get_service_client", lambda: client)
    fake.db = client
    monkeypatch.setattr(
        filing.storage, "download_bytes", lambda bucket, path: b"%PDF-1.4 fake"
    )
    monkeypatch.setattr(filing.settings, "google_drive_credentials_json", "{}")
    monkeypatch.setattr(filing.settings, "drive_folder_bt_invoices", "bt-root")
    monkeypatch.setattr(filing.settings, "drive_folder_invoice_uploads", "intake")
    return fake


def _invoice(**over):
    base = {
        "id": "inv-1",
        "amount": Decimal("2600.00"),
        "invoice_date": "2026-09-14",
        "pdf_storage_path": "2026/09/inv-1.pdf",
        "source_file_id": "drive-file-1",
        "source_path": "Invoice Uploads/scan.pdf",
    }
    base.update(over)
    return base


PROJECT = {"name": "A Street Flats", "drive_folder_name": "A Street Flats"}
VENDOR = {"invoice_name": "CalPortland", "drive_folder_name": "CalPortland"}


def test_a_full_run_copies_then_archives(drive):
    drive.folders[("bt-root", "A Street Flats")] = "proj-folder"
    drive.folders[("proj-folder", "CalPortland")] = "vend-folder"
    drive.folders[("intake", "Uploaded")] = "archive-folder"
    drive.filenames["vend-folder"] = ["09-01-26 $100.00.pdf"]

    result = filing.file_invoice(
        invoice=_invoice(), project=PROJECT, vendor=VENDOR
    )

    assert drive.uploaded == [("vend-folder", "09-14-26 $2,600.00.pdf", 13)]
    assert drive.moved == [("drive-file-1", "archive-folder")]
    assert result.archived is True
    assert result.filed_path == (
        "BT Invoices/A Street Flats/CalPortland/09-14-26 $2,600.00.pdf"
    )


def test_the_copy_happens_before_the_archive(drive, monkeypatch):
    """Order matters: archiving first and then failing the copy would leave
    the original out of the intake folder with no copy anywhere."""
    drive.folders[("bt-root", "A Street Flats")] = "proj-folder"
    drive.folders[("proj-folder", "CalPortland")] = "vend-folder"
    drive.folders[("intake", "Uploaded")] = "archive-folder"

    order = []
    original_upload = drive.upload_copy
    original_move = drive.move

    def upload(**kw):
        order.append("upload")
        return original_upload(**kw)

    def move(file_id, **kw):
        order.append("move")
        return original_move(file_id, **kw)

    monkeypatch.setattr(filing.drive, "upload_copy", upload)
    monkeypatch.setattr(filing.drive, "move", move)

    filing.file_invoice(invoice=_invoice(), project=PROJECT, vendor=VENDOR)
    assert order == ["upload", "move"]


def test_a_missing_project_folder_stops_and_asks(drive):
    """§7.7 and §12: never create a Drive folder. A folder the app invents is
    a folder nobody is looking in."""
    with pytest.raises(filing.FilingError) as e:
        filing.file_invoice(invoice=_invoice(), project=PROJECT, vendor=VENDOR)
    assert e.value.needs_folder is True
    assert "never creates" in str(e.value)
    assert drive.uploaded == []


def test_a_missing_vendor_folder_stops_and_asks(drive):
    drive.folders[("bt-root", "A Street Flats")] = "proj-folder"
    with pytest.raises(filing.FilingError) as e:
        filing.file_invoice(invoice=_invoice(), project=PROJECT, vendor=VENDOR)
    assert e.value.needs_folder is True
    assert drive.uploaded == []


def test_the_recorded_drive_folder_name_wins_over_the_letterhead(drive):
    """SOP §6: Holliday Rock files under a folder spelled "Holiday Rock"."""
    drive.folders[("bt-root", "A Street Flats")] = "proj-folder"
    drive.folders[("proj-folder", "Holiday Rock")] = "vend-folder"
    drive.folders[("intake", "Uploaded")] = "archive-folder"

    result = filing.file_invoice(
        invoice=_invoice(),
        project=PROJECT,
        vendor={"invoice_name": "Holliday Rock", "drive_folder_name": "Holiday Rock"},
    )
    assert "Holiday Rock" in result.filed_path


def test_a_failed_archive_does_not_undo_a_successful_copy(drive, monkeypatch):
    """Re-running would file a second copy, which is worse than an original
    left in place. So the archive failure is a warning, not an error."""
    drive.folders[("bt-root", "A Street Flats")] = "proj-folder"
    drive.folders[("proj-folder", "CalPortland")] = "vend-folder"
    drive.folders[("intake", "Uploaded")] = "archive-folder"

    def boom(file_id, **kw):
        raise RuntimeError("403 insufficient permissions")

    monkeypatch.setattr(filing.drive, "move", boom)

    result = filing.file_invoice(invoice=_invoice(), project=PROJECT, vendor=VENDOR)
    assert result.filed_path
    assert result.archived is False
    assert any("could not move the original" in w for w in result.warnings)
    assert any("nothing is deleted" in w for w in result.warnings)


def test_a_missing_uploaded_folder_is_a_warning_not_a_failure(drive):
    drive.folders[("bt-root", "A Street Flats")] = "proj-folder"
    drive.folders[("proj-folder", "CalPortland")] = "vend-folder"

    result = filing.file_invoice(invoice=_invoice(), project=PROJECT, vendor=VENDOR)
    assert result.filed_path
    assert result.archived is False
    assert any("Uploaded/" in w for w in result.warnings)


def test_no_stored_pdf_is_refused_before_anything_is_written(drive):
    drive.folders[("bt-root", "A Street Flats")] = "proj-folder"
    drive.folders[("proj-folder", "CalPortland")] = "vend-folder"
    with pytest.raises(filing.FilingError):
        filing.file_invoice(
            invoice=_invoice(pdf_storage_path=None), project=PROJECT, vendor=VENDOR
        )
    assert drive.uploaded == []


def test_drive_not_configured_says_the_bill_is_still_in_buildertrend(
    drive, monkeypatch
):
    """The message has to distinguish "the upload failed" from "the filing
    failed", because the recovery is completely different."""
    monkeypatch.setattr(filing.settings, "google_drive_credentials_json", None)
    monkeypatch.setattr(filing.settings, "drive_oauth_refresh_token", None)
    with pytest.raises(filing.FilingError) as e:
        filing.file_invoice(invoice=_invoice(), project=PROJECT, vendor=VENDOR)
    assert "BuilderTrend" in str(e.value)


def test_a_white_cap_original_archives_under_the_white_cap_tree(drive, monkeypatch):
    """SOP §5 keeps a separate archive for the combined-file folder."""
    monkeypatch.setattr(filing.settings, "drive_folder_white_cap", "whitecap")
    drive.folders[("bt-root", "A Street Flats")] = "proj-folder"
    drive.folders[("proj-folder", "White Cap")] = "vend-folder"
    drive.folders[("whitecap", "Uploaded")] = "wc-archive"
    drive.folders[("intake", "Uploaded")] = "intake-archive"

    filing.file_invoice(
        invoice=_invoice(source_path="White Cap/A Street Flats/inv.pdf"),
        project=PROJECT,
        vendor={"invoice_name": "White Cap", "drive_folder_name": "White Cap"},
    )
    assert drive.moved == [("drive-file-1", "wc-archive")]


def test_filing_never_deletes():
    """The rule that matters most, asserted against the module rather than a
    behaviour: there is no delete call to reach."""
    source = (filing.drive.__file__, filing.__file__)
    for path in source:
        with open(path) as fh:
            text = fh.read()
        assert ".delete(" not in text
        assert "files().delete" not in text


# ─── A combined file backs several invoices (SOP §5) ──────────────────


def _sibling(drive, invoice_id, status):
    drive.db.tables["invoices"].rows.append(
        {"id": invoice_id, "source_file_id": "drive-file-1", "status": status}
    )


def _ready(drive):
    drive.folders[("bt-root", "A Street Flats")] = "proj-folder"
    drive.folders[("proj-folder", "CalPortland")] = "vend-folder"
    drive.folders[("intake", "Uploaded")] = "archive-folder"


def test_the_original_stays_put_while_sibling_pages_are_unworked(drive):
    """A combined White Cap PDF is one Drive file behind twelve invoices.
    Moving it out of the intake folder when page one is filed hides a
    document whose other pages are still being worked, and the person looking
    for it has no way to know it went early."""
    _ready(drive)
    _sibling(drive, "inv-2", "assigned")
    _sibling(drive, "inv-3", "approved")

    result = filing.file_invoice(invoice=_invoice(), project=PROJECT, vendor=VENDOR)

    assert result.filed_path          # the copy still happens
    assert result.archived is False
    assert drive.moved == []
    assert any("2 of them are not filed yet" in w for w in result.warnings)


def test_the_last_page_filed_takes_the_original_with_it(drive):
    _ready(drive)
    _sibling(drive, "inv-2", "filed")
    _sibling(drive, "inv-3", "filed")

    result = filing.file_invoice(invoice=_invoice(), project=PROJECT, vendor=VENDOR)

    assert result.archived is True
    assert drive.moved == [("drive-file-1", "archive-folder")]


def test_a_voided_page_does_not_hold_the_original_forever(drive):
    """SOP §5 yard pages are voided and never entered, and most combined
    files have at least one. Counting them as pending would leave every
    combined original in the intake folder permanently."""
    _ready(drive)
    _sibling(drive, "inv-2", "filed")
    _sibling(drive, "inv-yard", "void")

    result = filing.file_invoice(invoice=_invoice(), project=PROJECT, vendor=VENDOR)
    assert result.archived is True


def test_an_ordinary_single_invoice_pdf_is_archived_immediately(drive):
    _ready(drive)
    result = filing.file_invoice(invoice=_invoice(), project=PROJECT, vendor=VENDOR)
    assert result.archived is True


def test_a_hand_uploaded_invoice_has_no_original_to_archive(drive):
    """There is no Drive file behind it. Saying so beats reporting a failed
    move, which on /uploads would read as something to retry."""
    _ready(drive)
    result = filing.file_invoice(
        invoice=_invoice(source_file_id="upload:abc123", source_path=None),
        project=PROJECT,
        vendor=VENDOR,
    )
    assert result.filed_path
    assert result.archived is False
    assert drive.moved == []
    assert any("uploaded by hand" in w for w in result.warnings)
