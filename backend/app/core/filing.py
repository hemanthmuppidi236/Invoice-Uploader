"""
Filing a processed invoice back to Drive (prompt §7.7).

Two steps, after the bill is confirmed saved in BuilderTrend:

  1. Copy the PDF to `BT Invoices/[Project]/[Vendor]/`, named to match the
     files already in that folder.
  2. Move the Drive original into `Uploaded/`.

§14 decided this runs in the backend rather than in the Chrome session,
"since it is deterministic". Two things bear that out. The naming convention
differs per vendor folder and has to be inferred from what is already there,
which is a parsing job rather than a browsing one. And SOP §9 records five
invoices filed with truncated names because a `$` in a filename went through
a shell unescaped — going through the Drive API means filenames are data, not
shell words, so that class of bug cannot recur.

Two rules are structural here, not conditional:

  - **Never create a folder.** §7.7 and §12 both say a missing project or
    vendor folder is a stop-and-ask. Creating one silently files invoices
    somewhere nobody is looking.
  - **Never delete.** The original is moved by reassigning its parent, so the
    Drive file id survives and re-polling still recognises it as ingested.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from . import audit, drive, storage
from .config import settings
from .invoice_rules import is_uploaded_source
from .supabase_client import get_service_client

log = logging.getLogger(__name__)


class FilingError(Exception):
    """Filing could not complete. The message is shown to a person.

    `needs_folder` marks the case where somebody has to create a Drive folder
    by hand — the app will not do it, so retrying changes nothing until they
    have.
    """

    def __init__(self, message: str, *, needs_folder: bool = False):
        super().__init__(message)
        self.needs_folder = needs_folder


@dataclass
class FilingResult:
    filed_path: str
    filed_file_id: Optional[str] = None
    archived: bool = False
    warnings: list[str] = None

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


# ─── Filename convention ──────────────────────────────────────────────
#
# SOP §7: "naming it to match the existing files in that folder (formats vary:
# `26 09-23 $447.46.pdf`, `09-14-26 $2,600.00.pdf`, etc.)". So the convention
# is per-folder and has to be read, not assumed. These are the shapes seen in
# the folders so far; an unrecognised folder falls back to the documented
# default.

_MONEY = r"\$[\d,]+\.\d{2}"

CONVENTIONS: list[tuple[str, str]] = [
    # "09-14-26 $2,600.00.pdf"
    (rf"^\d{{2}}-\d{{2}}-\d{{2}}\s+{_MONEY}\.pdf$", "MM-DD-YY $amount"),
    # "26 09-23 $447.46.pdf"
    (rf"^\d{{2}}\s+\d{{2}}-\d{{2}}\s+{_MONEY}\.pdf$", "YY MM-DD $amount"),
    # "2026-09-14 $2,600.00.pdf"
    (rf"^\d{{4}}-\d{{2}}-\d{{2}}\s+{_MONEY}\.pdf$", "YYYY-MM-DD $amount"),
]

DEFAULT_CONVENTION = "MM-DD-YY $amount"


def detect_convention(existing_filenames: list[str]) -> tuple[str, Optional[str]]:
    """Infer a folder's naming convention from what is in it.

    Returns (convention, note). The note is set when the folder gave no clear
    answer, so the caller can surface that rather than quietly imposing a
    format on a folder that uses another one.

    Majority wins rather than first-match: a folder with twenty files in one
    format and one stray in another should keep the twenty.
    """
    if not existing_filenames:
        return DEFAULT_CONVENTION, (
            "The vendor folder is empty, so there was no convention to match. "
            f"Used the default, {DEFAULT_CONVENTION}."
        )

    counts: dict[str, int] = {}
    for name in existing_filenames:
        for pattern, convention in CONVENTIONS:
            if re.match(pattern, name.strip(), re.I):
                counts[convention] = counts.get(convention, 0) + 1
                break

    if not counts:
        return DEFAULT_CONVENTION, (
            f"None of the {len(existing_filenames)} files in the vendor folder "
            f"match a known naming pattern. Used the default, "
            f"{DEFAULT_CONVENTION} — check the filed copy."
        )

    best = max(counts.items(), key=lambda kv: kv[1])
    matched = sum(counts.values())
    note = None
    if len(counts) > 1:
        note = (
            f"The vendor folder mixes {len(counts)} naming formats; used the "
            f"most common one ({best[0]}, {best[1]} of {matched} files)."
        )
    return best[0], note


def build_filename(
    *, convention: str, invoice_date: Optional[date], amount: Optional[Decimal]
) -> str:
    """Render the filed copy's name.

    A credit memo is written with the minus sign rather than in parentheses:
    the folder is browsed by humans looking for an amount, and `-$500.00`
    reads as a credit at a glance where `($500.00)` looks like a filename
    quirk.
    """
    when = invoice_date or datetime.utcnow().date()
    money = "$0.00" if amount is None else f"${abs(amount):,.2f}"
    if amount is not None and amount < 0:
        money = "-" + money

    if convention == "YY MM-DD $amount":
        stamp = when.strftime("%y %m-%d")
    elif convention == "YYYY-MM-DD $amount":
        stamp = when.strftime("%Y-%m-%d")
    else:
        stamp = when.strftime("%m-%d-%y")

    return f"{stamp} {money}.pdf"


def disambiguate(filename: str, existing: list[str]) -> tuple[str, Optional[str]]:
    """Avoid overwriting a same-named file.

    SOP §7 makes a filename collision a stop-and-ask when the sizes differ,
    because that means two different invoices on the same day for the same
    amount. Rather than stop the whole batch, the copy is suffixed and the
    collision is reported — a filed duplicate is recoverable, a lost invoice
    is not.
    """
    if filename not in existing:
        return filename, None

    stem, _, ext = filename.rpartition(".")
    for n in range(2, 50):
        candidate = f"{stem} ({n}).{ext}"
        if candidate not in existing:
            return candidate, (
                f"A file named {filename!r} was already in the vendor folder, "
                f"so this copy was filed as {candidate!r}. Check whether they "
                "are the same invoice."
            )
    raise FilingError(
        f"Could not find a free filename near {filename!r} after 48 tries."
    )


# ─── Filing ───────────────────────────────────────────────────────────


def file_invoice(
    *,
    invoice: dict,
    project: Optional[dict],
    vendor: Optional[dict],
    archive: bool = True,
) -> FilingResult:
    """Copy the PDF into the vendor folder and archive the Drive original.

    Raises FilingError with a message meant for a person. Nothing partial is
    left behind on the copy step; if the archive step fails after a successful
    copy, that is reported as a warning rather than an error, because the copy
    is the part that matters and re-running would file a second copy.
    """
    if not settings.drive_enabled:
        raise FilingError(
            "Drive is not configured, so the filed copy could not be made. "
            "The bill is in BuilderTrend; file it by hand or configure Drive "
            "and retry."
        )
    if not settings.drive_folder_bt_invoices:
        raise FilingError(
            "DRIVE_FOLDER_BT_INVOICES is not set, so there is nowhere to file "
            "to. The bill is in BuilderTrend."
        )

    project_folder_name = (project or {}).get("drive_folder_name") or (
        project or {}
    ).get("name")
    # SOP §6: the Drive folder spelling sometimes differs from both the
    # letterhead and the BuilderTrend name — "Holliday Rock" files under
    # "Holiday Rock". Always prefer the recorded folder name.
    vendor_folder_name = (vendor or {}).get("drive_folder_name") or (
        vendor or {}
    ).get("invoice_name")

    if not project_folder_name:
        raise FilingError(
            "This invoice has no project, so there is no folder to file it "
            "under.",
            needs_folder=True,
        )
    if not vendor_folder_name:
        raise FilingError(
            "This invoice has no vendor, so there is no folder to file it "
            "under.",
            needs_folder=True,
        )

    project_folder = drive.find_folder(
        settings.drive_folder_bt_invoices, project_folder_name
    )
    if not project_folder:
        raise FilingError(
            f"No folder named {project_folder_name!r} under BT Invoices. "
            "The app never creates Drive folders — create it, then retry.",
            needs_folder=True,
        )

    vendor_folder = drive.find_folder(project_folder["id"], vendor_folder_name)
    if not vendor_folder:
        raise FilingError(
            f"No folder named {vendor_folder_name!r} under "
            f"BT Invoices/{project_folder_name}. The app never creates Drive "
            "folders — create it, then retry.",
            needs_folder=True,
        )

    existing = drive.list_filenames(vendor_folder["id"])
    convention, convention_note = detect_convention(existing)

    amount = invoice.get("amount")
    invoice_date = invoice.get("invoice_date")
    if isinstance(invoice_date, str):
        try:
            invoice_date = date.fromisoformat(invoice_date[:10])
        except ValueError:
            invoice_date = None

    filename = build_filename(
        convention=convention,
        invoice_date=invoice_date,
        amount=Decimal(str(amount)) if amount is not None else None,
    )
    filename, collision_note = disambiguate(filename, existing)

    if not invoice.get("pdf_storage_path"):
        raise FilingError(
            "No stored PDF for this invoice, so there is nothing to file."
        )
    pdf_bytes = storage.download_bytes(
        storage.INVOICES, invoice["pdf_storage_path"]
    )

    created = drive.upload_copy(
        folder_id=vendor_folder["id"], filename=filename, data=pdf_bytes
    )

    filed_path = f"BT Invoices/{project_folder_name}/{vendor_folder_name}/{filename}"
    result = FilingResult(filed_path=filed_path, filed_file_id=created.get("id"))
    for note in (convention_note, collision_note):
        if note:
            result.warnings.append(note)

    # ── Archive the original ──
    #
    # Deliberately after the copy, and non-fatal. If this fails the invoice is
    # still filed; re-running the whole thing would put a second copy in the
    # vendor folder, which is worse than an original left in place.
    if is_uploaded_source(invoice.get("source_file_id")):
        # Nothing to archive: this invoice was handed to the app directly, so
        # there is no Drive original sitting in an intake folder. Said out
        # loud rather than left silent, because `original_archived: false`
        # otherwise reads as a failure on the /uploads screen.
        result.warnings.append(
            "This invoice was uploaded by hand, so there is no Drive "
            "original to archive. The filed copy is in place."
        )
    elif archive and invoice.get("source_file_id"):
        try:
            archive_folder = _find_archive_folder(invoice)
            if archive_folder:
                drive.move(invoice["source_file_id"], to_folder_id=archive_folder)
                result.archived = True
            else:
                result.warnings.append(
                    "Filed the copy, but found no Uploaded/ folder to move the "
                    "original into. Move it by hand; nothing is deleted."
                )
        except Exception as e:
            log.exception("archiving failed for invoice %s", invoice.get("id"))
            result.warnings.append(
                f"Filed the copy, but could not move the original to "
                f"Uploaded/ ({e}). Move it by hand; nothing is deleted."
            )

    log.info("filed invoice %s to %s", invoice.get("id"), filed_path)
    return result


def _find_archive_folder(invoice: dict) -> Optional[str]:
    """The `Uploaded/` folder for whichever intake folder this came from.

    SOP §2 keeps two: `Invoice Uploads/Uploaded/` and, for White Cap,
    `White Cap/Uploaded/[Job Name]/`. Only the first level is resolved here —
    the per-job White Cap subfolder arrives with the Phase 4 splitter.
    """
    source_path = invoice.get("source_path") or ""
    parent = (
        settings.drive_folder_white_cap
        if source_path.startswith("White Cap")
        else settings.drive_folder_invoice_uploads
    )
    if not parent:
        return None
    folder = drive.find_folder(parent, "Uploaded")
    return folder["id"] if folder else None


# ─── Recording the outcome ────────────────────────────────────────────


def file_and_record(*, invoice: dict, actor) -> dict:
    """File one invoice and write the outcome to its row.

    Returns a dict the API hands straight back to the caller:

        {"filed": bool, "filed_path": str|None, "error": str|None,
         "needs_folder": bool, "warnings": [str], "invoice": row|None}

    A failure here is recorded, not raised. By the time this runs the bill is
    already saved in BuilderTrend, and the Chrome session has moved on to the
    next invoice; turning a missing Drive folder into a 500 would make the
    session look like the *upload* failed. The invoice stays in `uploaded`
    with `filing_error` set, which is exactly the state /uploads lists for a
    retry.
    """
    from . import state_machine  # local: state_machine imports nothing here,
    # but keeping the import inside the function keeps the module importable
    # in isolation for the filename tests.

    sb = get_service_client()

    project = None
    if invoice.get("project_id"):
        rows = (
            sb.table("projects")
            .select("id,name,project_no,drive_folder_name")
            .eq("id", invoice["project_id"])
            .limit(1)
            .execute()
            .data
            or []
        )
        project = rows[0] if rows else None

    vendor = None
    if invoice.get("vendor_id"):
        rows = (
            sb.table("vendors")
            .select("id,invoice_name,bt_name,drive_folder_name")
            .eq("id", invoice["vendor_id"])
            .limit(1)
            .execute()
            .data
            or []
        )
        vendor = rows[0] if rows else None

    try:
        result = file_invoice(invoice=invoice, project=project, vendor=vendor)
    except FilingError as e:
        sb.table("invoices").update(
            {"filing_error": str(e), "filing_warnings": []}
        ).eq("id", invoice["id"]).execute()
        audit.record(
            entity_type="invoice",
            entity_id=invoice["id"],
            invoice_id=invoice["id"],
            action="filing_failed",
            actor=actor,
            diff={"error": str(e), "needs_folder": e.needs_folder},
        )
        log.warning("filing failed for invoice %s: %s", invoice["id"], e)
        return {
            "filed": False,
            "filed_path": None,
            "error": str(e),
            "needs_folder": e.needs_folder,
            "warnings": [],
            "invoice": None,
        }
    except Exception as e:  # noqa: BLE001 — any Drive or Storage error
        log.exception("filing blew up for invoice %s", invoice["id"])
        message = (
            f"Filing failed unexpectedly: {e}. The bill is in BuilderTrend; "
            "retry filing from /uploads."
        )
        sb.table("invoices").update(
            {"filing_error": message, "filing_warnings": []}
        ).eq("id", invoice["id"]).execute()
        audit.record(
            entity_type="invoice",
            entity_id=invoice["id"],
            invoice_id=invoice["id"],
            action="filing_failed",
            actor=actor,
            diff={"error": str(e)},
        )
        return {
            "filed": False,
            "filed_path": None,
            "error": message,
            "needs_folder": False,
            "warnings": [],
            "invoice": None,
        }

    updated = state_machine.apply(
        invoice=invoice,
        transition=state_machine.MARK_FILED,
        actor=actor,
        extra_fields={
            "filed_path": result.filed_path,
            "filed_file_id": result.filed_file_id,
            "original_archived": result.archived,
            "filing_warnings": result.warnings,
            "filing_error": None,
        },
        diff={"filed_path": result.filed_path, "archived": result.archived},
    )
    return {
        "filed": True,
        "filed_path": result.filed_path,
        "error": None,
        "needs_folder": False,
        "warnings": result.warnings,
        "invoice": updated,
    }
