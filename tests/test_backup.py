"""Real pg_dump -> encrypted archive -> isolated restore with verification."""

import io
import os
from pathlib import Path

import pytest

from conftest import ADMIN_URL
from helpers import demo_setup, make_job


@pytest.fixture
def backup_env(fresh_db, tmp_path, monkeypatch):
    from chops import backup, config

    key = tmp_path / "backup.key"
    backup.generate_key(key)
    monkeypatch.setenv("CHOPS_BACKUP_KEY_FILE", str(key))
    monkeypatch.setenv("CHOPS_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("CHOPS_RESTORE_ADMIN_URL", ADMIN_URL)
    config.reset_settings_cache()
    return tmp_path


def test_backup_restore_isolated_and_cannot_send(backup_env, session, owner, agent):
    from chops import backup
    from chops.models import OutboxJob
    from chops.services import billing, documents, settings

    demo_setup(session, owner)
    job_id = make_job(session, owner, agent)
    documents.store(session, agent, b"%PDF-1.4\n% synthetic plan\n%%EOF", filename="plan.pdf", kind="plan", job_id=job_id)
    inv = billing.draft_milestone_invoice(session, agent, job_id, "deposit")["invoice"]
    billing.request_issue(session, agent, inv["id"])          # a pending approval must survive
    session.commit()
    # Live database is SHADOW/demo; make the source look LIVE+prod to prove the copy is forced safe.
    settings._write(session, owner, "environment", "prod")
    settings._write(session, owner, "mode", "LIVE")
    session.commit()
    pending_external = session.query(OutboxJob).filter(OutboxJob.external_effect.is_(True)).count()
    assert pending_external >= 1

    res = backup.create_backup(label="test")
    arch = Path(res["archive"])
    assert arch.exists() and oct(arch.stat().st_mode & 0o777) == "0o600"
    assert b"synthetic plan" not in arch.read_bytes()             # encrypted at rest

    rt = backup.restore_test(str(arch))
    assert rt["ok"], rt
    assert rt["checks"]["row_counts_match"] and rt["checks"]["documents_verified"]
    assert rt["pending_approvals_preserved"] == 1
    assert rt["checks"]["dispatch_blocked"]
    # The live database was not touched by the restore test.
    session.expire_all()
    assert settings.mode(session) == "LIVE" and not settings.kill_engaged(session)


def test_tampered_or_truncated_backup_is_rejected(backup_env):
    from chops import backup

    key = os.urandom(32)
    data = os.urandom(3 * 1024 * 1024 + 17)
    enc = io.BytesIO()
    backup.encrypt_stream(io.BytesIO(data), enc, key)
    blob = enc.getvalue()
    out = io.BytesIO()
    backup.decrypt_stream(io.BytesIO(blob), out, key)
    assert out.getvalue() == data
    tampered = bytearray(blob)
    tampered[len(blob) // 2] ^= 1
    with pytest.raises(Exception):
        backup.decrypt_stream(io.BytesIO(bytes(tampered)), io.BytesIO(), key)
    with pytest.raises(ValueError):
        backup.decrypt_stream(io.BytesIO(blob[: len(blob) // 2]), io.BytesIO(), key)


def test_backup_refuses_without_key(fresh_db, monkeypatch, tmp_path):
    from chops import backup, config

    monkeypatch.delenv("CHOPS_BACKUP_KEY_FILE", raising=False)
    monkeypatch.setenv("CHOPS_BACKUP_DIR", str(tmp_path))
    config.reset_settings_cache()
    with pytest.raises(RuntimeError):
        backup.create_backup()
