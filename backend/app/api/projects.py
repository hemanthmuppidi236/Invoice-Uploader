"""
Projects API — onboarding (prompt §7.0) and project detail (§10).

A project is not eligible for invoice routing until `onboarded_at` is stamped,
which requires a PE and — for projects with a concrete supplier — a confirmed
mix design. The gate lives in `onboarding_blockers()` and is enforced by the
two endpoints that can stamp it, never by a generic PATCH.

The mix design flow is deliberately two calls:

    POST /projects/parse-mix-design   → upload + AI parse, nothing saved
    POST /projects                    → create with the confirmed rows

Prompt §7.0 requires the parsed table be shown for confirmation before
saving, so the parse step must not write mix_designs rows. It does store the
PDF (the file has to live somewhere to be re-read), and returns the path for
the create call to attach.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from ..core import audit, storage
from ..core.auth import (
    Actor,
    CurrentUser,
    as_actor,
    get_current_user,
    require_role,
)
from ..core.claude_client import (
    MAX_PDF_BYTES,
    ClaudeNotConfigured,
    ClaudeOutputInvalid,
)
from ..core.config import settings
from ..core.mix_design_parser import parse_submittal, resolve_proposals
from ..core.supabase_client import get_service_client
from ..schemas.projects import (
    MixDesignConfirm,
    MixDesignParseOut,
    MixDesignRowOut,
    MixDesignRowPatch,
    OnboardingStatus,
    ProjectCreate,
    ProjectDetail,
    ProjectOut,
    ProjectUpdate,
    SignedUrlOut,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["projects"])


# ─── Onboarding gate ──────────────────────────────────────────────────


def onboarding_blockers(project: dict, mix_rows: list[dict]) -> list[str]:
    """What still stops this project from receiving routed invoices.

    Prompt §7.0: a PE is always required; a mix design is required for any
    project with a concrete supplier. Unmapped mix rows are NOT a blocker —
    a row with a null cost_code_id simply means the AI will not use that mix
    to guess a concrete code, which is the documented behavior, so a submittal
    listing mixes the job never pours should not hold onboarding hostage.
    """
    blockers: list[str] = []

    if not project.get("pe_user_id"):
        blockers.append("No project engineer assigned.")

    if project.get("has_concrete_supplier", True):
        live = [r for r in mix_rows if not r.get("superseded_at")]
        if not live:
            blockers.append(
                "No mix design on file. Upload the concrete supplier's "
                "submittal, or clear 'has concrete supplier' if this job has "
                "no ready-mix scope."
            )

    return blockers


def onboarding_status(project: dict, mix_rows: list[dict]) -> OnboardingStatus:
    live = [r for r in mix_rows if not r.get("superseded_at")]
    return OnboardingStatus(
        onboarded=bool(project.get("onboarded_at")),
        blockers=onboarding_blockers(project, mix_rows),
        mix_design_rows=len(live),
        mix_design_rows_mapped=sum(1 for r in live if r.get("cost_code_id")),
    )


# ─── Helpers ──────────────────────────────────────────────────────────


def _user_names() -> dict[str, str]:
    sb = get_service_client()
    res = sb.table("app_users").select("id,name,email").execute()
    return {
        r["id"]: (r.get("name") or r.get("email") or "")
        for r in (res.data or [])
    }


def _cost_code_labels() -> dict[str, str]:
    sb = get_service_client()
    res = sb.table("cost_codes").select("id,code").execute()
    return {r["id"]: r["code"] for r in (res.data or [])}


def _fetch_project(project_id: str) -> dict:
    sb = get_service_client()
    res = (
        sb.table("projects")
        .select("*")
        .eq("id", project_id)
        .is_("deleted_at", "null")
        .limit(1)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Project not found")
    return res.data[0]


def _fetch_mix_rows(project_id: str) -> list[dict]:
    sb = get_service_client()
    res = (
        sb.table("mix_designs")
        .select("*")
        .eq("project_id", project_id)
        .order("revision")
        .order("mix_no")
        .execute()
    )
    return res.data or []


def _validate_cost_code_ids(rows: list, field: str = "cost_code_id") -> None:
    """Reject a mix row pointing at a cost code that does not exist.

    The FK would catch it, but as a 500 from PostgREST. Prompt §12 forbids
    creating a cost code from the app, so an unknown id is a client bug worth
    a clear 422.
    """
    ids = {getattr(r, field) for r in rows if getattr(r, field, None)}
    if not ids:
        return
    sb = get_service_client()
    res = sb.table("cost_codes").select("id").in_("id", list(ids)).execute()
    known = {r["id"] for r in (res.data or [])}
    missing = ids - known
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown cost_code_id(s): {', '.join(sorted(missing))}",
        )


def _validate_user_ids(project: dict) -> None:
    """Reject a PE or approver id that is not a real, active user."""
    ids = {
        project.get("pe_user_id"),
        project.get("default_approver_id"),
    } - {None}
    if not ids:
        return
    sb = get_service_client()
    res = (
        sb.table("app_users")
        .select("id,deactivated_at")
        .in_("id", list(ids))
        .execute()
    )
    rows = {r["id"]: r for r in (res.data or [])}
    for uid in ids:
        if uid not in rows:
            raise HTTPException(status_code=422, detail=f"Unknown user id: {uid}")
        if rows[uid].get("deactivated_at"):
            raise HTTPException(
                status_code=422,
                detail=f"User {uid} is deactivated and cannot be assigned.",
            )


def _to_out(project: dict, mix_rows: list[dict], names: dict, counts: dict) -> ProjectOut:
    return ProjectOut(
        **{k: project.get(k) for k in ProjectOut.model_fields if k in project},
        pe_name=names.get(project.get("pe_user_id") or ""),
        default_approver_name=names.get(project.get("default_approver_id") or ""),
        onboarding=onboarding_status(project, mix_rows),
        invoice_count=counts.get(project["id"], 0),
    )


def _invoice_counts() -> dict[str, int]:
    """Invoices per project, for the list screen.

    One query and a Python tally: the row count here is bounded by the
    invoice table, and a per-project count query would be N round trips.
    """
    sb = get_service_client()
    res = sb.table("invoices").select("project_id").execute()
    counts: dict[str, int] = {}
    for r in res.data or []:
        pid = r.get("project_id")
        if pid:
            counts[pid] = counts.get(pid, 0) + 1
    return counts


# ─── List and read ────────────────────────────────────────────────────


@router.get("", response_model=list[ProjectOut])
def list_projects(
    status_filter: Optional[str] = None,
    onboarded: Optional[bool] = None,
    user: CurrentUser = Depends(get_current_user),
):
    """All projects. Reads only require authentication (a viewer sees them)."""
    sb = get_service_client()
    q = sb.table("projects").select("*").is_("deleted_at", "null")
    if status_filter in ("old", "active"):
        q = q.eq("status", status_filter)
    if onboarded is True:
        q = q.not_.is_("onboarded_at", "null")
    elif onboarded is False:
        q = q.is_("onboarded_at", "null")
    projects = (q.order("project_no").execute().data) or []

    if not projects:
        return []

    # One mix_designs query for every project on screen, then group.
    ids = [p["id"] for p in projects]
    mix_res = (
        sb.table("mix_designs")
        .select("project_id,cost_code_id,superseded_at")
        .in_("project_id", ids)
        .execute()
    )
    by_project: dict[str, list[dict]] = {}
    for r in mix_res.data or []:
        by_project.setdefault(r["project_id"], []).append(r)

    names = _user_names()
    counts = _invoice_counts()
    return [
        _to_out(p, by_project.get(p["id"], []), names, counts) for p in projects
    ]


@router.get("/{project_id}", response_model=ProjectDetail)
def get_project(project_id: str, user: CurrentUser = Depends(get_current_user)):
    project = _fetch_project(project_id)
    mix_rows = _fetch_mix_rows(project_id)
    names = _user_names()
    labels = _cost_code_labels()
    counts = _invoice_counts()

    base = _to_out(project, mix_rows, names, counts)
    return ProjectDetail(
        **base.model_dump(),
        mix_designs=[
            MixDesignRowOut(
                **{k: r.get(k) for k in MixDesignRowOut.model_fields if k in r},
                cost_code=labels.get(r.get("cost_code_id") or ""),
            )
            for r in mix_rows
        ],
    )


@router.get("/{project_id}/mix-design-url", response_model=SignedUrlOut)
def get_mix_design_url(
    project_id: str, user: CurrentUser = Depends(get_current_user)
):
    """Signed URL for the stored submittal PDF.

    Not role-gated beyond authentication — same posture as the pay app, where
    downloads are open to any signed-in viewer.
    """
    project = _fetch_project(project_id)
    path = project.get("mix_design_pdf_path")
    if not path:
        raise HTTPException(
            status_code=404, detail="This project has no mix design on file."
        )
    return SignedUrlOut(url=storage.signed_url(storage.MIX_DESIGNS, path))


# ─── Mix design parse (nothing saved) ─────────────────────────────────


@router.post("/parse-mix-design", response_model=MixDesignParseOut)
async def parse_mix_design(
    file: UploadFile = File(...),
    project_no: Optional[str] = Form(default=None),
    project_name: Optional[str] = Form(default=None),
    project_id: Optional[str] = Form(default=None),
    revision: str = Form(default="CMD-01"),
    user: CurrentUser = Depends(require_role("admin", "accountant", "pe")),
):
    """Upload a submittal, parse the yardage sheet, return rows to confirm.

    Writes the PDF to Storage but no mix_designs rows — the reviewer confirms
    the table first (prompt §7.0). The returned `storage_path` is passed back
    on create/confirm so the file and the rows are attached together.
    """
    if not settings.claude_enabled:
        raise HTTPException(
            status_code=503,
            detail=(
                "ANTHROPIC_API_KEY is not configured, so mix design parsing "
                "is unavailable. Set it in the backend environment."
            ),
        )

    filename = file.filename or "mix-design.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=422,
            detail="Mix design submittals must be PDFs.",
        )

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=422, detail="The uploaded file is empty.")

    # Size check BEFORE the upload. The Claude API caps a request at 32 MB and
    # base64 inflates by a third, so an oversized PDF cannot be parsed —
    # storing it first would just leave an orphan in the bucket.
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"That PDF is {len(pdf_bytes) / 1_048_576:.1f} MB; the limit "
                f"is {MAX_PDF_BYTES / 1_048_576:.0f} MB. Extract just the "
                "yardage sheet pages, or downsample the scan."
            ),
        )

    # Park the file under the project id when we have one, and under a
    # staging prefix when the project does not exist yet. Abandoned staged
    # files are not cleaned up automatically — see docs/PHASE_0.md.
    path = storage.make_mix_design_path(
        project_id or "_staging", revision, filename
    )
    try:
        storage.upload_bytes(
            storage.MIX_DESIGNS, path, pdf_bytes, content_type="application/pdf"
        )
    except Exception as e:
        log.exception("mix design upload failed")
        raise HTTPException(
            status_code=502, detail=f"Could not store the PDF: {e}"
        )

    try:
        parse, meta = parse_submittal(
            pdf_bytes, project_no=project_no, project_name=project_name
        )
    except ClaudeNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ClaudeOutputInvalid as e:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Could not read the yardage sheet: {e} "
                "The PDF is stored; enter the mix rows by hand or re-upload a "
                "clearer scan."
            ),
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        log.exception("mix design parse failed")
        raise HTTPException(
            status_code=502, detail=f"The Claude API call failed: {e}"
        )

    rows, warnings = resolve_proposals(parse)

    return MixDesignParseOut(
        storage_path=path,
        original_filename=filename,
        revision=parse.revision or revision,
        project_hint=parse.project_hint,
        supplier_hint=parse.supplier_hint,
        rows=rows,
        notes=parse.notes,
        warnings=warnings,
        model=meta.get("model"),
    )


# ─── Create ───────────────────────────────────────────────────────────


@router.post("", response_model=ProjectDetail, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectCreate,
    actor: Actor = Depends(as_actor),
    _: CurrentUser = Depends(require_role("admin", "accountant")),
):
    """Onboard a project: details plus the confirmed mix design table."""
    sb = get_service_client()

    existing = (
        sb.table("projects")
        .select("id,project_no,deleted_at")
        .eq("project_no", payload.project_no)
        .limit(1)
        .execute()
    )
    if existing.data:
        raise HTTPException(
            status_code=409,
            detail=f"Project {payload.project_no} already exists.",
        )

    project_fields = payload.model_dump(
        exclude={
            "mix_design_revision",
            "mix_design_storage_path",
            "mix_design_rows",
            "complete_onboarding",
        }
    )
    _validate_user_ids(project_fields)
    _validate_cost_code_ids(payload.mix_design_rows)

    # Check the gate against what is ABOUT to be written, before writing it,
    # so a blocked onboarding leaves no partial project behind.
    if payload.complete_onboarding:
        prospective_rows = [{"superseded_at": None} for _ in payload.mix_design_rows]
        blockers = onboarding_blockers(project_fields, prospective_rows)
        if blockers:
            raise HTTPException(
                status_code=422,
                detail="Cannot complete onboarding: " + " ".join(blockers),
            )

    project_fields["created_by"] = actor.user_id
    if payload.mix_design_storage_path:
        project_fields["mix_design_pdf_path"] = payload.mix_design_storage_path

    created = sb.table("projects").insert(project_fields).execute()
    if not created.data:
        raise HTTPException(status_code=502, detail="Project insert returned no row.")
    project = created.data[0]

    # Mix rows BEFORE the onboarding stamp, and the stamp only after they
    # land. There is no transaction across these calls, so ordering is the
    # only guarantee available: if the mix insert fails, the project exists
    # but is un-onboarded — the safe state, visible on /projects and fixable
    # from the detail screen. Stamping first would leave an onboarded project
    # with no mix design, which is exactly what the gate exists to prevent.
    if payload.mix_design_rows:
        try:
            _insert_mix_rows(
                project_id=project["id"],
                revision=payload.mix_design_revision,
                rows=payload.mix_design_rows,
                source_pdf_path=payload.mix_design_storage_path,
            )
        except Exception as e:
            log.exception("mix row insert failed for project %s", project["id"])
            audit.record(
                entity_type="project",
                entity_id=project["id"],
                action="created",
                actor=actor,
                diff={"project_no": project["project_no"], "mix_rows_failed": str(e)},
            )
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Project {project['project_no']} was created but its mix "
                    f"design rows did not save ({e}). It is not onboarded, so "
                    "no invoices will route to it. Open the project and upload "
                    "the submittal again."
                ),
            )

    if payload.complete_onboarding:
        sb.table("projects").update(
            {
                "onboarded_at": datetime.now(timezone.utc).isoformat(),
                "onboarded_by": actor.user_id,
            }
        ).eq("id", project["id"]).execute()

    audit.record(
        entity_type="project",
        entity_id=project["id"],
        action="onboarded" if payload.complete_onboarding else "created",
        actor=actor,
        diff={
            "project_no": project["project_no"],
            "mix_design_rows": len(payload.mix_design_rows),
        },
    )

    return get_project(project["id"], user=actor.user)  # type: ignore[arg-type]


def _insert_mix_rows(
    *,
    project_id: str,
    revision: str,
    rows: list,
    source_pdf_path: Optional[str],
) -> None:
    sb = get_service_client()
    sb.table("mix_designs").insert(
        [
            {
                "project_id": project_id,
                "revision": revision,
                "mix_no": r.mix_no,
                "psi": r.psi,
                "element_use": r.element_use,
                "pump_line": r.pump_line,
                "cost_code_id": r.cost_code_id,
                "source_pdf_path": source_pdf_path,
            }
            for r in rows
        ]
    ).execute()


# ─── Update ───────────────────────────────────────────────────────────


@router.patch("/{project_id}", response_model=ProjectDetail)
def update_project(
    project_id: str,
    payload: ProjectUpdate,
    actor: Actor = Depends(as_actor),
    _: CurrentUser = Depends(require_role("admin", "accountant")),
):
    """Edit project fields. Cannot stamp or clear onboarding."""
    before = _fetch_project(project_id)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="No fields to update.")

    merged = {**before, **changes}
    _validate_user_ids(merged)

    if "project_no" in changes and changes["project_no"] != before["project_no"]:
        sb = get_service_client()
        clash = (
            sb.table("projects")
            .select("id")
            .eq("project_no", changes["project_no"])
            .neq("id", project_id)
            .limit(1)
            .execute()
        )
        if clash.data:
            raise HTTPException(
                status_code=409,
                detail=f"Project {changes['project_no']} already exists.",
            )

    # Turning off the concrete-supplier flag can satisfy the gate; turning it
    # on can invalidate an already-onboarded project. Surface the second case
    # rather than leaving a project marked onboarded with no mix design.
    sb = get_service_client()
    updated = (
        sb.table("projects").update(changes).eq("id", project_id).execute()
    )
    if not updated.data:
        raise HTTPException(status_code=502, detail="Project update returned no row.")

    audit.log_edit(
        entity_type="project",
        entity_id=project_id,
        before=before,
        after=changes,
        actor=actor,
    )

    after = _fetch_project(project_id)
    mix_rows = _fetch_mix_rows(project_id)
    if after.get("onboarded_at") and onboarding_blockers(after, mix_rows):
        log.warning(
            "project %s is marked onboarded but now has blockers: %s",
            project_id,
            onboarding_blockers(after, mix_rows),
        )

    return get_project(project_id, user=actor.user)  # type: ignore[arg-type]


@router.post("/{project_id}/complete-onboarding", response_model=ProjectDetail)
def complete_onboarding(
    project_id: str,
    actor: Actor = Depends(as_actor),
    _: CurrentUser = Depends(require_role("admin", "accountant")),
):
    """Stamp onboarded_at once the §7.0 requirements are met."""
    project = _fetch_project(project_id)
    mix_rows = _fetch_mix_rows(project_id)

    blockers = onboarding_blockers(project, mix_rows)
    if blockers:
        raise HTTPException(
            status_code=422,
            detail="Cannot complete onboarding: " + " ".join(blockers),
        )

    if project.get("onboarded_at"):
        # Idempotent: re-confirming an onboarded project is not an error.
        return get_project(project_id, user=actor.user)  # type: ignore[arg-type]

    sb = get_service_client()
    sb.table("projects").update(
        {
            "onboarded_at": datetime.now(timezone.utc).isoformat(),
            "onboarded_by": actor.user_id,
        }
    ).eq("id", project_id).execute()

    audit.record(
        entity_type="project",
        entity_id=project_id,
        action="onboarded",
        actor=actor,
    )
    return get_project(project_id, user=actor.user)  # type: ignore[arg-type]


# ─── Mix design revisions ─────────────────────────────────────────────


@router.post("/{project_id}/mix-designs", response_model=ProjectDetail)
def confirm_mix_design(
    project_id: str,
    payload: MixDesignConfirm,
    actor: Actor = Depends(as_actor),
    _: CurrentUser = Depends(require_role("admin", "accountant", "pe")),
):
    """Save a confirmed submittal revision.

    Prompt §7.0: when a new revision arrives the old rows are versioned, not
    deleted, and existing invoices keep the rows they were scored against. So
    this supersedes the previous revision's live rows instead of replacing
    them, and leaves their ids intact for anything already pointing at them.
    """
    _fetch_project(project_id)

    if not payload.rows:
        raise HTTPException(
            status_code=422,
            detail="A mix design revision needs at least one row.",
        )

    mix_nos = [r.mix_no for r in payload.rows]
    duplicates = {m for m in mix_nos if mix_nos.count(m) > 1}
    if duplicates:
        raise HTTPException(
            status_code=422,
            detail=f"Duplicate mix numbers in this revision: {', '.join(sorted(duplicates))}",
        )

    _validate_cost_code_ids(payload.rows)

    sb = get_service_client()
    existing = (
        sb.table("mix_designs")
        .select("id,revision")
        .eq("project_id", project_id)
        .eq("revision", payload.revision)
        .limit(1)
        .execute()
    )
    if existing.data:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Revision {payload.revision} already exists on this project. "
                "Use a new revision label for an updated submittal."
            ),
        )

    now = datetime.now(timezone.utc).isoformat()
    superseded = (
        sb.table("mix_designs")
        .update({"superseded_at": now})
        .eq("project_id", project_id)
        .is_("superseded_at", "null")
        .execute()
    )

    _insert_mix_rows(
        project_id=project_id,
        revision=payload.revision,
        rows=payload.rows,
        source_pdf_path=payload.storage_path,
    )

    if payload.storage_path:
        sb.table("projects").update(
            {"mix_design_pdf_path": payload.storage_path}
        ).eq("id", project_id).execute()

    audit.record(
        entity_type="project",
        entity_id=project_id,
        action="mix_design_confirmed",
        actor=actor,
        diff={
            "revision": payload.revision,
            "rows": len(payload.rows),
            "superseded_rows": len(superseded.data or []),
        },
    )

    return get_project(project_id, user=actor.user)  # type: ignore[arg-type]


@router.patch("/{project_id}/mix-designs/{row_id}", response_model=MixDesignRowOut)
def update_mix_row(
    project_id: str,
    row_id: str,
    payload: MixDesignRowPatch,
    actor: Actor = Depends(as_actor),
    _: CurrentUser = Depends(require_role("admin", "accountant", "pe")),
):
    """The PE mapping one element use to a cost code (prompt §7.0).

    Editing a superseded row is refused: prompt §7.0 guarantees existing
    invoices keep the rows they were scored against, and retro-editing one
    would rewrite the basis of an approval that already happened.
    """
    sb = get_service_client()
    res = (
        sb.table("mix_designs")
        .select("*")
        .eq("id", row_id)
        .eq("project_id", project_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Mix design row not found")
    before = res.data[0]

    if before.get("superseded_at"):
        raise HTTPException(
            status_code=409,
            detail=(
                "This row belongs to a superseded revision and is kept as the "
                "basis for invoices already scored against it. Edit the "
                "current revision instead."
            ),
        )

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="No fields to update.")

    if changes.get("cost_code_id"):
        check = (
            sb.table("cost_codes")
            .select("id")
            .eq("id", changes["cost_code_id"])
            .limit(1)
            .execute()
        )
        if not check.data:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown cost_code_id: {changes['cost_code_id']}",
            )

    updated = sb.table("mix_designs").update(changes).eq("id", row_id).execute()
    if not updated.data:
        raise HTTPException(status_code=502, detail="Mix row update returned no row.")

    audit.log_edit(
        entity_type="mix_design",
        entity_id=row_id,
        before=before,
        after=changes,
        actor=actor,
    )

    row = updated.data[0]
    labels = _cost_code_labels()
    return MixDesignRowOut(
        **{k: row.get(k) for k in MixDesignRowOut.model_fields if k in row},
        cost_code=labels.get(row.get("cost_code_id") or ""),
    )
