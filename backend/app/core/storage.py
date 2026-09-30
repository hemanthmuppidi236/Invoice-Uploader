"""
Supabase Storage helpers.

Two buckets:
  - invoices     — the PDF pulled off Drive for each invoice
  - mix-designs  — the mix design submittal PDF for each project

Path conventions:
  invoices/{YYYY}/{MM}/{invoice_id}.pdf
  mix-designs/{project_id}/{revision}/{filename}.pdf

Both buckets must be created manually in the Supabase dashboard and left
PRIVATE — the frontend reads PDFs through short-lived signed URLs, never a
public URL.
"""

import re
from datetime import datetime, timezone
from pathlib import Path

from .config import settings
from .supabase_client import get_service_client


def upload_bytes(
    bucket: str,
    path: str,
    data: bytes,
    content_type: str = "application/pdf",
    upsert: bool = True,
) -> str:
    """Upload bytes to a bucket. Returns the storage path."""
    sb = get_service_client()
    file_options = {
        "content-type": content_type,
        "upsert": "true" if upsert else "false",
    }
    sb.storage.from_(bucket).upload(path, data, file_options=file_options)
    return path


def signed_url(bucket: str, path: str, expires_in: int = 3600) -> str:
    """Signed download URL, valid for `expires_in` seconds (default 1 hour)."""
    sb = get_service_client()
    res = sb.storage.from_(bucket).create_signed_url(path, expires_in)
    return res.get("signedURL") or res.get("signed_url") or ""


def download_bytes(bucket: str, path: str) -> bytes:
    """Download a stored object. Used to feed the PDF to the Claude API and
    to attach it to emails."""
    sb = get_service_client()
    return sb.storage.from_(bucket).download(path)


def delete_object(bucket: str, path: str) -> None:
    sb = get_service_client()
    sb.storage.from_(bucket).remove([path])


# ─── Path builders ────────────────────────────────────────────────────


def make_invoice_pdf_path(invoice_id: str, when: datetime | None = None) -> str:
    """Year/month partitioned so the bucket stays browsable as volume grows."""
    ts = when or datetime.now(timezone.utc)
    return f"{ts.year:04d}/{ts.month:02d}/{invoice_id}.pdf"


def make_mix_design_path(project_id: str, revision: str, original_filename: str) -> str:
    stem = safe_segment(Path(original_filename).stem)
    rev = safe_segment(revision)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{project_id}/{rev}/{ts}_{stem}.pdf"


def safe_segment(s: str) -> str:
    """Sanitize a string for use as a storage path segment.

    Storage keys reject a lot of what shows up in vendor filenames, and dollar
    signs in particular have already cost a batch of invoices once (SOP §9).
    Strip to a known-safe alphabet rather than trying to escape.
    """
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", s).strip("_")
    return (cleaned or "file")[:80]


# Convenience aliases so callers do not import settings just for a bucket name.
INVOICES = settings.bucket_invoices
MIX_DESIGNS = settings.bucket_mix_designs
