"""Transactional outbox: business changes and their external effects commit together.

Delivery semantics
- Jobs are written in the same transaction as the approval execution; nothing is sent
  before commit.
- Workers lease jobs with SELECT ... FOR UPDATE SKIP LOCKED; a lease that expires while an
  external send was in flight becomes UNKNOWN (never blindly resent) unless the adapter
  supports provider-side idempotency.
- Transient errors retry with exponential backoff up to max_attempts, then DEAD.
- Kill switch and mode are rechecked immediately before dispatch. In BUILD/SHADOW, or for
  synthetic records, external jobs are SIMULATED (recorded, not sent).
"""

from __future__ import annotations

import datetime as dt
import logging
import random
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .. import timeutil
from ..authz import Actor, require
from ..errors import Blocked, InvalidTransition, NotFound
from ..hashing import jsonable
from ..models import Notification, OutboxJob
from ..refs import ref
from . import audit, integrations, settings

log = logging.getLogger("chops.outbox")
SYSTEM = Actor(user_id=None, role="owner", via="worker", display_name="worker")


def enqueue(session: Session, *, kind: str, payload: dict[str, Any], idempotency_key: str, external_effect: bool,
            approval_id: int | None = None, synthetic: bool = False, max_attempts: int = 5) -> OutboxJob:
    stmt = insert(OutboxJob).values(
        kind=kind, payload=jsonable(payload), idempotency_key=idempotency_key, external_effect=external_effect,
        approval_id=approval_id, status="pending", is_synthetic=synthetic, max_attempts=max_attempts,
        next_attempt_at=timeutil.now(),
    ).on_conflict_do_nothing(index_elements=["idempotency_key"])
    session.execute(stmt)
    session.flush()
    return session.scalar(select(OutboxJob).where(OutboxJob.idempotency_key == idempotency_key))


def outbox_view(j: OutboxJob) -> dict[str, Any]:
    return {"ref": ref("outbox", j.id), "kind": j.kind, "status": j.status, "attempts": j.attempts,
            "external_effect": j.external_effect, "approval": ref("approval", j.approval_id),
            "last_error": j.last_error, "provider_ref": j.provider_ref, "result": j.result,
            "created_at": timeutil.iso(j.created_at), "completed_at": timeutil.iso(j.completed_at),
            "next_attempt_at": timeutil.iso(j.next_attempt_at), "synthetic": j.is_synthetic}


def lease(session: Session, worker_id: str, limit: int, lease_seconds: int) -> list[int]:
    rows = session.execute(text(
        """
        UPDATE outbox_jobs SET status='leased', lease_owner=:w, lease_expires_at=now() + make_interval(secs => :ls),
               attempts = attempts + 1
        WHERE id IN (SELECT id FROM outbox_jobs WHERE status='pending' AND next_attempt_at <= now()
                     ORDER BY id FOR UPDATE SKIP LOCKED LIMIT :n)
        RETURNING id
        """), {"w": worker_id, "ls": lease_seconds, "n": limit}).scalars().all()
    return list(rows)


def recover_expired_leases(session: Session) -> dict[str, int]:
    """Called at worker start and periodically. Crashed internal jobs retry; crashed external
    sends without provider idempotency become UNKNOWN for owner investigation."""
    out = {"retried": 0, "unknown": 0}
    q = (select(OutboxJob).where(OutboxJob.status == "leased", OutboxJob.lease_expires_at < timeutil.now())
         .with_for_update(skip_locked=True).execution_options(populate_existing=True))
    for j in session.scalars(q):
        adapter = integrations.adapter_for(j.kind)
        if j.external_effect and not (adapter and adapter.idempotent):
            j.status = "unknown"
            j.last_error = "worker lease expired during dispatch; delivery state unknown - verify before resending"
            _notify(session, "dispatch_unknown", f"Delivery state unknown: {j.kind}", j.last_error, f"obx-unknown-{j.id}")
            out["unknown"] += 1
        else:
            j.status = "pending"
            j.lease_owner = None
            out["retried"] += 1
    return out


def _backoff(attempts: int) -> dt.timedelta:
    base = min(3600, 2 ** attempts * 5)
    return dt.timedelta(seconds=base * (0.8 + random.random() * 0.4))


