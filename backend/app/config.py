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

    # --- uploads ---
    max_upload_bytes: int = 50 * 1024 * 1024  # 50 MB cap (DoS guard)

    # --- static SPA ---
    # Empty locally (dev uses the Vite dev server + proxy). In the container this
    # points at the built React app, which FastAPI serves same-origin.
    static_dir: str = ""

    # --- misc ---
    cors_allow_origins: list[str] = []  # SPA is served same-origin; empty by design

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()
