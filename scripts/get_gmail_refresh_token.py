#!/usr/bin/env python3
"""
One-time helper: trade OAuth consent for a refresh token.

Usage (on your local machine, NOT on Render):

  1. In Google Cloud Console, create an OAuth Client ID of type
     "Desktop application" → download the JSON file → save it next to this
     script as `client_secret.json`.

  2. pip install google-auth-oauthlib google-auth google-api-python-client

  3. python scripts/get_gmail_refresh_token.py

  4. A browser window opens. Sign in as the Workspace inbox you want the app
     to send mail as (e.g. noreply@ferrocretebuilders.com). Click "Allow".

  5. The script prints three values. Paste them into Render env vars:
        GMAIL_OAUTH_CLIENT_ID
        GMAIL_OAUTH_CLIENT_SECRET
        GMAIL_OAUTH_REFRESH_TOKEN
     Also set GMAIL_SENDER_EMAIL to that account's address.

The refresh token does not expire, PROVIDED the OAuth consent screen's user
type is "Internal". On an "External" consent screen still in Testing status,
Google expires refresh tokens after 7 days — the app then stops sending mail
a week after setup with no other symptom. See docs/SETUP.md.
"""

import json
import sys
from pathlib import Path

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print(
        "Missing dependency. Run:\n"
        "    pip install google-auth-oauthlib google-auth google-api-python-client",
        file=sys.stderr,
    )
    sys.exit(1)


SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def main() -> None:
    here = Path(__file__).resolve().parent
    client_secret_path = here / "client_secret.json"
    if not client_secret_path.exists():
        print(
            f"Expected client_secret.json at {client_secret_path}.\n"
            "Download it from Google Cloud Console → APIs & Services →\n"
            "Credentials → your OAuth 2.0 Client ID → Download JSON.",
            file=sys.stderr,
        )
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)
    # access_type=offline + prompt=consent forces issuance of a refresh token
    # even if the same Google account previously consented to this OAuth client.
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
    )

    if not creds.refresh_token:
        print(
            "Google did not return a refresh token. This usually means you've\n"
            "previously granted this client consent. Revoke it at\n"
            "https://myaccount.google.com/permissions and re-run this script.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Pull client id/secret out of the same JSON for convenient copy-paste
    with open(client_secret_path) as f:
        client_data = json.load(f)
    client = client_data.get("installed") or client_data.get("web") or {}

    print("\n" + "=" * 70)
    print("✓ Success. Paste these into Render → Environment:")
    print("=" * 70)
    print(f"\nGMAIL_OAUTH_CLIENT_ID={client.get('client_id', '???')}")
    print(f"GMAIL_OAUTH_CLIENT_SECRET={client.get('client_secret', '???')}")
    print(f"GMAIL_OAUTH_REFRESH_TOKEN={creds.refresh_token}")
    print(f"\n# Also set (use the Workspace inbox you just signed in as):")
    print(f"GMAIL_SENDER_EMAIL=accounting@ferrocretebuilders.com   # ← edit me")
    print(f"EMAIL_FROM=accounting@ferrocretebuilders.com           # ← same as GMAIL_SENDER_EMAIL")
    print(f"EMAIL_PROVIDER=gmail_api")
    print()


if __name__ == "__main__":
    main()
