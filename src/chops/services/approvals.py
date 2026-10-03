"""Approval records for consequential actions.

Contract
- A request snapshots the exact action payload, computed server-side from current records,
  and stores its SHA-256. The approver sees that payload.
- A decision must come from the owner over a verified channel and must present the hash
  it was shown. Expired, already-decided, or changed requests cannot be approved.
- Execution re-derives the payload from current records; any difference (content, amount,
  destination, revision) invalidates the approval. Execution is single-use (atomic status
  flip approved -> executing), and the kill switch is checked again at dispatch time.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import timeutil
from ..authz import Actor, require, require_decider
from ..config import get_settings
from ..errors import Ambiguous, Blocked, Conflict, InvalidTransition, NotFound, ValidationFailed
from ..hashing import content_hash, jsonable
from ..models import Approval
from ..money import D
from ..refs import ref
from . import audit, settings


@dataclass
class ActionSpec:
    target_type: str
    # (session, target_id, extra) -> dict(payload, target_revision, summary, destination, amount)
    build: Callable[[Session, int, dict[str, Any]], dict[str, Any]]
    # (session, approval, actor) -> result dict
    execute: Callable[[Session, Approval, Actor], dict[str, Any]]
    money_movement: bool = False
    allow_standing_policy: bool = True


REGISTRY: dict[str, ActionSpec] = {}


def register(action_type: str, spec: ActionSpec) -> None:
    REGISTRY[action_type] = spec


def _spec(action_type: str) -> ActionSpec:
    _ensure_registered()
    if action_type not in REGISTRY:
        raise ValidationFailed(f"unknown action type {action_type}; known: {sorted(REGISTRY)}")
    return REGISTRY[action_type]


def _ensure_registered() -> None:
    # Action handlers live with their domain services; importing registers them.
    from . import actions  # noqa: F401


def approval_view(a: Approval) -> dict[str, Any]:
    return {
        "ref": ref("approval", a.id), "id": a.id, "action_type": a.action_type, "status": a.status,
        "summary": a.summary, "target": f"{a.target_type}:{a.target_id}", "target_revision": a.target_revision,
        "destination": a.destination, "amount": None if a.amount is None else str(a.amount),
        "payload": a.payload, "payload_hash": a.payload_hash,
        "requested_via": a.requested_via, "decided_via": a.decided_via,
        "created_at": timeutil.iso(a.created_at), "expires_at": timeutil.iso(a.expires_at),
        "expires_local": timeutil.fmt_local(a.expires_at),
        "decided_at": timeutil.iso(a.decided_at), "execution_result": a.execution_result,
        "synthetic": a.is_synthetic,
    }


def _build(session: Session, spec: ActionSpec, target_id: int, extra: dict[str, Any]) -> dict[str, Any]:
    built = spec.build(session, target_id, extra)
    payload = jsonable(built["payload"])
    return {**built, "payload": payload, "hash": content_hash({"payload": payload,
                                                               "target_revision": built["target_revision"],
                                                               "destination": built.get("destination"),
                                                               "amount": built.get("amount")})}


def request(session: Session, actor: Actor, action_type: str, target_id: int, *, extra: dict[str, Any] | None = None,
            synthetic: bool = False) -> dict[str, Any]:
    require(actor, "request:approval")
    spec = _spec(action_type)
    extra = extra or {}
    built = _build(session, spec, target_id, extra)
    # Reuse an identical pending request instead of piling up duplicates.
    existing = session.scalar(select(Approval).where(
        Approval.action_type == action_type, Approval.target_id == target_id, Approval.status == "pending",
        Approval.payload_hash == built["hash"]))
    if existing is not None and existing.expires_at > timeutil.now():
        return {"approval": approval_view(existing), "reused": True}
    # Older pending requests for the same target with different content are now stale.
    for stale in session.scalars(select(Approval).where(
            Approval.action_type == action_type, Approval.target_id == target_id, Approval.status == "pending")):
        stale.status = "invalidated"
        stale.decision_note = "superseded by a newer request with different content"
    ttl = int(settings.get(session, "approval_ttl_hours", get_settings().approval_ttl_hours))
    a = Approval(
        action_type=action_type, target_type=spec.target_type, target_id=target_id,
        target_revision=str(built["target_revision"]), summary=built["summary"][:500], payload=built["payload"],
        payload_hash=built["hash"], destination=built.get("destination"),
        amount=None if built.get("amount") is None else D(built["amount"]), status="pending",
        requested_by_id=actor.user_id, requested_via=actor.via, conversation_key=actor.conversation_key,
        expires_at=timeutil.now() + dt.timedelta(hours=ttl), is_synthetic=synthetic or bool(extra.get("synthetic")),
    )
    session.add(a)
    session.flush()
    audit.record(session, actor, "approval.request", "approval", a.id, action_type=action_type,
                 target=f"{spec.target_type}:{target_id}", payload_hash=a.payload_hash)
    standing = _standing_policy_match(session, a, spec)
    if standing is not None:
        system = Actor(user_id=None, role="owner", via="standing_policy", display_name=f"standing policy {standing}")
        _mark_decided(session, a, system, "approved", note=f"standing policy #{standing}")
        a.decided_by_id = _owner_id(session)
        result = _execute(session, a, system)
        return {"approval": approval_view(a), "auto_approved_by_policy": standing, "result": result}
    return {"approval": approval_view(a), "reused": False}


def _owner_id(session: Session) -> int | None:
    from ..models import User

    return session.scalar(select(User.id).where(User.role == "owner", User.active.is_(True)).limit(1))


def _standing_policy_match(session: Session, a: Approval, spec: ActionSpec) -> int | None:
    if spec.money_movement or not spec.allow_standing_policy:
        return None
    for idx, pol in enumerate(settings.get(session, "standing_policies", []) or []):
        if pol.get("action_type") != a.action_type:
            continue
        exp = pol.get("expires")
        if exp and dt.date.fromisoformat(exp) < timeutil.today_local():
            continue
        if pol.get("max_amount") is not None and (a.amount is None or a.amount > D(pol["max_amount"])):
            continue
        if pol.get("destination") and pol["destination"] != a.destination:
            continue
        return idx
    return None


def get(session: Session, actor: Actor, approval_id: int) -> Approval:
    require(actor, "read:all")
    a = session.get(Approval, approval_id)
    if a is None:
        raise NotFound(f"{ref('approval', approval_id)} not found")
    _expire_if_due(a)
    return a


def _expire_if_due(a: Approval) -> None:
    if a.status == "pending" and a.expires_at <= timeutil.now():
        a.status = "expired"


def list_pending(session: Session, actor: Actor, include_synthetic: bool = False) -> list[dict[str, Any]]:
    require(actor, "read:all")
    q = select(Approval).where(Approval.status == "pending").order_by(Approval.created_at)
    if not include_synthetic:
        q = q.where(Approval.is_synthetic.is_(False))
    out = []
    for a in session.scalars(q):
        _expire_if_due(a)
        if a.status == "pending":
            out.append(approval_view(a))
    return out


def resolve_bare_confirmation(session: Session, actor: Actor, conversation_key: str | None = None) -> Approval:
    """Map a bare "yes" to exactly one active request; otherwise refuse with the candidates."""
    require(actor, "read:all")
    key = conversation_key or actor.conversation_key
    q = select(Approval).where(Approval.status == "pending", Approval.expires_at > timeutil.now())
    if key:
        q = q.where(Approval.conversation_key == key)
    rows = list(session.scalars(q.order_by(Approval.created_at)))
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise NotFound("there is no pending approval request in this conversation")
    raise Ambiguous("more than one approval is pending; say which one",
                    candidates=[{"ref": ref("approval", r.id), "summary": r.summary} for r in rows])


def _mark_decided(session: Session, a: Approval, actor: Actor, status: str, note: str | None = None) -> None:
    a.status = status
    a.decided_by_id = actor.user_id
    a.decided_via = actor.via
    a.decided_at = timeutil.now()
    a.decision_note = note
    audit.record(session, actor, f"approval.{status}", "approval", a.id, payload_hash=a.payload_hash, note=note)


def decide(session: Session, actor: Actor, approval_id: int, decision: str, *, presented_hash: str,
           note: str | None = None) -> dict[str, Any]:
    """Owner decision over a verified channel. ``presented_hash`` must be the hash shown to them."""
    require_decider(actor)
    if decision not in ("approve", "reject"):
        raise ValidationFailed("decision must be approve or reject")
    # Lock the row so concurrent approve/approve or approve/execute cannot race.
    a = session.scalar(select(Approval).where(Approval.id == approval_id).with_for_update())
    if a is None:
        raise NotFound(f"{ref('approval', approval_id)} not found")
    _expire_if_due(a)
    if a.status != "pending":
        raise InvalidTransition(f"{ref('approval', a.id)} is {a.status}; it cannot be decided again")
    if presented_hash != a.payload_hash:
        raise Conflict("the approval you were shown does not match the current request; reload it")
    if decision == "reject":
        _mark_decided(session, a, actor, "rejected", note)
        return {"approval": approval_view(a)}
    spec = _spec(a.action_type)
    try:
        current = _build(session, spec, a.target_id, a.payload.get("_extra", {}))
    except (InvalidTransition, ValidationFailed, NotFound) as exc:
        a.status = "invalidated"
        a.decision_note = f"target no longer valid: {exc}"
        audit.record(session, actor, "approval.invalidated", "approval", a.id, reason=str(exc))
        raise Conflict(f"cannot approve: {exc}") from exc
    if current["hash"] != a.payload_hash:
        a.status = "invalidated"
        a.decision_note = "target changed after the request was made"
        audit.record(session, actor, "approval.invalidated", "approval", a.id, reason="payload changed")
        raise Conflict("the record changed after this approval was requested; a new approval is needed")
    _mark_decided(session, a, actor, "approved", note)
    result = _execute(session, a, actor)
    return {"approval": approval_view(a), "result": result}


def _execute(session: Session, a: Approval, actor: Actor) -> dict[str, Any]:
    if a.expires_at <= timeutil.now():
        a.status = "expired"
        raise Blocked("approval expired before execution")
    flipped = session.execute(
        update(Approval).where(Approval.id == a.id, Approval.status == "approved").values(status="executing")
    ).rowcount
    if flipped != 1:
        raise InvalidTransition("approval already used or not approved")
    session.refresh(a)
    spec = _spec(a.action_type)
    current = _build(session, spec, a.target_id, a.payload.get("_extra", {}))
    if current["hash"] != a.payload_hash:
        a.status = "invalidated"
        a.decision_note = "target changed before execution"
        raise Conflict("record changed before execution; approval invalidated")
    try:
        result = spec.execute(session, a, actor)
    except Exception as exc:
        a.status = "failed"
        a.execution_result = {"error": str(exc)[:500]}
        audit.record(session, actor, "approval.failed", "approval", a.id, error=str(exc)[:500])
        raise
    a.status = "executed"
    a.executed_at = timeutil.now()
    a.execution_result = jsonable(result)
    audit.record(session, actor, "approval.executed", "approval", a.id, result=result)
    return result


def cancel(session: Session, actor: Actor, approval_id: int, reason: str) -> dict[str, Any]:
    require(actor, "request:approval")
    a = get(session, actor, approval_id)
    if a.status != "pending":
        raise InvalidTransition(f"{ref('approval', a.id)} is {a.status}")
    a.status = "cancelled"
    a.decision_note = reason
    audit.record(session, actor, "approval.cancel", "approval", a.id, reason=reason)
    return approval_view(a)


def amount_or_none(v: Any) -> Decimal | None:
    return None if v is None else D(v)
