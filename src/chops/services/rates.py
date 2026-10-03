"""Rate library with provenance. No rate is ever invented: missing stays missing."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import timeutil
from ..authz import Actor, require
from ..errors import Forbidden, NotFound, ValidationFailed
from ..models import RATE_CATEGORIES, RATE_STATUSES, Assembly, Rate
from ..money import D
from ..refs import ref
from . import audit
from . import settings as _settings

TEMPLATES = Path(__file__).resolve().parent.parent / "assemblies" / "templates.json"


def rate_view(r: Rate) -> dict[str, Any]:
    status = effective_status(r)
    return {"ref": ref("rate", r.id), "code": r.code, "description": r.description, "category": r.category,
            "unit": r.unit, "unit_cost": str(r.unit_cost), "status": status, "source": r.source,
            "source_date": timeutil.iso(r.source_date), "geography": r.geography,
            "valid_until": timeutil.iso(r.valid_until), "synthetic": r.is_synthetic}


def effective_status(r: Rate) -> str:
    if r.status != "expired" and r.valid_until is not None and r.valid_until < timeutil.today_local():
        return "expired"
    return r.status


def create(session: Session, actor: Actor, *, code: str, description: str, category: str, unit: str,
           unit_cost: Any, source: str, source_date: str | dt.date, status: str = "provisional",
           geography: str | None = None, valid_until: str | dt.date | None = None,
           vendor_contact_id: int | None = None, synthetic: bool = False) -> dict[str, Any]:
    require(actor, "write:estimate")
    if category not in RATE_CATEGORIES:
        raise ValidationFailed(f"category must be one of {RATE_CATEGORIES}")
    if status not in RATE_STATUSES:
        raise ValidationFailed(f"status must be one of {RATE_STATUSES}")
    if status in ("verified", "owner_entered") and not actor.can("rates:verify"):
        raise Forbidden("only the owner/office can mark a rate verified or owner-entered; agent rates are provisional")
    if not source or len(source.strip()) < 3:
        raise ValidationFailed("a rate needs a source (supplier quote, invoice, owner, etc.)")
    cost = D(unit_cost)
    if cost < 0:
        raise ValidationFailed("unit cost cannot be negative")
    from . import settings as _settings

    synthetic = synthetic or _settings.environment(session) != "prod"
    sd = source_date if isinstance(source_date, dt.date) else dt.date.fromisoformat(source_date)
    vu = valid_until if (valid_until is None or isinstance(valid_until, dt.date)) else dt.date.fromisoformat(valid_until)
    prev = session.scalar(select(Rate).where(Rate.code == code, Rate.active.is_(True), Rate.is_synthetic.is_(synthetic)))
    r = Rate(code=code, description=description, category=category, unit=unit, unit_cost=cost, status=status,
             source=source.strip(), source_date=sd, geography=geography, valid_until=vu,
             vendor_contact_id=vendor_contact_id, supersedes_id=prev.id if prev else None, is_synthetic=synthetic,
             created_by_id=actor.user_id)
    if prev is not None:
        prev.active = False
    session.add(r)
    session.flush()
    audit.record(session, actor, "rate.create", "rate", r.id, code=code, unit_cost=str(cost), status=status,
                 supersedes=prev.id if prev else None)
    return rate_view(r)


def set_status(session: Session, actor: Actor, rate_id: int, status: str, note: str | None = None) -> dict[str, Any]:
    require(actor, "rates:verify")
    r = session.get(Rate, rate_id)
    if r is None:
        raise NotFound("rate not found")
    old = r.status
    r.status = status
    audit.record(session, actor, "rate.status", "rate", r.id, old=old, new=status, note=note)
    return rate_view(r)


def find_active(session: Session, code: str, synthetic: bool = False) -> Rate | None:
    return session.scalar(select(Rate).where(Rate.code == code, Rate.active.is_(True), Rate.is_synthetic.is_(synthetic)))


def list_rates(session: Session, actor: Actor, category: str | None = None, include_synthetic: bool = False) -> list[dict[str, Any]]:
    require(actor, "read:financial")
    q = select(Rate).where(Rate.active.is_(True)).order_by(Rate.code)
    if category:
        q = q.where(Rate.category == category)
    if not _settings.include_synthetic(session, include_synthetic):
        q = q.where(Rate.is_synthetic.is_(False))
    return [rate_view(r) for r in session.scalars(q)]


# ------------------------------------------------------------------ assemblies


def load_example_assemblies(session: Session) -> int:
    """Install/update the bundled example templates (marked is_example)."""
    data = json.loads(TEMPLATES.read_text())
    n = 0
    for t in data:
        a = session.scalar(select(Assembly).where(Assembly.code == t["code"]))
        if a is None:
            a = Assembly(code=t["code"])
            session.add(a)
        elif not a.is_example:
            continue  # owner-edited copy wins
        a.name, a.trade, a.description = t["name"], t["trade"], t["description"]
        a.parameters, a.components, a.notes, a.is_example = t["parameters"], t["components"], t.get("notes"), True
        n += 1
    session.flush()
    return n


def list_assemblies(session: Session, actor: Actor) -> list[dict[str, Any]]:
    require(actor, "read:all")
    return [{"ref": ref("assembly", a.id), "code": a.code, "name": a.name, "trade": a.trade, "is_example": a.is_example,
             "description": a.description, "parameters": a.parameters, "notes": a.notes}
            for a in session.scalars(select(Assembly).order_by(Assembly.trade, Assembly.code))]


def unit_cost_or_none(value: Any) -> Decimal | None:
    return None if value in (None, "") else D(value)
