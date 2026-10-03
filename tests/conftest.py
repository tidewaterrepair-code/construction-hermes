"""Test harness: a real PostgreSQL per test, cloned from a migrated template.

Requires a reachable PostgreSQL superuser URL in CHOPS_TEST_ADMIN_URL (default: the local
dev cluster on 127.0.0.1:5433). Tests never touch the dev/prod databases.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

ADMIN_URL = os.environ.get("CHOPS_TEST_ADMIN_URL", "postgresql+psycopg://postgres:devpostgres@127.0.0.1:5433/postgres")
OWNER_ROLE, OWNER_PW = "chops_owner", os.environ.get("CHOPS_TEST_OWNER_PW", "dev_owner")
APP_ROLE, APP_PW = "chops_app", os.environ.get("CHOPS_TEST_APP_PW", "dev_app")
TEMPLATE = f"chops_tpl_{os.getpid()}"


def _host_part() -> str:
    return ADMIN_URL.split("@", 1)[1].rsplit("/", 1)[0]


def db_url(dbname: str, role: str = APP_ROLE, pw: str = APP_PW) -> str:
    return f"postgresql+psycopg://{role}:{pw}@{_host_part()}/{dbname}"


def _admin():
    return create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")


@pytest.fixture(scope="session")
def template_db():
    from chops.migrate import upgrade

    eng = _admin()
    with eng.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{TEMPLATE}"'))
        c.execute(text(f'CREATE DATABASE "{TEMPLATE}" OWNER {OWNER_ROLE}'))
    upgrade(db_url(TEMPLATE, OWNER_ROLE, OWNER_PW))
    yield TEMPLATE
    with eng.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{TEMPLATE}" WITH (FORCE)'))
    eng.dispose()


@pytest.fixture
def fresh_db(template_db, monkeypatch):
    name = f"chops_t_{uuid.uuid4().hex[:10]}"
    eng = _admin()
    with eng.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{template_db}" OWNER {OWNER_ROLE}'))
    data_dir = Path(tempfile.mkdtemp(prefix="chops-test-"))
    monkeypatch.setenv("CHOPS_DATABASE_URL", db_url(name))
    monkeypatch.setenv("CHOPS_MIGRATE_DATABASE_URL", db_url(name, OWNER_ROLE, OWNER_PW))
    monkeypatch.setenv("CHOPS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("CHOPS_ENV", "test")
    monkeypatch.setenv("CHOPS_SECRET_KEY", "test-secret-key-0123456789abcdef")
    from chops import config, db

    config.reset_settings_cache()
    db.reset_engine()
    yield name
    db.reset_engine()
    config.reset_settings_cache()
    with eng.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    eng.dispose()
    shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture
def session(fresh_db):
    from chops.db import session_factory

    s = session_factory()()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def owner(session):
    """An owner user and Actor (dashboard channel)."""
    from chops.authz import Actor
    from chops.services import users

    u = users.create_user(session, None, username="jimmy", display_name="Jimmy Blackwell", role="owner",
                          password="correct horse battery staple", bootstrap=True)
    session.commit()
    return Actor(user_id=u.id, role="owner", via="dashboard", display_name=u.display_name)


@pytest.fixture
def agent(session, owner):
    from chops.authz import Actor
    from chops.services import users

    u = users.create_user(session, owner, username="hermes", display_name="Construction Hermes", role="agent")
    session.commit()
    return Actor(user_id=u.id, role="agent", via="mcp", display_name=u.display_name, conversation_key="telegram:dm")
