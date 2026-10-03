"""Health checks used by /healthz, `chops health`, and container healthchecks."""

from __future__ import annotations

import datetime as dt
import shutil
from typing import Any

from sqlalchemy import func, select, text

from . import timeutil
from .config import get_settings
from .db import session_scope
from .models import OutboxJob
from .services import integrations, settings


def _head_revision() -> str:
    from alembic.script import ScriptDirectory

    from .migrate import alembic_config

    return ScriptDirectory.from_config(alembic_config("postgresql://unused")).get_current_head()


def check() -> dict[str, Any]:
    out: dict[str, Any] = {"ok": True, "checks": {}}

    def fail(name: str, detail: Any) -> None:
        out["ok"] = False
        out["checks"][name] = {"ok": False, "detail": detail}

    try:
        with session_scope() as s:
            s.execute(text("SELECT 1"))
            rev = s.scalar(text("SELECT version_num FROM alembic_version"))
            head = _head_revision()
            out["checks"]["database"] = {"ok": True}
            if rev != head:
                fail("migrations", f"database at {rev}, code expects {head}")
            else:
                out["checks"]["migrations"] = {"ok": True, "revision": rev}
            out["mode"] = settings.mode(s)
            out["environment"] = settings.environment(s)
            out["kill_switch"] = settings.kill_switch(s)
            counts = dict(s.execute(select(OutboxJob.status, func.count()).group_by(OutboxJob.status)).all())
            out["outbox"] = counts
            stuck = s.scalar(select(func.count()).select_from(OutboxJob).where(
                OutboxJob.status == "pending", OutboxJob.next_attempt_at < timeutil.now() - dt.timedelta(minutes=10)))
            hb = settings.get(s, "worker_heartbeat")
            if not hb or timeutil.now() - dt.datetime.fromisoformat(hb["at"]) > dt.timedelta(minutes=5):
                out["checks"]["worker"] = {"ok": False, "detail": "no worker heartbeat in 5 minutes", "warn_only": True}
            else:
                out["checks"]["worker"] = {"ok": True, "last": hb["at"]}
            if stuck:
                out["checks"]["outbox_backlog"] = {"ok": False, "detail": f"{stuck} jobs pending >10 min", "warn_only": True}
            out["integrations"] = {r["name"]: r["status"] for r in integrations.status_list(s)}
    except Exception as exc:  # noqa: BLE001
        fail("database", f"{type(exc).__name__}: {str(exc)[:200]}")
    cfg = get_settings()
    try:
        cfg.documents_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.documents_dir / ".healthprobe"
        probe.write_text("ok")
        probe.unlink()
        free = shutil.disk_usage(cfg.documents_dir).free
        out["checks"]["storage"] = {"ok": free > 1_000_000_000, "free_gb": round(free / 1e9, 1)}
        if free <= 1_000_000_000:
            out["ok"] = False
    except OSError as exc:
        fail("storage", str(exc))
    return out
