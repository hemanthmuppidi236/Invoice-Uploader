#!/usr/bin/env python3
"""
Verify a minted Drive token and find the folder ids the app needs.

Two questions this answers, both of which otherwise fail silently:

  1. **Which Google account does this token actually belong to?** The consent
     screen is easy to complete as the wrong account — the browser was
     already signed in as someone else. Nothing downstream complains: the
     poll just never finds anything.

  2. **What are the folder ids?** They are the part of each folder's URL
     after `/folders/`, which means opening three folders and copying from
     the address bar. Searching for them by name is less error-prone, and it
     doubles as proof that this identity can actually see them.

Reads `scripts/google-credentials.env` by default — the file the token script
writes. Prints no secrets.

    python scripts/find_drive_folders.py
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# The folders the app is configured with, and the variable each one feeds.
WANTED = [
    ("Invoice Uploads", "DRIVE_FOLDER_INVOICE_UPLOADS", "intake (§7.1)"),
    ("White Cap", "DRIVE_FOLDER_WHITE_CAP", "combined-file intake (SOP §5)"),
    ("BT Invoices", "DRIVE_FOLDER_BT_INVOICES", "filing (§7.7)"),
]

SCOPES = ["https://www.googleapis.com/auth/drive"]


def load_env(path: Path) -> dict:
    if not path.exists():
        sys.exit(
            f"No credentials file at {path}.\n"
            "Run: python scripts/get_google_refresh_token.py --scopes both"
        )
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip()
    return values


def main() -> None:
    env = load_env(
        Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "google-credentials.env"
    )
    refresh = env.get("DRIVE_OAUTH_REFRESH_TOKEN") or env.get(
        "GMAIL_OAUTH_REFRESH_TOKEN"
    )
    if not refresh:
        sys.exit("No DRIVE_OAUTH_REFRESH_TOKEN in that file.")

    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=refresh,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=env["GMAIL_OAUTH_CLIENT_ID"],
        client_secret=env["GMAIL_OAUTH_CLIENT_SECRET"],
        scopes=SCOPES,
    )
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    about = service.about().get(fields="user(emailAddress,displayName)").execute()
    who = about["user"]["emailAddress"]
    print(f"\nThis token acts as: {who}")
    print("If that is not the account you meant, revoke at")
    print("https://myaccount.google.com/permissions and mint it again.\n")

    print("=" * 68)
    found = {}
    for name, var, purpose in WANTED:
        results = (
            service.files()
            .list(
                q=(
                    "mimeType = 'application/vnd.google-apps.folder' "
                    f"and name = '{name}' and trashed = false"
                ),
                fields="files(id, name, parents)",
                pageSize=10,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
            .get("files", [])
        )
        if not results:
            print(f"  # {name!r} — NOT FOUND ({purpose})")
            print(f"  # {who} cannot see a folder by that name. Share it, or")
            print(f"  # the name differs — set {var} by hand from the URL.")
        elif len(results) > 1:
            # Not a failure, but it has to be a decision rather than a guess:
            # pointing intake at the wrong 'White Cap' finds nothing and says
            # nothing.
            print(f"  # {name!r} — {len(results)} folders match. Pick one:")
            for f in results:
                print(f"  #   {f['id']}")
            print(f"  {var}=<one of the above>")
        else:
            found[var] = results[0]["id"]
            print(f"  {var}={results[0]['id']}")
    print("=" * 68)
    print(f"\n{len(found)}/{len(WANTED)} resolved. Paste these into Render.")


if __name__ == "__main__":
    main()
