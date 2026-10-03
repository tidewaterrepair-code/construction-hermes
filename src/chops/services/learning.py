"""Controlled learning: evidence-backed suggestions only. Nothing here edits rates,
permissions, tax rules, templates or code; the owner decides and edits explicitly."""

from __future__ import annotations

from decimal import Decimal
from statistics import mean, pstdev
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..authz import Actor, require
from ..errors import InvalidTransition, NotFound
from ..hashing import jsonable
from ..models import CostEntry, EstimateRevision, Job, LearningProposal
from ..money import D
from ..refs import ref
from . import audit

MIN_SAMPLE = 3


def labor_productivity(session: Session, actor: Actor) -> dict[str, Any]:
    """Compare estimated vs actual labor hours per cost code across completed jobs."""
    require(actor, "read:financial")
    ratios: dict[str, list[tuple[str, Decimal]]] = {}
    for job in session.scalars(select(Job).where(Job.status.in_(("complete", "closed")), Job.is_synthetic.is_(False))):
        if not job.source_estimate_revision_id:
            continue
        rev = session.get(EstimateRevision, job.source_estimate_revision_id)
        est_hours: dict[str, Decimal] = {}
        for i in rev.items:
            if i.kind == "labor" and i.quantity:
                est_hours[i.cost_code or "labor"] = est_hours.get(i.cost_code or "labor", Decimal(0)) + i.quantity
        act_hours: dict[str, Decimal] = {}
        for c in session.scalars(select(CostEntry).where(CostEntry.job_id == job.id, CostEntry.kind == "labor",
                                                         CostEntry.hours.is_not(None), CostEntry.status != "reversed")):
            act_hours[c.cost_code or "labor"] = act_hours.get(c.cost_code or "labor", Decimal(0)) + c.hours
        for code, eh in est_hours.items():
            if code in act_hours and eh > 0:
                ratios.setdefault(code, []).append((ref("job", job.id), act_hours[code] / eh))
    created = []
    for code, rows in ratios.items():
        vals = [float(r) for _, r in rows]
        n = len(vals)
        proposal = {"cost_code": code, "mean_actual_over_estimate": round(mean(vals), 3),
                    "spread": round(pstdev(vals), 3) if n > 1 else None,
                    "suggestion": (f"Labor on {code} ran {round((mean(vals) - 1) * 100)}% vs estimate; consider adjusting productivity"
                                   if n >= MIN_SAMPLE else "insufficient sample; keep collecting")}
        lp = LearningProposal(kind="labor_productivity", subject=code, proposal=jsonable(proposal),
                              evidence=jsonable([{"job": j, "ratio": str(r.quantize(D('0.001')))} for j, r in rows]),
                              sample_size=n, status="proposed")
        session.add(lp)
        session.flush()
        created.append({"ref": f"LRN-{lp.id}", **proposal, "sample_size": n})
    return {"proposals": created, "note": "Suggestions only; approved rates and templates are unchanged."}


def decide(session: Session, actor: Actor, proposal_id: int, accept: bool) -> dict[str, Any]:
    require(actor, "learning:decide")
    lp = session.get(LearningProposal, proposal_id)
    if lp is None:
        raise NotFound("proposal not found")
    if lp.status != "proposed":
        raise InvalidTransition(f"already {lp.status}")
    lp.status = "accepted" if accept else "rejected"
    lp.decided_by_id = actor.user_id
    audit.record(session, actor, "learning.decide", None, lp.id, accept=accept)
    return {"ref": f"LRN-{lp.id}", "status": lp.status,
            "note": "Accepted means noted; update the rate or template yourself to apply it."}
