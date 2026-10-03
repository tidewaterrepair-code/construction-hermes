"""Programmatic Alembic upgrade (used by the CLI, deploy, and tests)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(url: str) -> Config:
    cfg = Config(str(MIGRATIONS_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["url"] = url
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def current(url: str) -> str | None:
    from sqlalchemy import create_engine, text

    eng = create_engine(url)
    try:
        with eng.connect() as c:
            return c.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:
        return None
    finally:
        eng.dispose()
