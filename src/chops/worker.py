"""Background worker: outbox dispatch and scheduled owner routines.

- Bounded concurrency (CHOPS_WORKER_CONCURRENCY), leases with expiry, recovery on start.
- Each job is dispatched in its own transaction after the lease is committed.
- Routines run at local (America/New_York) times; a missed run is caught up once on the
  same local day, never replayed repeatedly. Routines send nothing when nothing changed.
- SIGTERM stops leasing; in-flight jobs finish (or their leases expire and are recovered).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import signal
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import select

from . import timeutil
from .authz import Actor
from .config import get_settings
from .db import session_scope
from .models import Notification, Routine
from .services import digest, integrations, outbox, settings

log = logging.getLogger("chops.worker")
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
_stop = threading.Event()
SYSTEM = Actor(user_id=None, role="owner", via="worker", display_name="worker")

DEFAULT_ROUTINES = [
    ("morning_priorities", "Morning priorities (money, jobs, leads, commitments)", "06:45", "0,1,2,3,4,5"),
    ("end_of_day_exceptions", "End-of-day exceptions", "17:30", "0,1,2,3,4"),
    ("weekly_job_cost", "Weekly job-cost review", "07:15", "0"),
    ("proposal_followup", "Proposals awaiting a customer answer", "09:10", "0,1,2,3,4"),
    ("receivables", "Receivables and reported payments", "08:10", "0,3"),
]


def ensure_routines(session) -> None:
    for name, desc, at, days in DEFAULT_ROUTINES:
        if session.get(Routine, name) is None:
            # Disabled by default: the owner enables each routine and its channel.
            session.add(Routine(name=name, description=desc, local_time=at, weekdays=days, enabled=False,
                                channel="dashboard", uses_model=False))


def routine_due(r: Routine, now_utc: dt.datetime) -> bool:
    """Due if today's local slot has passed and it has not run since that slot (catch-up once)."""
    local_now = now_utc.astimezone(timeutil.tz())
    if str(local_now.weekday()) not in r.weekdays.split(","):
        return False
    hh, mm = (int(x) for x in r.local_time.split(":"))
    slot_local = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    # Normalize through UTC so a slot inside a DST gap/overlap resolves deterministically.
    slot_utc = slot_local.astimezone(dt.timezone.utc)
    if now_utc < slot_utc:
        return False
    return r.last_run_at is None or r.last_run_at < slot_utc


def _routine_text(session, name: str) -> str | None:
    owner_view = Actor(user_id=None, role="owner", via="worker")
    if name == "morning_priorities":
        d = digest.today(session, owner_view)
        return None if d["nothing_urgent"] else digest.render_text(d)
    if name == "end_of_day_exceptions":
        from .services import costing, schedule

        ex = costing.exceptions(session, owner_view)
        cf = schedule.detect_conflicts(session, owner_view, days=2)["conflicts"]
        lines = [e["message"] for e in ex] + [c["message"] for c in cf]
        return None if not lines else "End of day:\n" + "\n".join(f"- {x}" for x in lines[:8])
    if name == "weekly_job_cost":
        from .services import costing, jobs

        rows = []
        for j in jobs.list_jobs(session, owner_view):
            r = costing.job_cost_report(session, owner_view, int(j["ref"].split("-")[1]))
            rows.append(f"- {j['ref']} {j['name']}: projected margin {r['projected_gross_margin'] or 'n/a'} "
                        f"(est {r['estimated_gross_margin'] or 'n/a'})")
        return None if not rows else "Weekly job cost:\n" + "\n".join(rows[:10])
    if name == "proposal_followup":
        from .services import proposals

        rows = [p for p in proposals.list_proposals(session, owner_view) if p["status"] in ("approved", "issued")]
        return None if not rows else "Proposals waiting on customers:\n" + "\n".join(
            f"- {p['ref']} ${p['total']} valid until {p['valid_until'] or '?'}" for p in rows[:8])
    if name == "receivables":
        from .services import billing

        rows = billing.receivables(session, owner_view)
        return None if not rows else "Open receivables:\n" + "\n".join(
            f"- {r['number']} ${r['open_amount']} {('overdue ' + str(r['days_overdue']) + 'd') if r['days_overdue'] else ''}"
            for r in rows[:10])
    return None


