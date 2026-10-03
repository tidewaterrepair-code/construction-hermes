"""Business configuration, operating mode, and the kill switch.

Unknown company facts (name, license, insurance, terms, tax treatment) are simply absent;
callers must treat absence as "not configured" and say so.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..authz import Actor, require
from ..errors import Blocked, ValidationFailed
from ..hashing import jsonable
from ..models import OrgSetting
from . import audit

MODES = ("BUILD", "SHADOW", "LIVE")

# Keys the owner may set, with a short description (shown in the configuration screen).
KNOWN_KEYS: dict[str, str] = {
    "company_name": "Legal/display company name for proposals and invoices",
    "company_contact": "Phone/email/address block printed on documents",
    "license_info": "Contractor license class/number as issued (printed only if set)",
    "insurance_info": "Insurance carrier/policy text (printed only if set)",
    "service_area": "Service area and confirmed jurisdictions {text, confirmed}",
    "commercial_terms": "Owner-approved proposal terms {payment_milestones:[{key,label,pct}], validity_days, terms_text, warranty_text}",
    "estimating_defaults": "{overhead_pct, profit_method, profit_pct, contingency_pct, labor_burden_pct, tax_mode, material_tax_pct, sales_tax_pct}",
    "rough_range": "{low_pct, high_pct} band for rough ranges",
    "budget": "{monthly_usd} owner budget for AI/services; unset keeps paid routines disabled",
    "local_usage_cap_usd": "Local cap on attributable model usage per month (not a provider billing guarantee)",
    "work_calendar": "{workdays:[0-6], start:'07:00', end:'17:00', holidays:['YYYY-MM-DD']}",
    "standing_policies": "List of narrowly defined pre-approvals [{action_type, max_amount, recipient_kind, expires}]",
    "approval_ttl_hours": "Hours an approval request stays valid",
    "invoice_terms_days": "Net days for invoice due date",
    "retainage_default_pct": "Default retainage fraction for new jobs (0 if not used)",
}


def get(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(OrgSetting, key)
    return default if row is None else row.value


def get_all(session: Session) -> dict[str, Any]:
    return {r.key: r.value for r in session.scalars(select(OrgSetting))}


def put(session: Session, actor: Actor, key: str, value: Any) -> None:
    require(actor, "admin:settings")
    if key not in KNOWN_KEYS and key not in ("mode", "environment"):
        raise ValidationFailed(f"unknown setting {key}")
    _write(session, actor, key, value)


def _write(session: Session, actor: Actor, key: str, value: Any) -> None:
    row = session.get(OrgSetting, key)
    old = None if row is None else row.value
    value = jsonable(value)
    if row is None:
        session.add(OrgSetting(key=key, value=value, updated_by_id=actor.user_id))
    else:
        row.value = value
        row.updated_by_id = actor.user_id
    audit.record(session, actor, "settings.update", "setting", None, key=key, old=old, new=value)


# ------------------------------------------------------------------ mode


def mode(session: Session) -> str:
    return get(session, "mode", "BUILD")


def set_mode(session: Session, actor: Actor, new_mode: str, *, confirm_live: bool = False) -> None:
    require(actor, "admin:settings")
    if new_mode not in MODES:
        raise ValidationFailed(f"mode must be one of {MODES}")
    if new_mode == "LIVE" and not confirm_live:
        raise ValidationFailed("LIVE mode requires explicit confirmation")
    if new_mode == "LIVE" and environment(session) == "demo":
        raise Blocked("demo/restore-test databases cannot enter LIVE mode")
    _write(session, actor, "mode", new_mode)


def environment(session: Session) -> str:
    return get(session, "environment", "prod")


def include_synthetic(session: Session, requested: bool = False) -> bool:
    """Production views hide synthetic records; demo/restore-test databases show them (they hold nothing else)."""
    return requested or environment(session) != "prod"


# ------------------------------------------------------------------ kill switch


def kill_switch(session: Session) -> dict[str, Any]:
    return get(session, "kill_switch", {"engaged": False})


def kill_engaged(session: Session) -> bool:
    return bool(kill_switch(session).get("engaged"))


def engage_kill_switch(session: Session, actor: Actor, reason: str) -> None:
    require(actor, "kill:engage")
    _write(session, actor, "kill_switch", {
        "engaged": True, "reason": reason[:300], "by": actor.display_name or actor.role,
        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
    })


def release_kill_switch(session: Session, actor: Actor, reason: str) -> None:
    require(actor, "kill:release")
    _write(session, actor, "kill_switch", {
        "engaged": False, "reason": reason[:300], "by": actor.display_name or actor.role,
        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
    })


def assert_external_allowed(session: Session) -> None:
    """Rechecked immediately before any external dispatch."""
    if kill_engaged(session):
        raise Blocked("kill switch engaged: external actions are blocked")


def company_display(session: Session) -> str:
    return get(session, "company_name") or "[Company name not configured]"
