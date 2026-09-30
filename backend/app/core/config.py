"""
Application settings, loaded from environment variables (.env in dev, Render
env vars in prod).

Typed and fail-fast (prompt §12): any field without a default must be present
at import time or the process refuses to start. A missing SUPABASE_URL should
crash on boot, not on the first request.
"""

import re
from typing import Optional

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Every Unicode whitespace and separator, not just the ASCII ones. U+2028
# and U+FEFF are the two that survive a copy-paste unnoticed.
_INVISIBLE = re.compile(
    r"[\s\u00a0\u1680\u2000-\u200b\u2028\u2029\u202f\u205f\u3000\ufeff]"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ─── App ──────────────────────────────────────────────────────────
    app_env: str = "development"           # development | production
    app_name: str = "Ferrocrete Invoice Processor API"
    log_level: str = "INFO"

    # ─── Supabase ─────────────────────────────────────────────────────
    supabase_url: str
    supabase_anon_key: str                 # verifying user JWTs
    supabase_service_role_key: str         # backend writes (bypasses RLS)

    @field_validator(
        "supabase_url",
        "supabase_anon_key",
        "supabase_service_role_key",
        mode="before",
    )
    @classmethod
    def _strip_invisibles(cls, v):
        """Drop whitespace a dashboard copy-paste smuggled in.

        A JWT copied out of the Supabase dashboard arrives often enough with
        a trailing newline, a non-breaking space, or a U+2028 LINE SEPARATOR —
        none of them visible in a Render or Vercel settings field. Nothing
        breaks until the value goes into an HTTP header, and then the runtime
        refuses it with a message that names a byte offset and no variable:

            Cannot convert argument to a ByteString because the character at
            index 215 has a value of 8232 which is greater than 255.

        Neither a URL nor a JWT ever legitimately contains whitespace, so
        removing all of it is safe, and it is the difference between a
        configuration that works and an hour spent on an OAuth callback.
        """
        if not isinstance(v, str):
            return v
        return _INVISIBLE.sub("", v)

    # ─── Storage buckets (create these in Supabase Storage) ───────────
    bucket_invoices: str = "invoices"
    bucket_mix_designs: str = "mix-designs"

    # ─── CORS ─────────────────────────────────────────────────────────
    cors_origins: str = "http://localhost:3000"   # comma-separated

    # ─── Claude API (invoice extraction + mix design parsing) ─────────
    anthropic_api_key: Optional[str] = None
    # Opus 5 for extraction: the cost-code call is a judgement task over a
    # scanned PDF, and a wrong code is a wrong bill in BuilderTrend.
    claude_model: str = "claude-opus-5"
    claude_effort: str = "high"            # low | medium | high | xhigh | max
    claude_max_tokens: int = 16000

    # ─── Google Drive (intake + filing, Phase 1/3) ────────────────────
    # Two supported ways to authenticate, checked in this order:
    #
    #   1. A service account JSON key. Simplest when your Google org allows
    #      service accounts, and the identity is not tied to a person.
    #   2. OAuth user credentials with a refresh token. The fallback when the
    #      org policy `iam.disableServiceAccountCreation` is in force — the
    #      same mechanism Gmail sending already uses. Consent once as an
    #      account that can see the folders; the cron reuses the token.
    #
    # Prefer a shared ops mailbox over a real person's account for option 2:
    # a refresh token is tied to whoever consented, and intake stops the day
    # they leave or revoke it.
    google_drive_credentials_json: Optional[str] = None   # service account JSON

    # Option 2. The client id and secret default to the Gmail ones, because
    # a single Desktop OAuth client can carry both scopes.
    google_oauth_client_id: Optional[str] = None
    google_oauth_client_secret: Optional[str] = None
    drive_oauth_refresh_token: Optional[str] = None

    drive_folder_invoice_uploads: Optional[str] = None    # folder id
    drive_folder_white_cap: Optional[str] = None          # folder id
    drive_folder_bt_invoices: Optional[str] = None        # folder id

    # ─── Gmail API (outbound email, same setup as the pay app) ────────
    email_from: str = "noreply@ferrocretebuilders.com"
    email_provider: str = "gmail_api"      # gmail_api | log_only
    gmail_oauth_client_id: Optional[str] = None
    gmail_oauth_client_secret: Optional[str] = None
    gmail_oauth_refresh_token: Optional[str] = None
    gmail_sender_email: Optional[str] = None

    # Admin alert recipient. Prompt §7.0 and §9 both require the app to email
    # the admin on onboarding gaps and email-job failures.
    admin_alert_email: Optional[str] = None

    # ─── Frontend URL, for deep links in emails ───────────────────────
    app_url: str = "http://localhost:3000"

    # ─── BuilderTrend ─────────────────────────────────────────────────
    # SOP §8.5: "Job context drifts. Open new bills via
    # buildertrend.net/app/Bills/Bill/0/{jobId}". The upload queue builds that
    # URL per invoice so the Chrome session never navigates by clicking
    # through jobs. Configurable only so a tenant-specific host does not need
    # a deploy.
    buildertrend_base_url: str = "https://buildertrend.net"

    # ─── Auth ─────────────────────────────────────────────────────────
    allowed_email_domain: Optional[str] = "ferrocretebuilders.com"

    # Shared secret for the Claude-in-Chrome upload session (prompt §11).
    # Scoped to list-approved / mark-uploaded / mark-filed / flag — never to
    # the endpoints that move an invoice into `approved`.
    agent_api_key: Optional[str] = None

    # ─── Kill switch (prompt §12) ─────────────────────────────────────
    # Pauses Drive polling and both scheduled email jobs without a deploy.
    jobs_paused: bool = False

    # ─── Business rules that were defaults, not discoveries ───────────
    # Prompt §14: poll every 15 min, emails at 3:30 PM and 5:30 PM Pacific,
    # weekdays only. Kept in config so a schedule change is not a code change.
    poll_interval_minutes: int = 15
    email_timezone: str = "America/Los_Angeles"
    weekdays_only: bool = True

    # Confidence thresholds (prompt §8). Above high = green, between = gold,
    # below low = red and the reviewer must pick.
    confidence_high: float = 0.85
    confidence_low: float = 0.60

    # §8 phase inference: how far back to look at approved invoices on the
    # same project when disambiguating a multi-element mix.
    phase_lookback_days: int = 30

    @property
    def claude_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def email_enabled(self) -> bool:
        """True when emails should actually send (vs. log only)."""
        if self.email_provider != "gmail_api":
            return False
        return bool(
            self.gmail_oauth_client_id
            and self.gmail_oauth_client_secret
            and self.gmail_oauth_refresh_token
            and self.gmail_sender_email
        )

    # ─── Drive credential resolution ──────────────────────────────────

    @property
    def drive_oauth_client_id(self) -> Optional[str]:
        """Falls back to the Gmail client — one Desktop OAuth client can
        carry both the drive and gmail.send scopes."""
        return self.google_oauth_client_id or self.gmail_oauth_client_id

    @property
    def drive_oauth_client_secret(self) -> Optional[str]:
        return self.google_oauth_client_secret or self.gmail_oauth_client_secret

    @property
    def drive_oauth_configured(self) -> bool:
        return bool(
            self.drive_oauth_client_id
            and self.drive_oauth_client_secret
            and self.drive_oauth_refresh_token
        )

    @property
    def drive_auth_mode(self) -> Optional[str]:
        """'service_account' | 'oauth_user' | None.

        Reported on /health so a deploy that is missing one half of a
        credential pair is visible without reading logs.
        """
        if self.google_drive_credentials_json:
            return "service_account"
        if self.drive_oauth_configured:
            return "oauth_user"
        return None

    @property
    def drive_enabled(self) -> bool:
        return bool(self.drive_auth_mode and self.drive_folder_invoice_uploads)

    @property
    def agent_auth_enabled(self) -> bool:
        return bool(self.agent_api_key)

    @property
    def is_dev(self) -> bool:
        return self.app_env in ("development", "dev", "local")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


# Where each required value comes from. A missing one is a deploy that dies
# on boot with a pydantic traceback, which says the field name and nothing
# about where to get it — and on a hosted deploy that is a full redeploy per
# guess. Naming the dashboard page turns a cycle of those into one.
_WHERE_TO_FIND = {
    "SUPABASE_URL": (
        "Supabase → Project Settings → API → Project URL. "
        "Looks like https://abcdefgh.supabase.co"
    ),
    "SUPABASE_ANON_KEY": (
        "Supabase → Project Settings → API → the `anon` / `public` key. "
        "On newer dashboards it may sit under 'Legacy API keys'; it is a JWT "
        "beginning `eyJ`. It must be the SAME value the frontend uses as "
        "NEXT_PUBLIC_SUPABASE_ANON_KEY, or sign-in verifies against a "
        "different project than the one holding the data."
    ),
    "SUPABASE_SERVICE_ROLE_KEY": (
        "Supabase → Project Settings → API → the `service_role` / `secret` "
        "key. This one bypasses RLS: it belongs on the backend only, and "
        "never in a NEXT_PUBLIC_* variable."
    ),
}


def _near_miss_hint(missing: list[str]) -> list[str]:
    """List the similarly-named variables that ARE set.

    The error pydantic raises names the *field* it wanted, not the variable
    you typed — so `SUPABASE_ANNON_KEY`, a trailing space, and nothing at all
    all produce the identical message. Someone who has just checked that the
    value they pasted is correct has no way to see that the key beside it is
    not. Printing the neighbouring names closes that gap.

    Names only, never values: this goes into a deploy log.
    """
    import os

    stems = {name.split("_")[0] for name in missing}
    present = sorted(
        key
        for key in os.environ
        if any(key.upper().startswith(stem) for stem in stems)
        and key.upper() not in missing
    )
    if not present:
        return []
    return [
        "Related variables that ARE set (names only):",
        *(f"  {key}" for key in present),
        "",
        "If one of those is the value you meant to supply, the name is "
        "wrong — a typo, a trailing space, or a NEXT_PUBLIC_ prefix that "
        "belongs on the frontend only. Fix the name, not the value.",
        "",
    ]


def _load_settings() -> Settings:
    """Build Settings, or fail with an error a person can act on.

    §12 wants fail-fast, and the crash itself is correct — a missing
    SUPABASE_URL should stop the process on boot, not surface as a 500 on the
    first request. What the raw pydantic traceback does not do is say which
    variables are missing and where to get them, so this rewrites it.
    """
    try:
        return Settings()
    except ValidationError as e:
        missing, invalid = [], []
        for err in e.errors():
            name = str(err["loc"][0]).upper() if err["loc"] else "?"
            if err["type"] == "missing":
                missing.append(name)
            else:
                invalid.append(f"{name}: {err['msg']}")

        lines = ["", "The app cannot start: its configuration is incomplete.", ""]
        if missing:
            lines.append(
                f"Missing environment variable(s): {', '.join(sorted(missing))}"
            )
            lines.append("")
            for name in sorted(missing):
                lines.append(f"  {name}")
                lines.append(f"      {_WHERE_TO_FIND.get(name, 'See backend/.env.example')}")
                lines.append("")
            lines.extend(_near_miss_hint(missing))
        if invalid:
            lines.append("Invalid value(s):")
            lines.extend(f"  {item}" for item in invalid)
            lines.append("")
        lines.append(
            "Set these in Render → your service → Environment (or in "
            "backend/.env locally), then redeploy. Full list: "
            "backend/.env.example, and docs/SETUP.md §1.4."
        )
        # `from None`: the pydantic traceback above this point is noise in a
        # deploy log, and the whole point is that the message is readable.
        raise RuntimeError("\n".join(lines)) from None


settings = _load_settings()
