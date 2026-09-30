"""
Invoice intake pipeline (prompt §7.1 and §7.2).

    Drive folder → Storage → `ingested` row → AI extraction → `suggested`
                                                           └→ `flagged`

Three properties the spec is explicit about, and which this module is
structured around:

**Idempotent.** Keyed on the Drive file id (plus page, for a future combined
file). Re-running the poll never creates a second record. Re-polling is the
normal case, not the exception — the job runs every 15 minutes over a folder
whose files are not moved until Phase 3.

**Fails loudly.** Any parse or API error lands the invoice in `flagged` with
the error text attached. Nothing is silently skipped, because a skipped
invoice is an unpaid vendor and nobody finds out from an empty list.

**Never touches the Drive original.** §7.1 is explicit that intake downloads
and leaves the file alone; moving to `Uploaded/` happens after a verified
BuilderTrend save, in Phase 3.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from . import audit, drive, storage
from .auth import Actor
from .claude_client import ClaudeNotConfigured, ClaudeOutputInvalid
from .config import settings
from .invoice_extractor import (
    InvoiceExtraction,
    extract_invoice,
    parse_invoice_date,
    resolve_costs,
    resolve_project,
    resolve_vendor,
)
from .invoice_rules import (
    allocate_proportionally,
    bill_no_for,
    costs_balance,
    due_date_for,
    is_yard_invoice,
    signed_amount,
    to_money,
)
from .supabase_client import get_service_client

log = logging.getLogger(__name__)

SYSTEM_ACTOR = Actor(is_agent=True)


@dataclass
class IntakeResult:
    """What one poll run did, for the job response and the /uploads screen."""

    scanned: int = 0
    ingested: int = 0
    suggested: int = 0
    flagged: int = 0
    skipped_existing: int = 0
    errors: list[str] = field(default_factory=list)
    detail: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "ingested": self.ingested,
            "suggested": self.suggested,
            "flagged": self.flagged,
            "skipped_existing": self.skipped_existing,
            "errors": self.errors,
            "detail": self.detail,
        }


def _sb():
    return get_service_client()


# ─── Flagging ─────────────────────────────────────────────────────────


def flag_invoice(
    invoice_id: str,
    *,
    code: str,
    detail: str,
    actor: Actor = SYSTEM_ACTOR,
    duplicate_of: Optional[str] = None,
    current_status: Optional[str] = None,
) -> None:
    """Move an invoice to `flagged`, remembering where it came from.

    `status_before_flag` is what unflag restores, which is why flagging works
    from any state (prompt §6) without needing a separate transition per
    source state.
    """
    now = datetime.now(timezone.utc).isoformat()
    update = {
        "status": "flagged",
        "flagged_at": now,
        "flag_code": code,
        "flag_detail": detail[:2000] if detail else None,
    }
    if current_status and current_status != "flagged":
        update["status_before_flag"] = current_status
    if duplicate_of:
        update["duplicate_of_invoice_id"] = duplicate_of

    _sb().table("invoices").update(update).eq("id", invoice_id).execute()
    audit.log_transition(
        invoice_id=invoice_id,
        action="flagged",
        from_status=current_status or "unknown",
        to_status="flagged",
        actor=actor,
        diff={"flag_code": code, "flag_detail": detail},
    )
    log.info("flagged invoice %s: %s — %s", invoice_id, code, detail)


# ─── Duplicate detection (prompt §7.2) ────────────────────────────────


def find_duplicate(
    *,
    invoice_id: str,
    vendor_id: Optional[str],
    invoice_no: Optional[str],
    amount: Optional[Decimal],
    invoice_date,
) -> Optional[dict]:
    """An existing invoice that looks like this one, or None.

    Two tests, per §7.2: same vendor and invoice number, or same vendor,
    amount, and date. The second catches a re-scan where the invoice number
    was read differently, which is the case a unique constraint misses.

    Voided records are excluded — voiding is how a real duplicate gets
    resolved, so matching against them would make the flag permanent.
    """
    if not vendor_id:
        return None

    base_fields = "id,invoice_no,amount,invoice_date,status,created_at"

    if invoice_no:
        hit = (
            _sb()
            .table("invoices")
            .select(base_fields)
            .eq("vendor_id", vendor_id)
            .eq("invoice_no", invoice_no)
            .neq("id", invoice_id)
            .neq("status", "void")
            .limit(1)
            .execute()
        )
        if hit.data:
            return hit.data[0]

    if amount is not None and invoice_date:
        hit = (
            _sb()
            .table("invoices")
            .select(base_fields)
            .eq("vendor_id", vendor_id)
            .eq("amount", str(amount))
            .eq("invoice_date", str(invoice_date))
            .neq("id", invoice_id)
            .neq("status", "void")
            .limit(1)
            .execute()
        )
        if hit.data:
            return hit.data[0]

    return None


# ─── Suggestion (prompt §7.2) ─────────────────────────────────────────


def apply_extraction(invoice_id: str, *, actor: Actor = SYSTEM_ACTOR) -> str:
    """Run the AI over a stored invoice and land it in `suggested` or `flagged`.

    Returns the resulting status. Used by intake and by the `retry-suggestion`
    endpoint, so a flagged invoice Linda has corrected re-runs through exactly
    the same path rather than a second implementation.
    """
    invoice = (
        _sb().table("invoices").select("*").eq("id", invoice_id).limit(1).execute()
    )
    if not invoice.data:
        raise ValueError(f"invoice {invoice_id} not found")
    row = invoice.data[0]
    from_status = row["status"]

    if not row.get("pdf_storage_path"):
        flag_invoice(
            invoice_id,
            code="unreadable",
            detail="No stored PDF for this invoice; intake did not complete.",
            actor=actor,
            current_status=from_status,
        )
        return "flagged"

    try:
        pdf_bytes = storage.download_bytes(
            storage.INVOICES, row["pdf_storage_path"]
        )
    except Exception as e:
        flag_invoice(
            invoice_id,
            code="unreadable",
            detail=f"Could not read the stored PDF: {e}",
            actor=actor,
            current_status=from_status,
        )
        return "flagged"

    # A project or vendor a person already set on a flagged record is passed
    # in as a hint, so a retry benefits from the correction.
    project_hint = None
    if row.get("project_id"):
        proj = (
            _sb()
            .table("projects")
            .select("project_no,name")
            .eq("id", row["project_id"])
            .limit(1)
            .execute()
        )
        if proj.data:
            project_hint = f"{proj.data[0]['project_no']} ({proj.data[0]['name']})"

    try:
        extraction, meta, ctx = extract_invoice(
            pdf_bytes,
            project_id=row.get("project_id"),
            vendor_id=row.get("vendor_id"),
            source_filename=row.get("source_filename"),
            project_hint=project_hint,
        )
    except ClaudeNotConfigured as e:
        flag_invoice(
            invoice_id,
            code="ai_error",
            detail=str(e),
            actor=actor,
            current_status=from_status,
        )
        return "flagged"
    except ClaudeOutputInvalid as e:
        flag_invoice(
            invoice_id,
            code="ai_error",
            detail=f"The model's response was unusable: {e}",
            actor=actor,
            current_status=from_status,
        )
        return "flagged"
    except Exception as e:
        log.exception("extraction failed for invoice %s", invoice_id)
        flag_invoice(
            invoice_id,
            code="ai_error",
            detail=f"{type(e).__name__}: {e}",
            actor=actor,
            current_status=from_status,
        )
        return "flagged"

    return _persist_extraction(
        row=row,
        extraction=extraction,
        meta=meta,
        ctx=ctx,
        actor=actor,
    )


def _persist_extraction(
    *,
    row: dict,
    extraction: InvoiceExtraction,
    meta: dict,
    ctx: dict,
    actor: Actor,
) -> str:
    """Write the extraction, then decide `suggested` or `flagged`."""
    invoice_id = row["id"]
    from_status = row["status"]

    # The full model output is stored first and unconditionally. Prompt §8
    # requires every prompt and response be logged, and a flag is exactly the
    # case where we most want to see what the model actually said.
    resolved_costs, cost_warnings = resolve_costs(extraction, ctx)
    primary = resolved_costs[0] if resolved_costs else None

    _sb().table("invoice_suggestions").insert(
        {
            "invoice_id": invoice_id,
            "cost_code_id": primary.cost_code_id if primary else None,
            "confidence": primary.confidence if primary else None,
            "rationale": primary.rationale if primary else None,
            "alternatives": primary.alternatives if primary else [],
            "extracted": extraction.model_dump(mode="json"),
            "model": meta.get("model"),
            "prompt_tokens": meta.get("prompt_tokens"),
            "output_tokens": meta.get("output_tokens"),
        }
    ).execute()

    # ── Yard invoices never get entered (SOP §5) ──
    if is_yard_invoice(
        extraction.job_hint,
        extraction.project_match.project_no,
        row.get("source_filename"),
    ):
        flag_invoice(
            invoice_id,
            code="yard",
            detail=(
                "This is a White Cap yard invoice, not a job invoice. SOP §5: "
                "yard invoices are never entered into BuilderTrend."
            ),
            actor=actor,
            current_status=from_status,
        )
        return "flagged"

    vendor, vendor_problem = resolve_vendor(extraction, ctx)
    project, project_flag, project_problem = resolve_project(extraction, ctx)

    invoice_date = parse_invoice_date(extraction.invoice_date)
    amount = signed_amount(to_money(extraction.amount), extraction.is_credit)
    invoice_no = (extraction.invoice_no or "").strip() or None

    # Write everything we did read, even on the way to a flag: Linda triages
    # from a populated form, not a blank one.
    update: dict = {
        "vendor_id": vendor["id"] if vendor else None,
        "invoice_no": invoice_no,
        "bill_no": bill_no_for(invoice_no),
        "invoice_date": str(invoice_date) if invoice_date else None,
        "due_date": str(due_date_for(invoice_date)) if invoice_date else None,
        "amount": str(amount) if amount is not None else None,
        "is_credit": extraction.is_credit,
    }
    # Only overwrite the project when the AI matched one; a project a person
    # already set must not be cleared by a retry that read the job poorly.
    if project:
        update["project_id"] = project["id"]

    _sb().table("invoices").update(update).eq("id", invoice_id).execute()

    _replace_lines(invoice_id, extraction)
    _replace_costs(invoice_id, resolved_costs, amount)

    # ── Flag conditions, most specific first ──
    if vendor_problem:
        flag_invoice(
            invoice_id,
            code="unknown_vendor",
            detail=vendor_problem,
            actor=actor,
            current_status=from_status,
        )
        return "flagged"

    if project_flag:
        flag_invoice(
            invoice_id,
            code=project_flag,
            detail=project_problem or "",
            actor=actor,
            current_status=from_status,
        )
        return "flagged"

    duplicate = find_duplicate(
        invoice_id=invoice_id,
        vendor_id=vendor["id"] if vendor else None,
        invoice_no=invoice_no,
        amount=amount,
        invoice_date=invoice_date,
    )
    if duplicate:
        flag_invoice(
            invoice_id,
            code="possible_duplicate",
            detail=(
                f"Looks like invoice {duplicate.get('invoice_no') or '(no number)'} "
                f"already recorded on {str(duplicate.get('invoice_date'))[:10]} "
                f"for {duplicate.get('amount')} — status {duplicate.get('status')}. "
                "Mark not a duplicate if these are genuinely different bills."
            ),
            actor=actor,
            duplicate_of=duplicate["id"],
            current_status=from_status,
        )
        return "flagged"

    if invoice_date is None or amount is None:
        missing = []
        if invoice_date is None:
            missing.append("invoice date")
        if amount is None:
            missing.append("amount")
        flag_invoice(
            invoice_id,
            code="unreadable",
            detail=(
                f"Could not read the {' and '.join(missing)}. "
                + ("Model notes: " + "; ".join(extraction.flags) if extraction.flags else "")
            ).strip(),
            actor=actor,
            current_status=from_status,
        )
        return "flagged"

    if cost_warnings and not resolved_costs:
        flag_invoice(
            invoice_id,
            code="ai_error",
            detail=" ".join(cost_warnings),
            actor=actor,
            current_status=from_status,
        )
        return "flagged"

    # ── Suggested ──
    # §7.3: the default reviewer is the project's PE, filled automatically at
    # `suggested`. The record is not `assigned` yet — Linda still does that,
    # or overrides the default first.
    stamp: dict = {
        "status": "suggested",
        "flag_code": None,
        "flag_detail": None,
        "flagged_at": None,
        "status_before_flag": None,
    }
    if project:
        if project.get("pe_user_id") and not row.get("reviewer_id"):
            stamp["reviewer_id"] = project["pe_user_id"]
        approver = _project_default_approver(project["id"])
        if approver and not row.get("approver_id"):
            stamp["approver_id"] = approver

    _sb().table("invoices").update(stamp).eq("id", invoice_id).execute()
    audit.log_transition(
        invoice_id=invoice_id,
        action="suggested",
        from_status=from_status,
        to_status="suggested",
        actor=actor,
        diff={
            "suggested_code": primary.code if primary else None,
            "confidence": primary.confidence if primary else None,
            "cost_rows": len(resolved_costs),
            "warnings": cost_warnings or None,
        },
    )
    return "suggested"


def _project_default_approver(project_id: str) -> Optional[str]:
    res = (
        _sb()
        .table("projects")
        .select("default_approver_id")
        .eq("id", project_id)
        .limit(1)
        .execute()
    )
    return res.data[0]["default_approver_id"] if res.data else None


def _replace_lines(invoice_id: str, extraction: InvoiceExtraction) -> None:
    """Rewrite the line items. Safe to re-run — a retry replaces, not appends."""
    _sb().table("invoice_lines").delete().eq("invoice_id", invoice_id).execute()
    if not extraction.lines:
        return
    _sb().table("invoice_lines").insert(
        [
            {
                "invoice_id": invoice_id,
                "ticket_no": line.ticket,
                "prod_num": line.prod_num,
                "description": line.description,
                "qty": line.qty,
                "uom": line.uom,
                "unit_price": line.unit_price,
                "gross": line.gross,
                "sort_order": i,
            }
            for i, line in enumerate(extraction.lines)
        ]
    ).execute()


def _replace_costs(
    invoice_id: str, resolved: list, amount: Optional[Decimal]
) -> None:
    """Rewrite the cost rows, forcing them to sum to the invoice total.

    The model is told to make its amounts sum, and mostly does. When it does
    not, reallocating proportionally here beats storing rows that cannot be
    approved: prompt §5 blocks approval on an unbalanced split, and a
    reviewer facing a silent one-cent gap has no way to tell whether it is a
    rounding artifact or a misread line.
    """
    _sb().table("invoice_costs").delete().eq("invoice_id", invoice_id).execute()
    if not resolved:
        return

    amounts = [to_money(r.amount) for r in resolved]

    if amount is not None:
        balanced, _ = costs_balance(amount, amounts)
        if not balanced:
            if len(resolved) == 1:
                # One row: it is simply the whole invoice.
                amounts = [amount]
            else:
                weights = [
                    abs(a) if a is not None else Decimal("0") for a in amounts
                ]
                amounts = allocate_proportionally(amount, weights)

    _sb().table("invoice_costs").insert(
        [
            {
                "invoice_id": invoice_id,
                "cost_code_id": r.cost_code_id,
                "amount": str(amounts[i]) if amounts[i] is not None else "0",
                "sort_order": i,
            }
            for i, r in enumerate(resolved)
        ]
    ).execute()


# ─── Polling (prompt §7.1) ────────────────────────────────────────────


def poll_drive(*, actor: Actor = SYSTEM_ACTOR, limit: Optional[int] = None) -> IntakeResult:
    """Scan the Drive intake folders and process anything new.

    Honors the §12 kill switch. Never moves or deletes a Drive file.
    """
    result = IntakeResult()

    if settings.jobs_paused:
        result.errors.append(
            "JOBS_PAUSED is set; Drive polling is off. Unset it to resume."
        )
        return result

    if not settings.drive_enabled:
        result.errors.append(
            "Drive is not configured (GOOGLE_DRIVE_CREDENTIALS_JSON and "
            "DRIVE_FOLDER_INVOICE_UPLOADS). Polling is unavailable."
        )
        return result

    folders: list[tuple[str, str, bool]] = [
        ("Invoice Uploads", settings.drive_folder_invoice_uploads, False),
    ]
    if settings.drive_folder_white_cap:
        # SOP §2 puts already-split White Cap invoices in per-job subfolders,
        # so this folder is walked one level deep.
        folders.append(("White Cap", settings.drive_folder_white_cap, True))

    for label, folder_id, recurse in folders:
        if not folder_id:
            continue
        try:
            files = drive.list_pdfs(folder_id, recurse_into_subfolders=recurse)
        except Exception as e:
            log.exception("could not list Drive folder %s", label)
            result.errors.append(f"Could not list {label}: {e}")
            continue

        for entry in files:
            if limit is not None and result.ingested >= limit:
                break
            result.scanned += 1
            try:
                outcome = _process_file(entry, source_label=label, actor=actor)
            except Exception as e:
                log.exception("intake failed for Drive file %s", entry.get("id"))
                result.errors.append(f"{entry.get('name')}: {e}")
                result.detail.append(
                    {"file": entry.get("name"), "outcome": "error", "detail": str(e)}
                )
                continue

            result.detail.append({"file": entry.get("name"), **outcome})
            if outcome["outcome"] == "existing":
                result.skipped_existing += 1
            else:
                result.ingested += 1
                if outcome["outcome"] == "suggested":
                    result.suggested += 1
                elif outcome["outcome"] == "flagged":
                    result.flagged += 1

    log.info(
        "poll complete: scanned=%d ingested=%d suggested=%d flagged=%d existing=%d",
        result.scanned,
        result.ingested,
        result.suggested,
        result.flagged,
        result.skipped_existing,
    )
    return result


def _process_file(entry: dict, *, source_label: str, actor: Actor) -> dict:
    """Ingest one Drive PDF. Idempotent on (file id, page)."""
    file_id = entry["id"]
    filename = entry.get("name") or "invoice.pdf"

    existing = (
        _sb()
        .table("invoices")
        .select("id,status")
        .eq("source_file_id", file_id)
        .eq("source_page", 1)
        .limit(1)
        .execute()
    )
    if existing.data:
        return {
            "outcome": "existing",
            "invoice_id": existing.data[0]["id"],
            "detail": f"Already ingested, status {existing.data[0]['status']}.",
        }

    # Create the row BEFORE the download. The unique index on
    # (source_file_id, source_page) is what makes the poll idempotent, so it
    # has to be claimed before any slow work — two overlapping cron firings
    # would otherwise both download and both insert.
    created = (
        _sb()
        .table("invoices")
        .insert(
            {
                "source_file_id": file_id,
                "source_page": 1,
                "source_filename": filename,
                "source_path": f"{source_label}/"
                + (entry.get("parent_folder_name") + "/" if entry.get("parent_folder_name") else "")
                + filename,
                "status": "ingested",
            }
        )
        .execute()
    )
    if not created.data:
        raise RuntimeError("invoice insert returned no row")
    invoice_id = created.data[0]["id"]

    audit.log_transition(
        invoice_id=invoice_id,
        action="ingested",
        from_status="none",
        to_status="ingested",
        actor=actor,
        diff={"source_file_id": file_id, "source_filename": filename},
    )

    try:
        pdf_bytes = drive.download(file_id)
    except Exception as e:
        flag_invoice(
            invoice_id,
            code="unreadable",
            detail=f"Could not download from Drive: {e}",
            actor=actor,
            current_status="ingested",
        )
        return {"outcome": "flagged", "invoice_id": invoice_id, "detail": str(e)}

    path = storage.make_invoice_pdf_path(invoice_id)
    try:
        storage.upload_bytes(storage.INVOICES, path, pdf_bytes)
    except Exception as e:
        flag_invoice(
            invoice_id,
            code="unreadable",
            detail=f"Could not store the PDF: {e}",
            actor=actor,
            current_status="ingested",
        )
        return {"outcome": "flagged", "invoice_id": invoice_id, "detail": str(e)}

    _sb().table("invoices").update({"pdf_storage_path": path}).eq(
        "id", invoice_id
    ).execute()

    # A file straight out of the White Cap root is a combined multi-job PDF.
    # The splitter is Phase 4; until then, flag rather than let the extractor
    # read a 12-page bundle as one invoice and produce a confident wrong total.
    if source_label == "White Cap" and not entry.get("parent_folder_name"):
        flag_invoice(
            invoice_id,
            code="stop_and_ask",
            detail=(
                "This is a combined White Cap file covering several jobs. The "
                "page splitter is a Phase 4 feature; split it by CUSTOMER JOB "
                "NO. into White Cap/[Job Name]/ for now, per SOP §5."
            ),
            actor=actor,
            current_status="ingested",
        )
        return {
            "outcome": "flagged",
            "invoice_id": invoice_id,
            "detail": "Combined White Cap file, needs splitting.",
        }

    status = apply_extraction(invoice_id, actor=actor)
    return {"outcome": status, "invoice_id": invoice_id, "detail": ""}
