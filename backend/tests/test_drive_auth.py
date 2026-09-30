"""
Drive credential resolution.

Two supported modes, and the common failure is a half-set pair — a refresh
token with no client secret, or a client id with no token. Those have to
produce a message naming the missing half, not a generic "not configured",
because the symptom otherwise is an empty invoice list that looks exactly
like "no new invoices".
"""

import json

import pytest

from app.core import drive
from app.core.config import Settings


def settings_with(**over):
    """A Settings instance with the required fields filled and the rest
    overridden. Built fresh rather than mutated, so property logic is
    exercised the way it runs in production.

    `_env_file=None` disables the dotenv read. Without it, a developer's
    populated `.env` supplies the very fields these tests assert are absent,
    and the half-set-credential cases silently pass for the wrong reason.
    """
    base = {
        "supabase_url": "https://t.supabase.co",
        "supabase_anon_key": "anon",
        "supabase_service_role_key": "service",
    }
    base.update(over)
    return Settings(_env_file=None, **base)


SERVICE_ACCOUNT_JSON = json.dumps(
    {"type": "service_account", "project_id": "p", "client_email": "x@y.iam"}
)


# ─── Which mode is selected ───────────────────────────────────────────


def test_no_credentials_means_no_mode():
    assert settings_with().drive_auth_mode is None
    assert settings_with().drive_enabled is False


def test_a_service_account_key_selects_service_account_mode():
    s = settings_with(
        google_drive_credentials_json=SERVICE_ACCOUNT_JSON,
        drive_folder_invoice_uploads="folder-1",
    )
    assert s.drive_auth_mode == "service_account"
    assert s.drive_enabled is True


def test_oauth_user_mode_when_the_org_blocks_service_accounts():
    s = settings_with(
        google_oauth_client_id="cid",
        google_oauth_client_secret="csecret",
        drive_oauth_refresh_token="rtoken",
        drive_folder_invoice_uploads="folder-1",
    )
    assert s.drive_auth_mode == "oauth_user"
    assert s.drive_enabled is True


def test_the_gmail_client_is_reused_when_no_drive_specific_one_is_set():
    """One Desktop OAuth client can carry both scopes, so requiring the id and
    secret twice would be a pointless second copy to keep in sync."""
    s = settings_with(
        gmail_oauth_client_id="shared-id",
        gmail_oauth_client_secret="shared-secret",
        drive_oauth_refresh_token="drive-token",
        drive_folder_invoice_uploads="folder-1",
    )
    assert s.drive_oauth_client_id == "shared-id"
    assert s.drive_oauth_client_secret == "shared-secret"
    assert s.drive_auth_mode == "oauth_user"


def test_a_drive_specific_client_wins_over_the_gmail_one():
    """So Drive and Gmail can be different accounts if they need to be."""
    s = settings_with(
        gmail_oauth_client_id="gmail-id",
        google_oauth_client_id="drive-id",
        google_oauth_client_secret="drive-secret",
        drive_oauth_refresh_token="t",
    )
    assert s.drive_oauth_client_id == "drive-id"


def test_a_service_account_wins_when_both_are_configured():
    """A deployment that deliberately set one up should not silently fall
    through to a person's token."""
    s = settings_with(
        google_drive_credentials_json=SERVICE_ACCOUNT_JSON,
        google_oauth_client_id="cid",
        google_oauth_client_secret="csecret",
        drive_oauth_refresh_token="rtoken",
    )
    assert s.drive_auth_mode == "service_account"


@pytest.mark.parametrize(
    "missing",
    ["google_oauth_client_id", "google_oauth_client_secret",
     "drive_oauth_refresh_token"],
)
def test_a_half_set_oauth_pair_is_not_treated_as_configured(missing):
    """Half a credential is worse than none: it looks configured and fails
    at the first call."""
    fields = {
        "google_oauth_client_id": "cid",
        "google_oauth_client_secret": "csecret",
        "drive_oauth_refresh_token": "rtoken",
    }
    fields.pop(missing)
    assert settings_with(**fields).drive_auth_mode is None


def test_credentials_without_a_folder_id_is_still_not_enabled():
    """Authentication with nowhere to look is not a working configuration."""
    s = settings_with(google_drive_credentials_json=SERVICE_ACCOUNT_JSON)
    assert s.drive_auth_mode == "service_account"
    assert s.drive_enabled is False


# ─── Building the credential object ───────────────────────────────────


@pytest.fixture(autouse=True)
def clear_cached_client():
    drive.reset_service()
    yield
    drive.reset_service()


def test_building_with_nothing_configured_names_both_options(monkeypatch):
    monkeypatch.setattr(drive, "settings", settings_with())
    with pytest.raises(drive.DriveNotConfigured) as e:
        drive._build_credentials()
    message = str(e.value)
    assert "GOOGLE_DRIVE_CREDENTIALS_JSON" in message
    assert "DRIVE_OAUTH_REFRESH_TOKEN" in message
    assert "docs/SETUP.md" in message


