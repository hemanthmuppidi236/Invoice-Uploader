#!/usr/bin/env python3
"""
One-time helper: trade OAuth consent for a refresh token.

The app needs unattended Google access for two things, and this script mints
the token for either or both:

    drive   Reading invoices out of Drive and filing copies back (Phase 1/3).
            Needed when your Google org blocks service account creation —
            `iam.disableServiceAccountCreation` is a common Workspace policy.
    gmail   Sending the two daily digests (Phase 2).

Usage, on your own machine and NOT on Render:

  1. Google Cloud Console → APIs & Services → Credentials →
     Create Credentials → OAuth client ID → **Desktop app**.
     Download the JSON and save it beside this script as `client_secret.json`.
     (Gitignored. Delete it once the tokens are in Render.)

  2. pip install google-auth-oauthlib google-auth google-api-python-client

  3. python scripts/get_google_refresh_token.py --scopes drive
     python scripts/get_google_refresh_token.py --scopes gmail
     python scripts/get_google_refresh_token.py --scopes both

  4. A browser opens. Sign in as the account the app should ACT AS, which is
     not necessarily you:
       - for drive: an account that can already see the Invoice Uploads,
         White Cap, and BT Invoices folders
       - for gmail: the mailbox the digests should come from
     Click Allow.

  5. Paste the printed values into Render.

Two things that will bite you otherwise:

  * **Set the OAuth consent screen's user type to Internal.** On an External
    screen still in "Testing", Google expires refresh tokens after 7 days —
    the app stops working a week later with no other symptom. Internal also
    exempts you from app verification, which the Drive scope would otherwise
    require because it is a restricted scope.

  * **Prefer a shared ops account over a personal one.** A refresh token
    belongs to whoever consented. If that is a person and they leave, or
    revoke access in their Google account settings, intake stops.
"""

import argparse
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


# Full `drive` rather than `drive.file`: the app reads invoices it did not
# create, which `drive.file` cannot see. `drive.readonly` would cover intake
# but not the Phase 3 filing step.
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

SCOPE_SETS = {
    "drive": DRIVE_SCOPES,
    "gmail": GMAIL_SCOPES,
    "both": DRIVE_SCOPES + GMAIL_SCOPES,
}

# The client id and secret live under GMAIL_OAUTH_* because that is the pair
# the app treats as canonical: Drive falls back to it when GOOGLE_OAUTH_* is
# unset, but Gmail has no fallback the other way. Printing GOOGLE_OAUTH_* for
# a gmail-only run would leave email_enabled False with nothing to explain it.
CLIENT_ID_VAR = "GMAIL_OAUTH_CLIENT_ID"
CLIENT_SECRET_VAR = "GMAIL_OAUTH_CLIENT_SECRET"

REFRESH_TOKEN_VARS = {
    "drive": "DRIVE_OAUTH_REFRESH_TOKEN",
    "gmail": "GMAIL_OAUTH_REFRESH_TOKEN",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mint a Google OAuth refresh token for this app."
    )
    parser.add_argument(
        "--scopes",
        choices=sorted(SCOPE_SETS),
        default="both",
        help=(
            "Which access to request. 'both' mints ONE token usable for "
            "Drive and Gmail — only right when the same account should do "
            "both. Use separate runs when the Drive account and the sending "
            "mailbox differ."
        ),
    )
    args = parser.parse_args()

    here = Path(__file__).resolve().parent
    client_secret_path = here / "client_secret.json"
    if not client_secret_path.exists():
        print(
            f"Expected client_secret.json at {client_secret_path}.\n"
            "Download it from Google Cloud Console → APIs & Services →\n"
            "Credentials → your Desktop OAuth client → Download JSON.",
            file=sys.stderr,
        )
        sys.exit(1)

    scopes = SCOPE_SETS[args.scopes]
    print(f"Requesting: {', '.join(scopes)}\n")

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), scopes)
    # access_type=offline plus prompt=consent forces a refresh token even when
    # this Google account has consented to this client before. Without both,
    # a second run returns an access token only and the script looks broken.
    creds = flow.run_local_server(
        port=0, access_type="offline", prompt="consent"
    )

    if not creds.refresh_token:
        print(
            "\nGoogle returned no refresh token.\n"
            "Revoke this app at https://myaccount.google.com/permissions and "
            "run again.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("\n" + "=" * 68)
    print("Paste these into Render (Environment):")
    print("=" * 68)
    print(f"{CLIENT_ID_VAR}={creds.client_id}")
    print(f"{CLIENT_SECRET_VAR}={creds.client_secret}")

    if args.scopes == "both":
        print(f"GMAIL_OAUTH_REFRESH_TOKEN={creds.refresh_token}")
        print(f"DRIVE_OAUTH_REFRESH_TOKEN={creds.refresh_token}")
        print(
            "\n(Same token twice — one consent covered both scopes. They stay "
            "separate variables so you can later point Drive and Gmail at "
            "different accounts without re-minting both.)"
        )
    else:
        print(f"{REFRESH_TOKEN_VARS[args.scopes]}={creds.refresh_token}")

    if args.scopes in ("gmail", "both"):
        print(
            "\nAlso set GMAIL_SENDER_EMAIL to the address you just signed in "
            "as — Gmail rewrites the From header to it regardless."
        )
        print("And EMAIL_PROVIDER=gmail_api.")

    if args.scopes == "drive":
        print(
            "\nThe client id and secret go under GMAIL_OAUTH_* even for a "
            "Drive-only run: Drive falls back to that pair, and naming them "
            "once avoids two copies to keep in sync. Use GOOGLE_OAUTH_CLIENT_ID "
            "and GOOGLE_OAUTH_CLIENT_SECRET only if Drive needs a DIFFERENT "
            "OAuth client from the mail sender."
        )

    print("=" * 68)
    print(
        "\nThe client id and secret are shared by both integrations, so you "
        "only need them once.\nDelete scripts/client_secret.json when you are "
        "done."
    )


if __name__ == "__main__":
    main()
