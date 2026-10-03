"""Process configuration (environment only; business policy lives in the database)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHOPS_", env_file=None, extra="ignore")

    # dev | test | demo | prod. "demo" databases hold synthetic records only.
    env: str = "dev"
    database_url: str = "postgresql+psycopg://chops_app:dev_app@127.0.0.1:5433/chops"
    # Used only by `chops migrate` (DDL). The runtime app role cannot alter schema.
    migrate_database_url: str | None = None
    secret_key: SecretStr = SecretStr("dev-only-insecure-secret-change-me")
    data_dir: Path = Path("./var/data")
    base_url: str = "http://127.0.0.1:8640"
    cookie_secure: bool = False
    timezone: str = "America/New_York"

    # Upload limits
    max_upload_bytes: int = 25 * 1024 * 1024
    max_extracted_chars: int = 2_000_000

    # Session/approval lifetimes
    session_hours: int = 12
    approval_ttl_hours: int = 72

    # Worker
    worker_concurrency: int = 2
    worker_lease_seconds: int = 120
    worker_poll_seconds: float = 2.0

    # Outbound allowlist for integration adapters (comma separated hostnames).
    outbound_hosts: str = "api.telegram.org"

    # Telegram (application notifications only; the Hermes gateway has its own token)
    telegram_bot_token: SecretStr | None = None
    telegram_owner_chat_id: str | None = None

    # SMTP for customer email (disconnected unless all set)
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None

    # Backup encryption key file (Fernet key). Backups refuse to run without it.
    backup_key_file: Path | None = None
    backup_dir: Path = Path("./var/backups")

    rate_limit_login_per_min: int = Field(default=10)

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
