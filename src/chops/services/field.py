"""Daily logs and change orders."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import timeutil
from ..authz import Actor, require, require_job_access
from ..errors import InvalidTransition, NotFound, ValidationFailed
from ..hashing import content_hash, jsonable
from ..models import ChangeOrder, DailyLog, Job, JobBudgetLine
from ..money import D, q2
from ..refs import ref
from . import audit

# ------------------------------------------------------------------ daily logs


def log_view(d: DailyLog) -> dict[str, Any]:
    return {"ref": ref("daily_log", d.id), "id": d.id, "job": ref("job", d.job_id), "date": d.log_date.isoformat(),
            "status": d.status, "original_note": d.original_note, "work_completed": d.work_completed,
            "labor": d.labor, "materials": d.materials, "delays": d.delays, "issues": d.issues,
            "tomorrow_plan": d.tomorrow_plan, "photos": [ref("document", i) for i in d.photo_document_ids or []],
            "extraction": d.extraction}


def draft_daily_log(session: Session, actor: Actor, job_id: int, *, original_note: str, log_date: str | None = None,
                    work_completed: str | None = None, labor: list[dict[str, Any]] | None = None,
                    materials: list[dict[str, Any]] | None = None, delays: str | None = None,
                    issues: str | None = None, tomorrow_plan: str | None = None,
                    photo_document_ids: list[int] | None = None, extraction_method: str = "manual",
                    uncertain_fields: list[str] | None = None) -> dict[str, Any]:
    """The original note is preserved verbatim; structured fields carry extraction provenance."""
    if not (actor.can("write:field") or actor.can("write:job")):
        raise ValidationFailed("not allowed to write daily logs")
    require_job_access(session, actor, job_id, write=True)
    if not original_note or not original_note.strip():
        raise ValidationFailed("the original field note is required")
    job = session.get(Job, job_id)
    d = DailyLog(job_id=job_id, log_date=dt.date.fromisoformat(log_date) if log_date else timeutil.today_local(),
                 status="draft", original_note=original_note, work_completed=work_completed, labor=jsonable(labor or []),
                 materials=jsonable(materials or []), delays=delays, issues=issues, tomorrow_plan=tomorrow_plan,
                 photo_document_ids=photo_document_ids or [], is_synthetic=job.is_synthetic, created_by_id=actor.user_id,
                 extraction={"method": extraction_method, "by": actor.display_name or actor.role,
                             "at": timeutil.now().isoformat(), "uncertain_fields": uncertain_fields or []})
    session.add(d)
    session.flush()
    audit.record(session, actor, "daily_log.draft", "daily_log", d.id, job=job_id)
    return log_view(d)


def finalize_daily_log(session: Session, actor: Actor, log_id: int) -> dict[str, Any]:
    d = session.get(DailyLog, log_id)
    if d is None:
        raise NotFound("daily log not found")
    require_job_access(session, actor, d.job_id, write=True)
    if actor.role not in ("owner", "office", "foreman"):
        raise ValidationFailed("only owner/office/foreman can finalize a daily log")
    d.status = "final"
    audit.record(session, actor, "daily_log.final", "daily_log", d.id)
    return log_view(d)


def list_daily_logs(session: Session, actor: Actor, job_id: int, limit: int = 14) -> list[dict[str, Any]]:
    require_job_access(session, actor, job_id)
    return [log_view(d) for d in session.scalars(select(DailyLog).where(DailyLog.job_id == job_id)
                                                 .order_by(DailyLog.log_date.desc(), DailyLog.id.desc()).limit(limit))]


# ------------------------------------------------------------------ change orders


def _co_hash(co: ChangeOrder) -> str:
    return content_hash({"job": co.job_id, "number": co.number, "revision": co.revision_no, "title": co.title,
                         "scope": co.scope, "price": co.price, "schedule_impact_days": co.schedule_impact_days,
                         "evidence": co.evidence_document_ids})


def ensure_hash(co: ChangeOrder) -> None:
    from ..errors import Conflict

    if co.content_hash != _co_hash(co):
        raise Conflict("change order content changed since it was hashed")


def co_view(co: ChangeOrder, actor: Actor) -> dict[str, Any]:
    out = {"ref": ref("change_order", co.id), "id": co.id, "job": ref("job", co.job_id), "number": co.number,
           "revision": co.revision_no, "title": co.title, "scope": co.scope, "price": str(co.price),
           "schedule_impact_days": co.schedule_impact_days, "status": co.status,
           "evidence": [ref("document", i) for i in co.evidence_document_ids or []],
           "customer_approval_evidence": co.customer_approval_evidence,
           "counts_as_revenue": co.status == "customer_approved", "content_hash": co.content_hash,
           "supersedes": ref("change_order", co.supersedes_id)}
    if actor.sees_financials:
        out["estimated_cost"] = None if co.estimated_cost is None else str(co.estimated_cost)
    return out


def create_change_order(session: Session, actor: Actor, job_id: int, *, title: str, scope: str, price: Any,
                        estimated_cost: Any = None, schedule_impact_days: int = 0, cost_code: str | None = None,
                        evidence_document_ids: list[int] | None = None) -> dict[str, Any]:
    require(actor, "write:job")
    require_job_access(session, actor, job_id, write=True)
    job = session.get(Job, job_id)
    number = (session.scalar(select(func.max(ChangeOrder.number)).where(ChangeOrder.job_id == job_id)) or 0) + 1
    co = ChangeOrder(job_id=job_id, number=number, revision_no=1, title=title, scope=scope, price=q2(D(price)),
                     estimated_cost=None if estimated_cost in (None, "") else q2(D(estimated_cost)), cost_code=cost_code,
                     schedule_impact_days=int(schedule_impact_days), status="draft",
                     evidence_document_ids=evidence_document_ids or [], content_hash="", is_synthetic=job.is_synthetic,
                     created_by_id=actor.user_id)
    session.add(co)
    session.flush()
    co.content_hash = _co_hash(co)
    audit.record(session, actor, "change_order.create", "change_order", co.id, job=job_id, price=str(co.price))
    return co_view(co, actor)


def revise_change_order(session: Session, actor: Actor, co_id: int, **fields: Any) -> dict[str, Any]:
    """Drafts change in place (invalidating any pending approval via the hash). Issued change
    orders are never edited: a new revision supersedes them."""
    require(actor, "write:job")
    co = session.get(ChangeOrder, co_id)
    if co is None:
        raise NotFound("change order not found")
    require_job_access(session, actor, co.job_id, write=True)
    if co.status in ("customer_approved",):
        raise InvalidTransition("customer-approved change orders cannot be revised; create a new change order")
    target = co
    if co.status in ("approved_to_issue", "issued", "pending_approval"):
        target = ChangeOrder(job_id=co.job_id, number=co.number, revision_no=co.revision_no + 1, title=co.title,
                             scope=co.scope, price=co.price, estimated_cost=co.estimated_cost, cost_code=co.cost_code,
                             schedule_impact_days=co.schedule_impact_days, status="draft",
                             evidence_document_ids=list(co.evidence_document_ids or []), content_hash="",
                             supersedes_id=co.id, is_synthetic=co.is_synthetic, created_by_id=actor.user_id)
        co.status = "void"
        session.add(target)
    for k in ("title", "scope", "cost_code"):
        if fields.get(k) is not None:
            setattr(target, k, fields[k])
    if fields.get("price") not in (None, ""):
        target.price = q2(D(fields["price"]))
    if fields.get("estimated_cost") not in (None, ""):
        target.estimated_cost = q2(D(fields["estimated_cost"]))
    if fields.get("schedule_impact_days") is not None:
        target.schedule_impact_days = int(fields["schedule_impact_days"])
    if target.status == "pending_approval":
        target.status = "draft"
    session.flush()
    target.content_hash = _co_hash(target)
    audit.record(session, actor, "change_order.revise", "change_order", target.id, fields={k: str(v) for k, v in fields.items()})
    return co_view(target, actor)


def request_change_order_issue(session: Session, actor: Actor, co_id: int) -> dict[str, Any]:
    from . import approvals

    co = session.get(ChangeOrder, co_id)
    if co is None:
        raise NotFound("change order not found")
    require_job_access(session, actor, co.job_id)
    res = approvals.request(session, actor, "change_order.issue", co.id, synthetic=co.is_synthetic)
    co.status = "pending_approval"
    return res


def record_customer_approval(session: Session, actor: Actor, co_id: int, evidence: str) -> dict[str, Any]:
    require(actor, "payment:verify")
    co = session.get(ChangeOrder, co_id)
    if co is None:
        raise NotFound("change order not found")
    if co.status not in ("approved_to_issue", "issued"):
        raise InvalidTransition(f"change order is {co.status}; the owner must approve issuing it first")
    if not evidence or len(evidence.strip()) < 5:
        raise ValidationFailed("customer approval evidence required (signature, email)")
    co.status = "customer_approved"
    co.customer_approval_evidence = evidence.strip()[:500]
    co.customer_approved_at = timeutil.now()
    if co.estimated_cost:
        session.add(JobBudgetLine(job_id=co.job_id, cost_code=co.cost_code or f"co-{co.number}",
                                  description=f"Change order #{co.number}: {co.title}", estimated_cost=co.estimated_cost,
                                  source=f"change_order:{co.id}"))
    audit.record(session, actor, "change_order.customer_approved", "change_order", co.id, evidence=evidence)
    return co_view(co, actor)


def list_change_orders(session: Session, actor: Actor, job_id: int) -> list[dict[str, Any]]:
    require_job_access(session, actor, job_id)
    return [co_view(c, actor) for c in session.scalars(select(ChangeOrder).where(ChangeOrder.job_id == job_id)
                                                       .order_by(ChangeOrder.number, ChangeOrder.revision_no))]
