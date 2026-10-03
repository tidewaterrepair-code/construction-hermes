"""Owner priorities: "What needs me today?" in the owner's priority order.

1. collect money already earned  2. protect active jobs  3. respond to qualified leads
4. prevent missed commitments    5. improve systems
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import timeutil
from ..authz import Actor, require
from ..models import (Appointment, Approval, Inspection, Invoice, Job, Lead, Notification, OutboxJob, Payment,
                      Proposal, Task)
from ..money import fmt
from ..refs import ref
from . import billing, costing, integrations, procurement, schedule, settings


def _item(priority: int, section: str, text: str, link: str | None = None, ref_: str | None = None) -> dict[str, Any]:
    return {"priority": priority, "section": section, "text": text, "link": link, "ref": ref_}


def today(session: Session, actor: Actor, include_synthetic: bool = False, max_items: int = 10) -> dict[str, Any]:
    require(actor, "read:financial")
    items: list[dict[str, Any]] = []
    syn = (lambda q, m: q) if include_synthetic else (lambda q, m: q.where(m.is_synthetic.is_(False)))
    now = timeutil.now()
    t_local = timeutil.today_local()

    # 1. Money
    for r in billing.receivables(session, actor, include_synthetic):
        if r["days_overdue"] > 0:
            items.append(_item(1, "money", f"Invoice {r['number']} {fmt(Decimal(r['open_amount']))} is {r['days_overdue']}d overdue",
                               f"/invoices/{r['invoice'].split('-')[1]}", r["invoice"]))
    for inv in session.scalars(syn(select(Invoice).where(Invoice.status == "approved"), Invoice)):
        items.append(_item(1, "money", f"Invoice {inv.number} approved but not issued", f"/invoices/{inv.id}", ref("invoice", inv.id)))
    n_rep = session.scalar(syn(select(func.count()).select_from(Payment).where(Payment.status == "reported_unverified"), Payment))
    if n_rep:
        items.append(_item(1, "money", f"{n_rep} reported payment(s) need verification against deposits", "/money"))
    for ex in costing.exceptions(session, actor, include_synthetic=include_synthetic):
        if ex["type"] == "unbilled_change_order":
            items.append(_item(1, "money", ex["message"], f"/jobs/{ex['job'].split('-')[1]}", ex["job"]))

    # Approvals waiting (any domain) rank right after money.
    for a in session.scalars(syn(select(Approval).where(Approval.status == "pending", Approval.expires_at > now)
                                 .order_by(Approval.created_at), Approval)):
        items.append(_item(1, "approvals", f"Approve? {a.summary}", f"/approvals/{a.id}", ref("approval", a.id)))

    # 2. Active jobs
    sched = schedule.detect_conflicts(session, actor, days=7)
    for c in sched["conflicts"]:
        items.append(_item(2, "jobs", c["message"], None, (c.get("jobs") or [None])[0]))
    for ex in costing.exceptions(session, actor, include_synthetic=include_synthetic):
        if ex["type"] in ("margin_erosion", "forecast_overrun"):
            items.append(_item(2, "jobs", ex["message"], f"/jobs/{ex['job'].split('-')[1]}", ex["job"]))
    start = dt.datetime.combine(t_local, dt.time.min, tzinfo=timeutil.tz())
    end = start + dt.timedelta(days=1)
    for i in session.scalars(syn(select(Inspection).where(Inspection.scheduled_for >= start, Inspection.scheduled_for < end), Inspection)):
        items.append(_item(2, "jobs", f"Inspection today: {i.inspection_type} ({ref('job', i.job_id)}) {timeutil.fmt_local(i.scheduled_for)}",
                           f"/jobs/{i.job_id}", ref("inspection", i.id)))
    n_tasks = session.scalar(syn(select(func.count()).select_from(Task).where(Task.starts_at >= start, Task.starts_at < end), Task))

    # 3. Leads
    overdue = session.scalars(syn(select(Lead).where(Lead.status.not_in(("won", "lost")), Lead.next_action_due < now)
                                  .order_by(Lead.next_action_due), Lead))
    for lead in overdue:
        items.append(_item(3, "leads", f"Follow up {lead.contact.name}: {lead.next_action or 'next step'}", f"/leads/{lead.id}",
                           ref("lead", lead.id)))
    n_review = session.scalar(syn(select(func.count()).select_from(Lead).where(Lead.needs_review.is_(True),
                                                                               Lead.status.not_in(("won", "lost"))), Lead))
    if n_review:
        items.append(_item(3, "leads", f"{n_review} lead(s) need a quick check (uncertain or possible duplicate)", "/leads"))

    # 4. Commitments
    for a in session.scalars(syn(select(Appointment).where(Appointment.status == "tentative", Appointment.starts_at >= now,
                                                           Appointment.starts_at < now + dt.timedelta(days=2)), Appointment)):
        items.append(_item(4, "commitments", f"Tentative site visit {timeutil.fmt_local(a.starts_at)} is not confirmed",
                           f"/leads/{a.lead_id}" if a.lead_id else None, ref("appointment", a.id)))
    for p in session.scalars(syn(select(Proposal).where(Proposal.status.in_(("approved", "issued"))), Proposal)):
        vu = p.content.get("valid_until")
        if vu and t_local <= dt.date.fromisoformat(vu) <= t_local + dt.timedelta(days=5):
            items.append(_item(4, "commitments", f"Proposal {ref('proposal', p.id)} expires {vu}", f"/proposals/{p.id}", ref("proposal", p.id)))
    for v in procurement.compliance(session, actor):
        if any("expire" in f for f in v["flags"]):
            items.append(_item(4, "commitments", f"{v['name']}: " + ", ".join(f for f in v["flags"] if "expire" in f), "/vendors", v["vendor"]))

    # 5. Systems
    ks = settings.kill_switch(session)
    if ks.get("engaged"):
        items.append(_item(5, "system", f"Kill switch ON: {ks.get('reason')}", "/settings"))
    for j in session.scalars(select(OutboxJob).where(OutboxJob.status.in_(("unknown", "dead", "failed", "blocked")))
                             .order_by(OutboxJob.id.desc()).limit(5)):
        items.append(_item(5, "system", f"Delivery {j.status}: {j.kind} ({j.last_error or ''})"[:160], "/system", ref("outbox", j.id)))
    for row in integrations.status_list(session):
        if row["status"] == "degraded":
            items.append(_item(5, "system", f"{row['name']} degraded: {row['last_error']}", "/system"))
    unread = session.scalar(select(func.count()).select_from(Notification).where(Notification.read_at.is_(None)))

    items.sort(key=lambda x: x["priority"])
    counts = {}
    for it in items:
        counts[it["section"]] = counts.get(it["section"], 0) + 1
    active_jobs = session.scalar(syn(select(func.count()).select_from(Job).where(Job.status == "active"), Job))
    return {"date": t_local.isoformat(), "mode": settings.mode(session), "top": items[:max_items],
            "more": max(0, len(items) - max_items), "counts": counts, "tasks_today": n_tasks, "active_jobs": active_jobs,
            "unread_notifications": unread, "nothing_urgent": not items}


def render_text(d: dict[str, Any]) -> str:
    """One phone screen."""
    if d["nothing_urgent"]:
        return f"{d['date']}: nothing needs you right now. {d['active_jobs']} active job(s), {d['tasks_today']} task(s) today."
    lines = [f"{d['date']} · {d['mode']} — needs you:"]
    for i, it in enumerate(d["top"], 1):
        lines.append(f"{i}. {it['text']}" + (f" [{it['ref']}]" if it.get("ref") else ""))
    if d["more"]:
        lines.append(f"+{d['more']} more on the dashboard")
    return "\n".join(lines)
