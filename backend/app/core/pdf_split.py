"""
Splitting a combined vendor PDF into one document per page (SOP §5).

White Cap sends a single PDF covering many jobs, and the SOP is explicit
about the rule: **one page is one invoice**. A twelve-page bundle is twelve
payables on up to twelve different jobs, and reading it as one document
produces a confident, wrong, five-figure total — the failure mode that gets
paid before anyone notices.

Everything here is mechanical: no Claude call, no database. Each page comes
out as its own standalone PDF so that the rest of the pipeline cannot tell
the difference between a split page and a file that arrived on its own. That
is the point — one extraction path, one set of flag reasons, one thing to
test.
"""

import io
import logging
from typing import Optional

log = logging.getLogger(__name__)

# A bundle larger than this is a scanning accident, not a vendor statement.
# The cap is here so one malformed file cannot turn a poll run into hundreds
# of Claude calls before anybody looks at it.
MAX_PAGES = 60


class PdfSplitError(Exception):
    """The PDF could not be read or split. The message is shown to a person."""


def page_count(pdf_bytes: bytes) -> int:
    """How many pages, without splitting.

    Used to decide whether a file needs splitting at all — a one-page White
    Cap invoice is just an invoice, and routing it through the splitter would
    only add a layer to debug through.
    """
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        return len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    except (PdfReadError, Exception) as e:  # noqa: BLE001
        raise PdfSplitError(
            f"This file could not be read as a PDF ({e}). It may be a scan "
            "that failed, or encrypted."
        )


def split_pages(pdf_bytes: bytes, *, max_pages: int = MAX_PAGES) -> list[bytes]:
    """One standalone single-page PDF per page, in document order.

    Order matters and is preserved: `source_page` is half of the uniqueness
    key that makes re-polling idempotent, so page 3 has to be page 3 on every
    run. Nothing here depends on content, so it is.
    """
    from pypdf import PdfReader, PdfWriter
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except (PdfReadError, Exception) as e:  # noqa: BLE001
        raise PdfSplitError(f"This file could not be read as a PDF ({e}).")

    if reader.is_encrypted:
        # Trying an empty password first is worth it: plenty of vendor PDFs
        # are "encrypted" with no password at all, purely to set permissions.
        try:
            reader.decrypt("")
        except Exception:
            raise PdfSplitError(
                "This PDF is password protected, so its pages cannot be "
                "separated. Ask the vendor for an unprotected copy."
            )

    total = len(reader.pages)
    if total == 0:
        raise PdfSplitError("This PDF has no pages.")
    if total > max_pages:
        raise PdfSplitError(
            f"This PDF has {total} pages, over the {max_pages}-page limit. "
            "That is usually a scanning accident rather than a statement — "
            "check it before splitting, then split it by hand."
        )

    out: list[bytes] = []
    for index in range(total):
        writer = PdfWriter()
        writer.add_page(reader.pages[index])
        buffer = io.BytesIO()
        writer.write(buffer)
        out.append(buffer.getvalue())

    log.info("split a %d-page PDF into %d single-page documents", total, total)
    return out


def page_text(pdf_bytes: bytes) -> str:
    """The page's embedded text, or "" when there is none.

    A cheap pre-read, used only to recognise a page that should not become an
    invoice at all — SOP §5's yard pages. It is deliberately not used for any
    field: White Cap's PDFs are often scans with no text layer, and a rule
    that works on half the pages is worse than one that works on none,
    because the half it misses looks like success.
    """
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception:  # noqa: BLE001
        return ""


def customer_job_no(text: str) -> Optional[str]:
    """The job name printed under "CUSTOMER JOB NO.", if the page has text.

    SOP §5 names this field as the thing that identifies which job a White
    Cap page belongs to. Pulling it out here gives the model a stated hint
    rather than leaving it to find the right line among twelve similar ones —
    but it is a hint, never the answer: `resolve_project` still has to match
    it, and an unmatched job flags rather than guesses.
    """
    import re

    match = re.search(
        r"CUSTOMER\s+JOB\s+NO\.?\s*[:\-]?\s*(.+)", text, re.I
    )
    if not match:
        return None
    value = match.group(1).splitlines()[0].strip()
    return value or None
