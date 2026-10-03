"""Jobs created from accepted proposals; job lookup and access-scoped views."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import timeutil
from ..authz import Actor, job_ids_visible, require, require_job_access
from ..errors import Ambiguous, InvalidTransition, NotFound, ValidationFailed
from ..models import (Contact, Estimate, EstimateRevision, Job, JobAssignment, JobBudgetLine, Lead, Proposal,
                      Site, User)
from ..money import D, pct
from ..refs import ref
from . import audit, settings
from . import settings as _settings

JOB_TRANSITIONS = {
    "planned": {"active", "on_hold", "cancelled"},
    "active": {"on_hold", "complete", "cancelled"},
    "on_hold": {"active", "cancelled"},
    "complete": {"closed", "active"},
    "closed": set(),
    "cancelled": set(),
}


def create_from_proposal(session: Session, actor: Actor, proposal_id: int) -> dict[str, Any]:
    """Idempotent: one job per accepted proposal (unique source_proposal_id)."""
    require(actor, "write:job")
    existing = session.scalar(select(Job).where(Job.source_proposal_id == proposal_id))
    if existing is not None:
        return job_view(session, actor, existing)
    p = session.get(Proposal, proposal_id)
    if p is None or p.status != "accepted":
        raise InvalidTransition("a job can only be created from an accepted proposal")
    est = session.get(Estimate, p.estimate_id)
    rev = session.get(EstimateRevision, p.estimate_revision_id)
    lead = session.get(Lead, est.lead_id) if est.lead_id else None
    if lead is None:
        raise ValidationFailed("estimate has no lead/customer")
    retainage = settings.get(session, "retainage_default_pct")
    job = Job(name=est.title, customer_id=lead.contact_id, site_id=lead.site_id, lead_id=lead.id, status="planned",
              source_proposal_id=p.id, source_estimate_revision_id=rev.id, contract_value=p.total,
              retainage_pct=pct(retainage) if retainage is not None else Decimal("0"), is_synthetic=p.is_synthetic,
              created_by_id=actor.user_id)
    session.add(job)
    session.flush()
    est.job_id = job.id
    totals = rev.totals or {}
    for code, amount in (totals.get("direct_by_cost_code") or {}).items():
        session.add(JobBudgetLine(job_id=job.id, cost_code=code, description=code, estimated_cost=D(amount),
                                  source=f"estimate_revision:{rev.id}"))
    for code, key in (("contingency", "contingency"), ("material-tax", "material_tax")):
        if totals.get(key) and D(totals[key]) > 0:
            session.add(JobBudgetLine(job_id=job.id, cost_code=code, description=key.replace("_", " "),
                                      estimated_cost=D(totals[key]), source=f"estimate_revision:{rev.id}"))
    session.flush()
    audit.record(session, actor, "job.create", "job", job.id, proposal=p.id, estimate_revision=rev.id,
                 contract_value=str(p.total))
    return job_view(session, actor, job)


def get(session: Session, actor: Actor, job_id: int) -> Job:
    require_job_access(session, actor, job_id)
    j = session.get(Job, job_id)
    if j is None:
        raise NotFound(f"{ref('job', job_id)} not found")
    return j


def resolve(session: Session, actor: Actor, name_or_ref: str) -> Job:
    """Resolve 'JOB-12', a job name fragment, customer name, or street. Ambiguity is an error."""
    s = name_or_ref.strip()
    if s.upper().startswith("JOB-") or s.isdigit():
        from ..refs import parse

        return get(session, actor, parse(s, "job"))
    visible = job_ids_visible(session, actor)
    like = f"%{s.lower()}%"
    q = select(Job).join(Contact, Contact.id == Job.customer_id).outerjoin(Site, Site.id == Job.site_id).where(
        or_(func.lower(Job.name).like(like), func.lower(Contact.name).like(like), func.lower(Site.address_line).like(like)),
        Job.status.not_in(("closed", "cancelled")))
    if visible is not None:
        q = q.where(Job.id.in_(visible or {-1}))
    rows = list(session.scalars(q.limit(10)))
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise NotFound(f"no open job matches '{s}'")
    raise Ambiguous(f"'{s}' matches {len(rows)} jobs; which one?",
                    candidates=[{"ref": ref("job", j.id), "name": j.name,
                                 "site": j.site.address_line if j.site else None} for j in rows])


def transition(session: Session, actor: Actor, job_id: int, to_status: str, reason: str | None = None) -> dict[str, Any]:
    require(actor, "write:job")
    j = get(session, actor, job_id)
    if to_status not in JOB_TRANSITIONS[j.status]:
        raise InvalidTransition(f"cannot move job from {j.status} to {to_status}",
                                allowed=sorted(JOB_TRANSITIONS[j.status]))
    old = j.status
    j.status = to_status
    audit.record(session, actor, "job.transition", "job", j.id, frm=old, to=to_status, reason=reason)
    return job_view(session, actor, j)


def assign_user(session: Session, actor: Actor, job_id: int, user_id: int, role: str) -> dict[str, Any]:
    require(actor, "admin:users")
    get(session, actor, job_id)
    u = session.get(User, user_id)
    if u is None or u.role not in ("foreman", "crew", "viewer", "office"):
        raise ValidationFailed("assign foreman/crew/viewer/office users only")
    if session.scalar(select(JobAssignment).where(JobAssignment.job_id == job_id, JobAssignment.user_id == user_id)) is None:
        session.add(JobAssignment(job_id=job_id, user_id=user_id, role=role))
    audit.record(session, actor, "job.assign_user", "job", job_id, user=user_id, role=role)
    return {"job": ref("job", job_id), "user": ref("user", user_id), "role": role}


def job_view(session: Session, actor: Actor, j: Job) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ref": ref("job", j.id), "id": j.id, "name": j.name, "status": j.status,
        "customer": j.customer.name if j.customer else None,
        "site": j.site.address_line if j.site else None,
        "planned_start": timeutil.iso(j.planned_start), "planned_finish": timeutil.iso(j.planned_finish),
        "lead": ref("lead", j.lead_id), "synthetic": j.is_synthetic,
    }
    if actor.sees_financials:
        out.update({
            "contract_value": str(j.contract_value), "retainage_pct": str(j.retainage_pct),
            "source_proposal": ref("proposal", j.source_proposal_id),
            "source_estimate_revision_id": j.source_estimate_revision_id,
        })
    return out


def list_jobs(session: Session, actor: Actor, include_closed: bool = False, include_synthetic: bool = False) -> list[dict[str, Any]]:
    visible = job_ids_visible(session, actor)
    q = select(Job).order_by(Job.id.desc()).limit(200)
    if visible is not None:
        q = q.where(Job.id.in_(visible or {-1}))
    if not include_closed:
        q = q.where(Job.status.not_in(("closed", "cancelled")))
    if not _settings.include_synthetic(session, include_synthetic):
        q = q.where(Job.is_synthetic.is_(False))
    return [job_view(session, actor, j) for j in session.scalars(q)]


def set_dates(session: Session, actor: Actor, job_id: int, planned_start: str | None, planned_finish: str | None) -> dict[str, Any]:
    import datetime as dt

    require(actor, "write:job")
    j = get(session, actor, job_id)
    if planned_start:
        j.planned_start = dt.date.fromisoformat(planned_start)
    if planned_finish:
        j.planned_finish = dt.date.fromisoformat(planned_finish)
    if j.planned_start and j.planned_finish and j.planned_finish < j.planned_start:
        raise ValidationFailed("finish before start")
    audit.record(session, actor, "job.dates", "job", j.id, start=planned_start, finish=planned_finish)
    return job_view(session, actor, j)
