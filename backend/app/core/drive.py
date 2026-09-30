"""
Google Drive client for invoice intake (prompt §7.1) and filing (§7.7).

Read and copy only, in Phase 1. Two rules from the spec are enforced here
rather than left to the caller:

  - **Never delete.** There is no delete call in this module. Processed
    originals are MOVED to `Uploaded/` (§7.1, §7.7, §12), and the move is a
    parent-reassignment, not a copy-then-delete.
  - **Never create a folder.** `find_folder` returns None rather than
    creating, so a missing project or vendor folder flags for a person
    (§7.7, §12). SOP §7 makes this a stop-and-ask.

Two ways to authenticate, resolved in `_build_credentials()`:

  - **Service account** (`GOOGLE_DRIVE_CREDENTIALS_JSON`). Preferred when the
    Google org allows it: the identity is not tied to a person, so nothing
    breaks when someone leaves. The account has to be granted access to the
    shared drive explicitly.

  - **OAuth user credentials** (`DRIVE_OAUTH_REFRESH_TOKEN`). The fallback
    when the org enforces `iam.disableServiceAccountCreation`, which is a
    common Workspace policy. Identical mechanism to Gmail sending: consent
    once as an account that already has the folders, and the cron reuses the
    refresh token. No sharing step needed — the account already sees them.

Every call passes `supportsAllDrives` and `includeItemsFromAllDrives` — the
invoices live on a shared drive, and without those flags the API silently
returns an empty list rather than an error, which looks exactly like "no new
invoices".
"""

import io
import json
import logging
from typing import Iterator, Optional

from .config import settings

log = logging.getLogger(__name__)

# Drive's own MIME type for folders.
FOLDER_MIME = "application/vnd.google-apps.folder"
PDF_MIME = "application/pdf"

# §7.1: "Ignore `Uploaded/` subfolders." Matched case-insensitively against
# the folder name so `uploaded` and `UPLOADED` are both skipped.
ARCHIVE_FOLDER_NAMES = {"uploaded"}

SCOPES = ["https://www.googleapis.com/auth/drive"]


class DriveNotConfigured(RuntimeError):
    """Raised when a Drive path is reached with no credentials set."""


_service = None


def _build_credentials():
    """Resolve Drive credentials from whichever mode is configured.

    Service account first when both are present — it is the more robust
    identity, and a deployment that has deliberately set one up should not
    silently fall through to a person's token.
    """
    mode = settings.drive_auth_mode

    if mode == "service_account":
        from google.oauth2 import service_account

        try:
            info = json.loads(settings.google_drive_credentials_json or "")
        except json.JSONDecodeError as e:
            raise DriveNotConfigured(
                "GOOGLE_DRIVE_CREDENTIALS_JSON is not valid JSON. Paste the "
                f"whole service account key file as one line. ({e})"
            )
        return service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES
        )

    if mode == "oauth_user":
        from google.oauth2.credentials import Credentials

        return Credentials(
            token=None,
            refresh_token=settings.drive_oauth_refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.drive_oauth_client_id,
            client_secret=settings.drive_oauth_client_secret,
            scopes=SCOPES,
        )

    # Neither mode is complete. Say which half is missing rather than just
    # "not configured" — a half-set credential pair is the common case.
    raise DriveNotConfigured(
        "Drive is not configured. Set either GOOGLE_DRIVE_CREDENTIALS_JSON "
        "(a service account key), or DRIVE_OAUTH_REFRESH_TOKEN together with "
        "a client id and secret — GOOGLE_OAUTH_CLIENT_ID/SECRET, or the "
        "GMAIL_OAUTH_ ones, which are reused when the same Desktop OAuth "
        "client carries both scopes. See docs/SETUP.md §4."
    )


def get_service():
    """Build (and cache) the Drive v3 service."""
    global _service
    if _service is None:
        from googleapiclient.discovery import build

        creds = _build_credentials()
        log.info("Drive authenticated via %s", settings.drive_auth_mode)
        # cache_discovery=False: the on-disk cache breaks on Render's
        # read-only container filesystem.
        _service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _service


def reset_service() -> None:
    """Drop the cached client. Used by tests, and after a credential change."""
    global _service
    _service = None


