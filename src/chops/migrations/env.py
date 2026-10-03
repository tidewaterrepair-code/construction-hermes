"""Alembic environment. Uses CHOPS_MIGRATE_DATABASE_URL (schema owner), never the runtime app role."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from chops.config import get_settings
from chops.models import Base

target_metadata = Base.metadata


def _url() -> str:
    explicit = context.config.attributes.get("url")
    if explicit:
        return explicit
    s = get_settings()
    return s.migrate_database_url or s.database_url


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations_online()