def dispatch(session: Session, job_id: int) -> str:
    """Process one leased job inside the caller's transaction. Returns the final status."""
    # lease() updates rows with SQL; always reload so a cached object cannot hide the lease.
    j = session.get(OutboxJob, job_id, populate_existing=True, with_for_update=True)
    if j is None or j.status != "leased":
        return "skipped"
    adapter = integrations.adapter_for(j.kind)
    if j.external_effect:
        if settings.kill_engaged(session):
            j.status = "blocked"
            j.last_error = "kill switch engaged at dispatch time"
            return j.status
        if settings.mode(session) != "LIVE" or j.is_synthetic or settings.environment(session) != "prod":
            j.status = "simulated"
            j.completed_at = timeutil.now()
            j.result = {"simulated": True, "mode": settings.mode(session),
                        "reason": "not LIVE" if settings.mode(session) != "LIVE" else "synthetic/non-prod record",
                        "would_send": j.payload}
            audit.record(session, SYSTEM, "outbox.simulated", "outbox", j.id, kind=j.kind)
            return j.status
    if adapter is None:
        j.status = "failed"
        j.last_error = f"no adapter for {j.kind}"
        return j.status
    if not adapter.is_connected(session):
        j.status = "blocked"
        j.last_error = f"{adapter.name} is not connected"
        return j.status
    try:
        result = adapter.send(session, j.payload, j.idempotency_key)
    except integrations.TransientError as exc:
        integrations.mark_error(session, adapter.name, str(exc), degraded=True)
        if j.attempts >= j.max_attempts:
            j.status = "dead"
            j.last_error = f"gave up after {j.attempts} attempts: {exc}"
            _notify(session, "dispatch_dead", f"Delivery failed: {j.kind}", j.last_error, f"obx-dead-{j.id}")
        else:
            j.status = "pending"
            j.last_error = str(exc)[:500]
            j.next_attempt_at = timeutil.now() + (exc.retry_after or _backoff(j.attempts))
        return j.status
    except integrations.AmbiguousError as exc:
        integrations.mark_error(session, adapter.name, str(exc), degraded=True)
        if adapter.idempotent and j.attempts < j.max_attempts:
            j.status = "pending"
            j.next_attempt_at = timeutil.now() + _backoff(j.attempts)
        else:
            j.status = "unknown"
            _notify(session, "dispatch_unknown", f"Delivery state unknown: {j.kind}", str(exc), f"obx-unknown-{j.id}")
        j.last_error = str(exc)[:500]
        return j.status
    except integrations.PermanentError as exc:
        integrations.mark_error(session, adapter.name, str(exc), degraded=False)
        j.status = "failed"
        j.last_error = str(exc)[:500]
        _notify(session, "dispatch_failed", f"Delivery failed: {j.kind}", j.last_error, f"obx-failed-{j.id}")
        return j.status
    integrations.mark_success(session, adapter.name)
    j.status = "succeeded"
    j.completed_at = timeutil.now()
    j.provider_ref = (result or {}).get("provider_ref")
    j.result = jsonable(result or {})
    audit.record(session, SYSTEM, "outbox.succeeded", "outbox", j.id, kind=j.kind, provider_ref=j.provider_ref)
    return j.status


def _notify(session: Session, kind: str, title: str, body: str, fingerprint: str) -> None:
    if session.scalar(select(Notification).where(Notification.fingerprint == fingerprint)) is None:
        session.add(Notification(kind=kind, title=title[:200], body=body, fingerprint=fingerprint))


def list_jobs(session: Session, actor: Actor, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    require(actor, "read:all")
    q = select(OutboxJob).order_by(OutboxJob.id.desc()).limit(min(limit, 200))
    if status:
        q = q.where(OutboxJob.status == status)
    return [outbox_view(j) for j in session.scalars(q)]


def resolve_unknown(session: Session, actor: Actor, job_id: int, outcome: str, evidence: str) -> dict[str, Any]:
    """Owner investigates an UNKNOWN delivery: mark delivered, or explicitly allow one resend."""
    require(actor, "decide:approval")
    j = session.get(OutboxJob, job_id)
    if j is None:
        raise NotFound("outbox job not found")
    if j.status not in ("unknown", "blocked", "dead", "failed"):
        raise InvalidTransition(f"job is {j.status}")
    if outcome == "delivered":
        j.status = "succeeded"
        j.completed_at = timeutil.now()
    elif outcome == "resend":
        if settings.kill_engaged(session):
            raise Blocked("kill switch engaged")
        j.status = "pending"
        j.next_attempt_at = timeutil.now()
        j.max_attempts = j.attempts + 1
    elif outcome == "abandon":
        j.status = "dead"
    else:
        raise InvalidTransition("outcome must be delivered, resend, or abandon")
    audit.record(session, actor, "outbox.resolve", "outbox", j.id, outcome=outcome, evidence=evidence)
    return outbox_view(j)