def _escape(value: str) -> str:
    """Escape a value for a Drive query string literal.

    Drive queries are single-quoted strings with backslash escaping. A vendor
    folder named "Bob's Rebar" would otherwise terminate the literal early and
    produce a confusing 400.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


# ─── Listing ──────────────────────────────────────────────────────────


def list_pdfs(folder_id: str, *, recurse_into_subfolders: bool = False) -> list[dict]:
    """PDFs directly in `folder_id`, oldest first.

    `recurse_into_subfolders` walks one level down, which is what the
    White Cap folder needs — SOP §2 puts split invoices in
    `White Cap/[Job Name]/`. `Uploaded/` subtrees are always skipped.
    """
    service = get_service()
    files: list[dict] = []

    for entry in _iter_children(service, folder_id):
        name = entry.get("name", "")
        if entry.get("mimeType") == FOLDER_MIME:
            if name.strip().lower() in ARCHIVE_FOLDER_NAMES:
                log.debug("skipping archive folder %r", name)
                continue
            if recurse_into_subfolders:
                for child in _iter_children(service, entry["id"]):
                    if child.get("mimeType") == PDF_MIME:
                        child["parent_folder_name"] = name
                        files.append(child)
            continue
        if entry.get("mimeType") == PDF_MIME:
            files.append(entry)

    # Oldest first. Intake caps how many new invoices it takes per run, so
    # on a backlog the order decides who waits: the oldest invoice is the
    # most overdue vendor, and it should not be the one left behind.
    files.sort(key=lambda f: f.get("modifiedTime") or "")
    return files


def _iter_children(service, folder_id: str) -> Iterator[dict]:
    """Paginate over one folder's immediate children."""
    query = f"'{_escape(folder_id)}' in parents and trashed = false"
    page_token: Optional[str] = None
    while True:
        response = (
            service.files()
            .list(
                q=query,
                fields=(
                    "nextPageToken, files(id, name, mimeType, size, "
                    "modifiedTime, createdTime, parents, webViewLink)"
                ),
                pageSize=200,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        for entry in response.get("files", []):
            yield entry
        page_token = response.get("nextPageToken")
        if not page_token:
            break


def find_folder(parent_id: str, name: str) -> Optional[dict]:
    """A subfolder by name, or None.

    Deliberately never creates. Prompt §12 and SOP §7 both make a missing
    Drive folder a stop-and-ask: creating one silently files invoices
    somewhere nobody is looking.
    """
    service = get_service()
    query = (
        f"'{_escape(parent_id)}' in parents and trashed = false "
        f"and mimeType = '{FOLDER_MIME}' and name = '{_escape(name)}'"
    )
    response = (
        service.files()
        .list(
            q=query,
            fields="files(id, name)",
            pageSize=2,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    found = response.get("files", [])
    if not found:
        return None
    if len(found) > 1:
        log.warning(
            "more than one folder named %r under %s; using the first",
            name,
            parent_id,
        )
    return found[0]


def get_metadata(file_id: str) -> dict:
    service = get_service()
    return (
        service.files()
        .get(
            fileId=file_id,
            fields="id, name, mimeType, size, modifiedTime, parents, webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )


# ─── Download ─────────────────────────────────────────────────────────


def download(file_id: str) -> bytes:
    """Download a file's bytes."""
    from googleapiclient.http import MediaIoBaseDownload

    service = get_service()
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request, chunksize=5 * 1024 * 1024)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buffer.getvalue()


# ─── Write operations (Phase 3 filing) ────────────────────────────────


def upload_copy(
    *, folder_id: str, filename: str, data: bytes, mime_type: str = PDF_MIME
) -> dict:
    """Put a renamed copy of a PDF into an existing folder (§7.7).

    Filenames here routinely contain a dollar sign ("09-14-26 $2,600.00.pdf").
    That is fine through the API — the SOP §9 escaping problem was a shell
    quoting bug, not a Drive one — but it is exactly why filing goes through
    this function instead of a terminal command.
    """
    from googleapiclient.http import MediaIoBaseUpload

    service = get_service()
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=False)
    return (
        service.files()
        .create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media,
            fields="id, name, parents, webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )


def move(file_id: str, *, to_folder_id: str) -> dict:
    """Move a file by reassigning its parent. Never a copy-then-delete.

    Prompt §12: never delete a Drive file, move only. Reassigning parents
    keeps the same file id, so the invoice row's `source_file_id` stays valid
    and re-polling still recognises the file as already ingested.
    """
    service = get_service()
    current = (
        service.files()
        .get(fileId=file_id, fields="id, name, parents", supportsAllDrives=True)
        .execute()
    )
    parents = current.get("parents", [])

    # Already there. This is not a hypothetical: a combined White Cap PDF is
    # one Drive file behind several invoice rows (§7.1 keys on
    # (source_file_id, source_page)), so the second page's filing asks to
    # archive a file the first page already archived. Without this the call
    # would try to remove the destination from its own parents.
    if parents == [to_folder_id]:
        log.debug("file %s is already in %s; nothing to move", file_id, to_folder_id)
        return current

    previous = ",".join(p for p in parents if p != to_folder_id)
    return (
        service.files()
        .update(
            fileId=file_id,
            addParents=to_folder_id,
            removeParents=previous or None,
            fields="id, name, parents",
            supportsAllDrives=True,
        )
        .execute()
    )


def list_filenames(folder_id: str) -> list[str]:
    """Existing filenames in a folder.

    SOP §7 says to name a filed copy to match what is already in the vendor
    folder, because the formats vary per folder. Filing reads the folder first
    rather than imposing one convention.
    """
    service = get_service()
    return [entry.get("name", "") for entry in _iter_children(service, folder_id)]
