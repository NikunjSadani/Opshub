"""Application settings (env-driven, per environment).

Single source of runtime config. Never hardcode env-specific values in code —
they belong here, read from the environment / Secret Manager.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- environment ---
    env: Literal["local", "staging", "prod"] = "local"
    app_name: str = "Gifsy OpsHub"

    # --- database ---
    # Prod/staging use Postgres (Cloud SQL). Local bootstrap defaults to a sqlite
    # file so the skeleton runs with no Postgres instance; real work uses Postgres.
    database_url: str = "sqlite:///./opshub_local.db"

    # --- auth (Firebase) ---
    # Path to the Firebase service-account JSON, or rely on GOOGLE_APPLICATION_CREDENTIALS.
    firebase_credentials_file: str | None = None
    firebase_project_id: str | None = None

    # --- dev auth shim (LOCAL ONLY) ---
    # When True AND env=='local', current_user skips Firebase and resolves to the
    # seeded user `dev_auth_uid` for any bearer token. Ignored in staging/prod
    # (double-guarded on env below) so it can never weaken real auth. Default OFF.
    dev_auth: bool = False
    dev_auth_uid: str = "dev-admin"

    # --- stub PDF renderer (LOCAL ONLY) ---
    # When True AND env=='local', challan generation uses a tiny native-free PDF
    # stub instead of WeasyPrint (which is container-only), so the full
    # generate -> register -> void lifecycle runs locally / in the E2E harness.
    # Double-guarded on env below; a non-local env ALWAYS uses WeasyPrint. Default OFF.
    stub_render: bool = False

    # --- uploads ---
    max_upload_bytes: int = 50 * 1024 * 1024  # 50 MB cap (DoS guard)

    # --- static SPA ---
    # Empty locally (dev uses the Vite dev server + proxy). In the container this
    # points at the built React app, which FastAPI serves same-origin.
    static_dir: str = ""

    # --- public challan-QR invoice access (owner-gated feature) ---
    # Base URL the challan QR encodes (e.g. "https://ops.example.com"). Empty -> no QR is
    # stamped on the challan (the feature is inert until a deploy sets this).
    public_base_url: str = ""
    # MASTER SWITCH for the public, no-login invoice-viewer endpoint. Default OFF: even once
    # deployed the endpoint stays disabled (404) until the owner deliberately flips this on
    # (after setting per-client PINs). A kill-switch for all public invoice access.
    qr_invoice_access_enabled: bool = False
    # Retention horizon for the public invoice-access audit log. Rows older than this are
    # pruned (defense-in-depth against unbounded growth) during the secret-gated sweep.
    invoice_access_retention_days: int = 180

    # --- reconcile sweep (scheduler) ---
    # Shared secret gating POST /numbering/sweep. Unset/empty -> the endpoint is
    # disabled (fail-closed 503); set via Secret Manager so only the scheduler calls it.
    sweep_secret: str | None = None

    # --- email (MSG91 SMTP relay — FAIL-CLOSED) ---
    # Transactional email (staff invite / password-setup) sends via MSG91's SMTP relay
    # (Domain Settings -> SMTP Integration) over STARTTLS, reusing loyalty's ALREADY-
    # verified sending domain notify.gifsy.in (SPF/DKIM/DMARC). FAIL-CLOSED: with
    # `msg91_smtp_pass` unset the sender is a no-op (see app/platform/email.py
    # get_email_sender), so deploying changes nothing until the owner adds the secret.
    # Host/port/user default to the verified relay and are env-overridable; the
    # from-address MUST be on the verified domain.
    msg91_smtp_pass: str | None = None
    email_from: str = "opshub@notify.gifsy.in"
    msg91_smtp_host: str = "smtp.mailer91.com"
    msg91_smtp_port: int = 587
    msg91_smtp_user: str = "emailer@notify.gifsy.in"

    # --- access tracking (inc 38) ---
    # GET /me stamps the caller's `last_seen_at` at most this often (seconds), so
    # "last active" tracking never becomes a write on every request. Default 5 min.
    last_seen_throttle_seconds: int = 300
    # POST /auth/login-event coalesces a repeat sign-in with the SAME (ip, user_agent)
    # within this window (seconds) into no new row, so an authenticated client can't loop
    # the endpoint to flood login_event on the small prod DB. Default 5 min.
    login_event_coalesce_seconds: int = 300
    # Retention horizon for login_event: rows older than this are pruned during the
    # secret-gated sweep (defense-in-depth vs unbounded growth). audit_log is NEVER pruned
    # (tamper-evident compliance record). Default 180 days.
    login_event_retention_days: int = 180

    # --- misc ---
    cors_allow_origins: list[str] = []  # SPA is served same-origin; empty by design

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()
