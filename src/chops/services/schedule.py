"""Tasks, dependencies, crew assignments, conflict and prerequisite detection.

Weather is advisory only: with no weather source connected, weather-sensitive work is
listed with "check forecast" and nothing is moved automatically. Reschedules are proposed
with their downstream impact; committing them is an owner/office action, and customer
commitments are never changed silently.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import timeutil
from ..authz import Actor, job_ids_visible, require, require_job_access
from ..errors import Conflict, NotFound, ValidationFailed
from ..models import (Appointment, CrewMember, Inspection, Integration, Job, Permit, PurchaseOrder, Task, TaskAssignment,
                      TaskDependency)
from ..money import D
from ..refs import ref
from . import audit, integrations, settings


def task_view(t: Task, crew: list[str] | None = None) -> dict[str, Any]:
    return {"ref": ref("task", t.id), "id": t.id, "job": ref("job", t.job_id), "phase": t.phase, "name": t.name,
            "status": t.status, "starts_at": timeutil.iso(t.starts_at), "ends_at": timeutil.iso(t.ends_at),
            "starts_local": timeutil.fmt_local(t.starts_at), "ends_local": timeutil.fmt_local(t.ends_at),
            "estimated_hours": None if t.estimated_hours is None else str(t.estimated_hours),
            "weather_sensitive": t.weather_sensitive, "requires_permit": ref("permit", t.requires_permit_id),
            "requires_inspection": ref("inspection", t.requires_inspection_id),
            "requires_po": ref("purchase_order", t.requires_po_id), "crew": crew or []}


def add_crew_member(session: Session, actor: Actor, name: str, trade: str | None = None, phone: str | None = None,
                    daily_capacity_hours: Any = "8", user_id: int | None = None) -> dict[str, Any]:
    require(actor, "admin:users")
    c = CrewMember(name=name, trade=trade, phone=phone, daily_capacity_hours=D(daily_capacity_hours), user_id=user_id,
                   created_by_id=actor.user_id)
    session.add(c)
    session.flush()
    audit.record(session, actor, "crew.add", "crew", c.id, name=name)
    return {"ref": ref("crew", c.id), "name": c.name, "trade": c.trade, "capacity_hours": str(c.daily_capacity_hours)}


def list_crew(session: Session, actor: Actor) -> list[dict[str, Any]]:
    require(actor, "read:all")
    return [{"ref": ref("crew", c.id), "id": c.id, "name": c.name, "trade": c.trade,
             "capacity_hours": str(c.daily_capacity_hours)}
            for c in session.scalars(select(CrewMember).where(CrewMember.active.is_(True)).order_by(CrewMember.name))]


def add_task(session: Session, actor: Actor, job_id: int, *, name: str, phase: str | None = None,
             starts_at: str | None = None, ends_at: str | None = None, estimated_hours: Any = None,
             weather_sensitive: bool = False, depends_on: list[int] | None = None, crew_ids: list[int] | None = None,
             requires_permit_id: int | None = None, requires_inspection_id: int | None = None,
             requires_po_id: int | None = None, notes: str | None = None) -> dict[str, Any]:
    require(actor, "write:job")
    require_job_access(session, actor, job_id, write=True)
    job = session.get(Job, job_id)
    t = Task(job_id=job_id, name=name, phase=phase, status="scheduled" if starts_at else "todo",
             starts_at=timeutil.parse_local(starts_at) if starts_at else None,
             ends_at=timeutil.parse_local(ends_at) if ends_at else None,
             estimated_hours=None if estimated_hours in (None, "") else D(estimated_hours),
             weather_sensitive=weather_sensitive, requires_permit_id=requires_permit_id,
             requires_inspection_id=requires_inspection_id, requires_po_id=requires_po_id, notes=notes,
             is_synthetic=job.is_synthetic, created_by_id=actor.user_id)
    if t.starts_at and t.ends_at and t.ends_at <= t.starts_at:
        raise ValidationFailed("task must end after it starts")
    session.add(t)
    session.flush()
    for dep in depends_on or []:
        add_dependency(session, actor, t.id, dep)
    for cid in crew_ids or []:
        assign_crew(session, actor, t.id, cid)
    audit.record(session, actor, "task.add", "task", t.id, job=job_id, name=name)
    return {"task": task_view(t, _crew_names(session, t.id)), "conflicts": detect_conflicts(session, actor, job_id=job_id)["conflicts"]}


def _crew_names(session: Session, task_id: int) -> list[str]:
    return [c.name for c in session.scalars(select(CrewMember).join(TaskAssignment, TaskAssignment.crew_member_id == CrewMember.id)
                                            .where(TaskAssignment.task_id == task_id))]


def add_dependency(session: Session, actor: Actor, task_id: int, depends_on_task_id: int) -> None:
    require(actor, "write:job")
    a, b = session.get(Task, task_id), session.get(Task, depends_on_task_id)
    if a is None or b is None:
        raise NotFound("task not found")
    if a.job_id != b.job_id:
        raise ValidationFailed("dependencies must be within one job")
    # Reject cycles: walk predecessors of b looking for a.
    seen, stack = set(), [depends_on_task_id]
    while stack:
        cur = stack.pop()
        if cur == task_id:
            raise ValidationFailed("dependency would create a cycle")
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(session.scalars(select(TaskDependency.depends_on_task_id).where(TaskDependency.task_id == cur)))
    if session.scalar(select(TaskDependency).where(TaskDependency.task_id == task_id,
                                                   TaskDependency.depends_on_task_id == depends_on_task_id)) is None:
        session.add(TaskDependency(task_id=task_id, depends_on_task_id=depends_on_task_id))
        session.flush()


def assign_crew(session: Session, actor: Actor, task_id: int, crew_member_id: int) -> None:
    require(actor, "write:job")
    if session.get(CrewMember, crew_member_id) is None:
        raise NotFound(f"{ref('crew', crew_member_id)} not found")
    if session.scalar(select(TaskAssignment).where(TaskAssignment.task_id == task_id,
                                                   TaskAssignment.crew_member_id == crew_member_id)) is None:
        session.add(TaskAssignment(task_id=task_id, crew_member_id=crew_member_id))
        session.flush()


def update_task_status(session: Session, actor: Actor, task_id: int, status: str, note: str | None = None) -> dict[str, Any]:
    t = session.get(Task, task_id)
    if t is None:
        raise NotFound("task not found")
    require_job_access(session, actor, t.job_id, write=True)
    if not (actor.can("write:job") or actor.can("write:field")):
        raise ValidationFailed("not allowed")
    if status == "in_progress":
        blockers = prerequisite_blockers(session, t)
        if blockers:
            raise Conflict("prerequisites not met", blockers=blockers)
    old = t.status
    t.status = status
    audit.record(session, actor, "task.status", "task", t.id, frm=old, to=status, note=note)
    return task_view(t, _crew_names(session, t.id))


def prerequisite_blockers(session: Session, t: Task) -> list[str]:
    out = []
    for dep in session.scalars(select(Task).join(TaskDependency, TaskDependency.depends_on_task_id == Task.id)
                               .where(TaskDependency.task_id == t.id)):
        if dep.status != "done":
            out.append(f"predecessor {ref('task', dep.id)} '{dep.name}' is {dep.status}")
    if t.requires_permit_id:
        p = session.get(Permit, t.requires_permit_id)
        if p is None or p.status not in ("issued", "not_required_verified"):
            out.append(f"permit {ref('permit', t.requires_permit_id)} not issued ({p.status if p else 'missing'})")
    if t.requires_inspection_id:
        i = session.get(Inspection, t.requires_inspection_id)
        if i is None or i.result != "passed":
            out.append(f"inspection {ref('inspection', t.requires_inspection_id)} not passed ({i.result if i else 'missing'})")
    if t.requires_po_id:
        po = session.get(PurchaseOrder, t.requires_po_id)
        if po is None or po.status not in ("received",):
            out.append(f"materials {ref('purchase_order', t.requires_po_id)} not received ({po.status if po else 'missing'})")
            if po is not None and t.starts_at and po.status in ("draft", "pending_approval", "approved") and po.lead_time_days:
                earliest = timeutil.today_local() + dt.timedelta(days=po.lead_time_days)
                if earliest > timeutil.to_local(t.starts_at).date():
                    out.append(f"PO lead time {po.lead_time_days}d means earliest delivery {earliest}, after task start")
    return out


def _workdays(session: Session) -> tuple[set[int] | None, set[str]]:
    cal = settings.get(session, "work_calendar")
    if not cal:
        return None, set()
    return set(cal.get("workdays", [0, 1, 2, 3, 4])), set(cal.get("holidays", []))


def detect_conflicts(session: Session, actor: Actor, *, job_id: int | None = None, days: int = 21) -> dict[str, Any]:
    """Crew double-booking, daily capacity, dependency order, prerequisites, calendar, weather advisories."""
    require(actor, "read:all") if job_id is None else require_job_access(session, actor, job_id)
    now = timeutil.now()
    horizon = now + dt.timedelta(days=days)
    q = select(Task).where(Task.status.in_(("todo", "scheduled", "in_progress", "blocked")))
    visible = job_ids_visible(session, actor)
    if visible is not None:
        q = q.where(Task.job_id.in_(visible or {-1}))
    tasks = list(session.scalars(q))
    by_id = {t.id: t for t in tasks}
    conflicts: list[dict[str, Any]] = []
    advisories: list[dict[str, Any]] = []

    assigns: dict[int, list[Task]] = defaultdict(list)
    for ta in session.scalars(select(TaskAssignment).where(TaskAssignment.task_id.in_(list(by_id) or [-1]))):
        assigns[ta.crew_member_id].append(by_id[ta.task_id])
    crew = {c.id: c for c in session.scalars(select(CrewMember))}
    for cid, ts in assigns.items():
        timed = sorted([t for t in ts if t.starts_at and t.ends_at and t.starts_at < horizon], key=lambda t: t.starts_at)
        for i, a in enumerate(timed):
            for b in timed[i + 1:]:
                if b.starts_at >= a.ends_at:
                    break
                conflicts.append({"type": "crew_double_booked", "crew": crew[cid].name,
                                  "tasks": [ref("task", a.id), ref("task", b.id)],
                                  "jobs": sorted({ref("job", a.job_id), ref("job", b.job_id)}),
                                  "message": f"{crew[cid].name} is booked on overlapping tasks"})
        per_day: dict[dt.date, Decimal] = defaultdict(Decimal)
        for t in timed:
            if t.estimated_hours is not None:
                per_day[timeutil.to_local(t.starts_at).date()] += t.estimated_hours
        for day, hrs in per_day.items():
            if hrs > crew[cid].daily_capacity_hours:
                conflicts.append({"type": "over_capacity", "crew": crew[cid].name, "date": day.isoformat(),
                                  "hours": str(hrs), "capacity": str(crew[cid].daily_capacity_hours),
                                  "message": f"{crew[cid].name} has {hrs}h on {day} (capacity {crew[cid].daily_capacity_hours}h)"})

    for dep in session.scalars(select(TaskDependency).where(TaskDependency.task_id.in_(list(by_id) or [-1]))):
        t = by_id.get(dep.task_id)
        pre = session.get(Task, dep.depends_on_task_id)
        if t and pre and t.starts_at and pre.ends_at and t.starts_at < pre.ends_at and pre.status != "done":
            conflicts.append({"type": "dependency_order", "tasks": [ref("task", pre.id), ref("task", t.id)],
                              "jobs": [ref("job", t.job_id)],
                              "message": f"'{t.name}' starts before predecessor '{pre.name}' finishes"})

    workdays, holidays = _workdays(session)
    weather_connected = session.get(Integration, "weather")
    weather_on = weather_connected is not None and weather_connected.status == "connected"
    for t in tasks:
        if job_id is not None and t.job_id != job_id:
            continue
        if t.starts_at and now <= t.starts_at < horizon:
            blockers = prerequisite_blockers(session, t)
            if blockers:
                conflicts.append({"type": "missing_prerequisite", "tasks": [ref("task", t.id)], "jobs": [ref("job", t.job_id)],
                                  "message": f"'{t.name}': " + "; ".join(blockers)})
            local = timeutil.to_local(t.starts_at)
            if workdays is not None and (local.weekday() not in workdays or local.date().isoformat() in holidays):
                conflicts.append({"type": "non_working_day", "tasks": [ref("task", t.id)], "jobs": [ref("job", t.job_id)],
                                  "message": f"'{t.name}' is scheduled on a non-working day ({local:%a %b %-d})"})
            if t.weather_sensitive and t.starts_at < now + dt.timedelta(days=5):
                advisories.append({"type": "weather", "tasks": [ref("task", t.id)], "jobs": [ref("job", t.job_id)],
                                   "message": f"'{t.name}' is weather-sensitive on {local:%a %b %-d}: "
                                              + ("see forecast advisory" if weather_on else "no weather source connected - check the forecast"),
                                   "source": "weather integration" if weather_on else None})
    if workdays is None:
        advisories.append({"type": "calendar", "message": "work calendar not configured; working-day checks skipped"})
    if job_id is not None:
        conflicts = [c for c in conflicts if ref("job", job_id) in c.get("jobs", [ref("job", job_id)])]
    return {"conflicts": conflicts, "advisories": advisories}


def propose_reschedule(session: Session, actor: Actor, task_id: int, new_start: str) -> dict[str, Any]:
    """Compute (without saving) the shift for a task and all downstream dependents."""
    t = session.get(Task, task_id)
    if t is None:
        raise NotFound("task not found")
    require_job_access(session, actor, t.job_id)
    if t.starts_at is None:
        raise ValidationFailed("task has no current start")
    start = timeutil.parse_local(new_start)
    delta = start - t.starts_at
    moves, queue, seen = [], [t.id], set()
    while queue:
        cur = session.get(Task, queue.pop(0))
        if cur.id in seen:
            continue
        seen.add(cur.id)
        if cur.starts_at:
            moves.append({"task": ref("task", cur.id), "name": cur.name, "job": ref("job", cur.job_id),
                          "from": timeutil.fmt_local(cur.starts_at), "to": timeutil.fmt_local(cur.starts_at + delta),
                          "crew": _crew_names(session, cur.id)})
        queue.extend(session.scalars(select(TaskDependency.task_id).where(TaskDependency.depends_on_task_id == cur.id)))
    job = session.get(Job, t.job_id)
    appts = list(session.scalars(select(Appointment).where(Appointment.job_id == job.id, Appointment.status == "confirmed")))
    return {"task": ref("task", t.id), "shift_hours": round(delta.total_seconds() / 3600, 2), "moves": moves,
            "affected_people": sorted({n for m in moves for n in m["crew"]}),
            "customer_commitments": [{"appointment": ref("appointment", a.id), "at": timeutil.fmt_local(a.starts_at)}
                                     for a in appts],
            "note": "Proposal only. Committing changes internal dates; customer notices need separate approval."}


def commit_reschedule(session: Session, actor: Actor, task_id: int, new_start: str) -> dict[str, Any]:
    require(actor, "schedule:commit")
    proposal = propose_reschedule(session, actor, task_id, new_start)
    t = session.get(Task, task_id)
    delta = timeutil.parse_local(new_start) - t.starts_at
    for m in proposal["moves"]:
        from ..refs import parse

        tk = session.get(Task, parse(m["task"], "task"))
        tk.starts_at = tk.starts_at + delta
        if tk.ends_at:
            tk.ends_at = tk.ends_at + delta
    audit.record(session, actor, "schedule.commit", "task", task_id, moves=proposal["moves"])
    return {**proposal, "committed": True, "conflicts": detect_conflicts(session, actor, job_id=t.job_id)["conflicts"]}


def job_schedule(session: Session, actor: Actor, job_id: int) -> dict[str, Any]:
    require_job_access(session, actor, job_id)
    tasks = list(session.scalars(select(Task).where(Task.job_id == job_id).order_by(Task.starts_at.asc().nulls_last(), Task.id)))
    # Field users only see tasks they are assigned to.
    if actor.role == "crew":
        mine = set(session.scalars(select(TaskAssignment.task_id).join(CrewMember, CrewMember.id == TaskAssignment.crew_member_id)
                                   .where(CrewMember.user_id == actor.user_id)))
        tasks = [t for t in tasks if t.id in mine]
    return {"job": ref("job", job_id), "tasks": [task_view(t, _crew_names(session, t.id)) for t in tasks],
            **detect_conflicts(session, actor, job_id=job_id)}


def weather_status(session: Session) -> str:
    integrations.ensure_rows(session)
    row = session.get(Integration, "weather")
    return row.status
