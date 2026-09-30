"""
Application settings, loaded from environment variables (.env in dev, Render
env vars in prod).

Typed and fail-fast (prompt §12): any field without a default must be present
at import time or the process refuses to start. A missing SUPABASE_URL should
crash on boot, not on the first request.
"""

from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    google_drive_credentials_json: Optional[str] = None   # service account JSON
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

    @property
    def drive_enabled(self) -> bool:
        return bool(
            self.google_drive_credentials_json
            and self.drive_folder_invoice_uploads
        )

    @property
    def agent_auth_enabled(self) -> bool:
        return bool(self.agent_api_key)

    @property
    def is_dev(self) -> bool:
        return self.app_env in ("development", "dev", "local")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