def run_routines(now_utc: dt.datetime | None = None) -> list[str]:
    now_utc = now_utc or timeutil.now()
    ran = []
    with session_scope() as s:
        ensure_routines(s)
        for r in s.scalars(select(Routine).where(Routine.enabled.is_(True))):
            if not routine_due(r, now_utc):
                continue
            if r.uses_model and not settings.get(s, "budget"):
                r.last_result = "skipped: model-using routines need an owner budget"
                r.last_run_at = now_utc
                continue
            text = _routine_text(s, r.name)
            r.last_run_at = now_utc
            if text is None:
                r.last_result = "nothing new; no message"
                continue
            fp = hashlib.sha256(f"{r.name}:{text}".encode()).hexdigest()
            last = s.scalar(select(Notification).where(Notification.kind == f"routine:{r.name}")
                            .order_by(Notification.id.desc()).limit(1))
            if last is not None and last.body == text:
                r.last_result = "unchanged since last run; no message"
                continue
            local_day = now_utc.astimezone(timeutil.tz()).date().isoformat()
            outbox.enqueue(s, kind="inbox.notify", external_effect=False,
                           idempotency_key=f"routine-{r.name}-{local_day}-{fp[:16]}",
                           payload={"kind": f"routine:{r.name}", "title": r.description, "body": text})
            if r.channel == "telegram":
                outbox.enqueue(s, kind="telegram.notify", external_effect=True,
                               idempotency_key=f"routine-tg-{r.name}-{local_day}-{fp[:16]}", payload={"text": text})
            r.last_result = "queued"
            ran.append(r.name)
    return ran


def _dispatch_one(job_id: int) -> str:
    try:
        with session_scope() as s:
            return outbox.dispatch(s, job_id)
    except Exception:  # noqa: BLE001
        log.exception("dispatch crashed for job %s; lease will expire and be recovered", job_id)
        return "error"


def tick(pool: ThreadPoolExecutor | None = None) -> dict[str, Any]:
    cfg = get_settings()
    with session_scope() as s:
        recovered = outbox.recover_expired_leases(s)
        integrations.refresh_configured(s)
        if _heartbeat_due(s):
            settings._write(s, SYSTEM, "worker_heartbeat", {"at": timeutil.now().isoformat(), "worker": WORKER_ID})
    with session_scope() as s:
        ids = outbox.lease(s, WORKER_ID, cfg.worker_concurrency * 2, cfg.worker_lease_seconds)
    results = list(pool.map(_dispatch_one, ids)) if pool and ids else [_dispatch_one(i) for i in ids]
    routines = run_routines()
    return {"recovered": recovered, "dispatched": dict(zip(ids, results)), "routines": routines}


def _heartbeat_due(session) -> bool:
    hb = settings.get(session, "worker_heartbeat")
    if not hb:
        return True
    return timeutil.now() - dt.datetime.fromisoformat(hb["at"]) > dt.timedelta(seconds=60)


def run_forever(once: bool = False) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = get_settings()
    signal.signal(signal.SIGTERM, lambda *_: _stop.set())
    signal.signal(signal.SIGINT, lambda *_: _stop.set())
    log.info("worker %s starting (concurrency=%s)", WORKER_ID, cfg.worker_concurrency)
    with ThreadPoolExecutor(max_workers=cfg.worker_concurrency) as pool:
        while not _stop.is_set():
            try:
                res = tick(pool)
                if res["dispatched"] or res["routines"] or any(res["recovered"].values()):
                    log.info("tick %s", res)
            except Exception:  # noqa: BLE001 - keep the loop alive; DB outages surface in health
                log.exception("worker tick failed")
            if once:
                break
            _stop.wait(cfg.worker_poll_seconds)
    log.info("worker stopped")
