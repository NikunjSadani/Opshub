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

    # --- uploads ---
    max_upload_bytes: int = 50 * 1024 * 1024  # 50 MB cap (DoS guard)

    # --- static SPA ---
    # Empty locally (dev uses the Vite dev server + proxy). In the container this
    # points at the built React app, which FastAPI serves same-origin.
    static_dir: str = ""

    # --- reconcile sweep (scheduler) ---
    # Shared secret gating POST /numbering/sweep. Unset/empty -> the endpoint is
    # disabled (fail-closed 503); set via Secret Manager so only the scheduler calls it.
    sweep_secret: str | None = None

    # --- misc ---
    cors_allow_origins: list[str] = []  # SPA is served same-origin; empty by design

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()