def test_malformed_service_account_json_says_so(monkeypatch):
    """Pasting a multi-line key file into an env var is the usual cause, and
    the raw JSONDecodeError does not suggest that."""
    monkeypatch.setattr(
        drive, "settings", settings_with(google_drive_credentials_json="{not json")
    )
    with pytest.raises(drive.DriveNotConfigured) as e:
        drive._build_credentials()
    assert "not valid JSON" in str(e.value)
    assert "one line" in str(e.value)


def test_oauth_user_credentials_carry_the_drive_scope(monkeypatch):
    monkeypatch.setattr(
        drive,
        "settings",
        settings_with(
            google_oauth_client_id="cid",
            google_oauth_client_secret="csecret",
            drive_oauth_refresh_token="rtoken",
        ),
    )
    creds = drive._build_credentials()
    assert creds.refresh_token == "rtoken"
    assert creds.client_id == "cid"
    # drive.file cannot see files the app did not create, and readonly cannot
    # file a copy back in Phase 3.
    assert "https://www.googleapis.com/auth/drive" in creds.scopes


def test_service_account_credentials_are_built_from_the_key(monkeypatch):
    # google-auth parses the PEM eagerly, so this needs a real key. Generating
    # one needs `cryptography`, which is not a runtime dependency — skip
    # rather than add a package for one test. The OAuth branch above is the
    # new code path and is covered unconditionally.
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    monkeypatch.setattr(
        drive,
        "settings",
        settings_with(
            google_drive_credentials_json=json.dumps(
                {
                    "type": "service_account",
                    "project_id": "p",
                    "private_key_id": "kid",
                    "private_key": pem,
                    "client_email": "svc@p.iam.gserviceaccount.com",
                    "client_id": "1",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            )
        ),
    )
    creds = drive._build_credentials()
    assert creds.service_account_email == "svc@p.iam.gserviceaccount.com"
    assert "https://www.googleapis.com/auth/drive" in creds.scopes


# ─── The setup script must name the variables Settings actually reads ──


def test_the_token_script_prints_variable_names_the_app_reads():
    """The script's output is copy-pasted straight into Render, so a name it
    prints that Settings does not read leaves the integration silently off.

    This shipped wrong once: a gmail-only run printed GOOGLE_OAUTH_CLIENT_ID,
    but `email_enabled` checks `gmail_oauth_client_id`, so following the
    output literally left email disabled with nothing to explain it.
    """
    from pathlib import Path
    import re

    script = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "get_google_refresh_token.py"
    ).read_text()

    printed = set(re.findall(r'"([A-Z][A-Z0-9_]{4,})"', script))
    printed |= set(re.findall(r'f"\{?([A-Z][A-Z0-9_]{4,})\}?=', script))
    printed |= set(re.findall(r"\b([A-Z][A-Z0-9_]{4,})=\{creds", script))

    known = {f.upper() for f in Settings.model_fields}
    # Names the script mentions as prose rather than as a variable to set.
    prose = {"DRIVE_SCOPES", "GMAIL_SCOPES", "SCOPE_SETS", "CLIENT_ID_VAR",
             "CLIENT_SECRET_VAR", "REFRESH_TOKEN_VARS"}

    unknown = {p for p in printed if p not in known and p not in prose}
    assert not unknown, (
        f"get_google_refresh_token.py names {sorted(unknown)}, which "
        f"Settings does not read. Anything pasted under those names is "
        f"ignored, and the integration stays off with no error."
    )


def test_gmail_client_vars_have_no_fallback_so_the_script_must_print_them():
    """Drive falls back to the Gmail client pair, but not the reverse. That
    asymmetry is why the script prints GMAIL_OAUTH_* for every scope."""
    # email_provider is passed explicitly because conftest forces log_only
    # suite-wide, which would make both halves of this comparison False and
    # the test pass for the wrong reason.
    only_google = settings_with(
        email_provider="gmail_api",
        google_oauth_client_id="cid",
        google_oauth_client_secret="csecret",
        gmail_oauth_refresh_token="rtoken",
        gmail_sender_email="a@b.com",
    )
    assert only_google.email_enabled is False, (
        "GOOGLE_OAUTH_* must not satisfy Gmail — if it ever does, the script "
        "can print either name and this asymmetry note is stale."
    )

    proper = settings_with(
        email_provider="gmail_api",
        gmail_oauth_client_id="cid",
        gmail_oauth_client_secret="csecret",
        gmail_oauth_refresh_token="rtoken",
        gmail_sender_email="a@b.com",
    )
    assert proper.email_enabled is True
