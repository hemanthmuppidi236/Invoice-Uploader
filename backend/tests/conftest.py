"""
Test configuration.

Two jobs, and the second one is the important one.

**Satisfy the fail-fast loader.** `Settings` has required fields with no
defaults, so the Supabase values must exist before `app.core.config` is
imported by anything under test.

**Isolate the suite from the developer's own `.env`.** `Settings` is
configured with `env_file=".env"`, which means a configured machine feeds its
real Supabase keys, Anthropic key, and Gmail refresh token into every test
that constructs a Settings. That is not hypothetical: with a populated `.env`,
`settings.email_enabled` becomes True and the email tests attempt a real Gmail
call. A suite that passes on a clean checkout and reaches the network on a
working machine is worse than useless — it fails in exactly the situation
where you most need to trust it.

Environment variables take priority over the dotenv file in pydantic-settings,
so setting them here overrides whatever is in `.env` without having to move or
rename the file.
"""

import os

# ─── Required by the fail-fast loader ─────────────────────────────────
os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["SUPABASE_ANON_KEY"] = "test-anon-key"
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = "test-service-key"
os.environ["APP_ENV"] = "test"

# ─── Every credential blanked, so nothing can reach a real service ────
#
# Blank rather than absent: an empty string still overrides the dotenv value,
# and is falsy everywhere the code checks. A test that wants an integration
# switched on turns it on explicitly with monkeypatch, which also documents
# at the test site that it is doing so.
for _credential in (
    "ANTHROPIC_API_KEY",
    "GOOGLE_DRIVE_CREDENTIALS_JSON",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "DRIVE_OAUTH_REFRESH_TOKEN",
    "DRIVE_FOLDER_INVOICE_UPLOADS",
    "DRIVE_FOLDER_WHITE_CAP",
    "DRIVE_FOLDER_BT_INVOICES",
    "GMAIL_OAUTH_CLIENT_ID",
    "GMAIL_OAUTH_CLIENT_SECRET",
    "GMAIL_OAUTH_REFRESH_TOKEN",
    "GMAIL_SENDER_EMAIL",
    "ADMIN_ALERT_EMAIL",
    "AGENT_API_KEY",
):
    os.environ[_credential] = ""

# log_only, so a stray send path logs instead of dialling Gmail.
os.environ["EMAIL_PROVIDER"] = "log_only"
os.environ["JOBS_PAUSED"] = "false"


def pytest_report_header(config):
    """Say out loud that the suite is hermetic, so a green run on a machine
    with live credentials is not mistaken for a run against them."""
    return "ferrocrete: credentials blanked, no integration reachable"
