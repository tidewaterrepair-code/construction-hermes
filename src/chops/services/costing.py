"""Job costing: estimated vs committed vs actual vs forecast, exceptions, CSV import."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
from collections import defaultdict
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import timeutil
from ..authz import Actor, require, require_job_access
from ..errors import Conflict, NotFound, ValidationFailed
from ..models import ChangeOrder, CostEntry, Invoice, Job, JobBudgetLine, PurchaseOrder
from ..money import ZERO, D, gross_margin, q2
from ..refs import parse, ref
from . import audit, billing
from . import settings as _settings


def cost_view(c: CostEntry) -> dict[str, Any]:
    return {"ref": ref("cost", c.id), "id": c.id, "job": ref("job", c.job_id), "kind": c.kind, "cost_code": c.cost_code,
            "description": c.description, "amount": str(c.amount), "occurred_on": c.occurred_on.isoformat(),
            "vendor": c.vendor_name, "po": ref("purchase_order", c.po_id), "receipt": ref("document", c.document_id),
            "status": c.status, "source": c.source, "match_note": c.match_note, "corrects": ref("cost", c.corrects_id)}


def log_cost(session: Session, actor: Actor, job_id: int, *, kind: str, amount: Any, description: str,
             occurred_on: str | None = None, cost_code: str | None = None, vendor_name: str | None = None,
             vendor_id: int | None = None, po_id: int | None = None, document_id: int | None = None,
             source: str = "manual", external_id: str | None = None, hours: Any = None) -> dict[str, Any]:
    """Record an actual cost (e.g. a receipt). Uncertain PO matches are routed to review."""
    if not (actor.can("write:billing") or actor.can("write:field")):
        raise ValidationFailed("not allowed to log costs")
    require_job_access(session, actor, job_id, write=True)
    if external_id:
        dup = session.scalar(select(CostEntry).where(CostEntry.source == source, CostEntry.external_id == external_id))
        if dup is not None:
            return {"cost": cost_view(dup), "duplicate": True}
    job = session.get(Job, job_id)
    amt = q2(D(amount))
    c = CostEntry(job_id=job_id, kind=kind, amount=amt, description=description,
                  occurred_on=dt.date.fromisoformat(occurred_on) if occurred_on else timeutil.today_local(),
                  cost_code=cost_code, vendor_name=vendor_name, vendor_id=vendor_id, po_id=po_id, document_id=document_id,
                  source=source, external_id=external_id, hours=None if hours in (None, "") else D(hours),
                  status="unreviewed", is_synthetic=job.is_synthetic, created_by_id=actor.user_id)
    session.add(c)
    session.flush()
    _match(session, c)
    audit.record(session, actor, "cost.log", "cost", c.id, job=job_id, amount=str(amt), kind=kind)
    out = {"cost": cost_view(c), "duplicate": False}
    if not actor.sees_financials:
        out["cost"].pop("match_note", None)
    return out


def _match(session: Session, c: CostEntry) -> None:
    if c.po_id is not None:
        po = session.get(PurchaseOrder, c.po_id)
        if po is None or po.job_id != c.job_id:
            c.status = "needs_review"
            c.match_note = "PO reference does not belong to this job"
        elif abs(po.total - c.amount) > max(Decimal("1.00"), po.total * Decimal("0.02")):
            c.status = "needs_review"
            c.match_note = f"amount differs from {po.number} total {po.total}"
        else:
            c.match_note = f"matches {po.number}"
            c.cost_code = c.cost_code or (po.lines[0].cost_code if po.lines else None)
        return
    if c.vendor_name and c.kind == "material":
        cands = [po for po in session.scalars(select(PurchaseOrder).where(PurchaseOrder.job_id == c.job_id,
                                                                          PurchaseOrder.status.in_(("approved", "issued", "partially_received", "received"))))
                 if abs(po.total - c.amount) <= max(Decimal("1.00"), po.total * Decimal("0.02"))]
        if len(cands) == 1:
            c.match_note = f"possible match {cands[0].number} (confirm)"
            c.status = "needs_review"
        elif len(cands) > 1:
            c.match_note = "several POs could match; review"
            c.status = "needs_review"
    if not c.cost_code:
        c.status = "needs_review"
        c.match_note = (c.match_note + "; " if c.match_note else "") + "no cost code"


def approve_cost(session: Session, actor: Actor, cost_id: int, cost_code: str | None = None, po_id: int | None = None) -> dict[str, Any]:
    require(actor, "write:billing")
    c = session.get(CostEntry, cost_id)
    if c is None:
        raise NotFound("cost not found")
    if cost_code:
        c.cost_code = cost_code
    if po_id:
        c.po_id = po_id
    if not c.cost_code:
        raise ValidationFailed("cost code required")
    c.status = "approved"
    audit.record(session, actor, "cost.approve", "cost", c.id)
    return cost_view(c)


def correct_cost(session: Session, actor: Actor, cost_id: int, *, new_amount: Any, reason: str) -> dict[str, Any]:
    """Never edits history: reverses the original and books a corrected entry."""
    require(actor, "write:billing")
    c = session.get(CostEntry, cost_id)
    if c is None:
        raise NotFound("cost not found")
    if c.status == "reversed":
        raise Conflict("already reversed")
    if not reason:
        raise ValidationFailed("reason required")
    c.status = "reversed"
    rev = CostEntry(job_id=c.job_id, kind=c.kind, amount=-c.amount, description=f"Reversal of {ref('cost', c.id)}: {reason}",
                    occurred_on=c.occurred_on, cost_code=c.cost_code, vendor_name=c.vendor_name, source="correction",
                    status="reversed", corrects_id=c.id, is_synthetic=c.is_synthetic, created_by_id=actor.user_id)
    new = CostEntry(job_id=c.job_id, kind=c.kind, amount=q2(D(new_amount)), description=c.description,
                    occurred_on=c.occurred_on, cost_code=c.cost_code, vendor_name=c.vendor_name, po_id=c.po_id,
                    document_id=c.document_id, source="correction", status="approved", corrects_id=c.id,
                    is_synthetic=c.is_synthetic, created_by_id=actor.user_id)
    session.add_all([rev, new])
    session.flush()
    audit.record(session, actor, "cost.correct", "cost", c.id, reason=reason, old=str(c.amount), new=str(new.amount))
    return {"reversed": ref("cost", c.id), "reversal": ref("cost", rev.id), "corrected": cost_view(new)}


def _actuals(session: Session, job_id: int) -> dict[str, Decimal]:
    out: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for c in session.scalars(select(CostEntry).where(CostEntry.job_id == job_id)):
        if c.status == "reversed" and c.corrects_id is None:
            continue  # original that was reversed
        if c.status == "reversed" and c.corrects_id is not None:
            continue  # reversal rows net out the original, which we already skipped
        out[c.cost_code or "uncoded"] += c.amount
    return out


def set_forecast(session: Session, actor: Actor, job_id: int, cost_code: str, forecast_to_complete: Any, note: str) -> dict[str, Any]:
    require(actor, "write:billing")
    require_job_access(session, actor, job_id)
    line = session.scalar(select(JobBudgetLine).where(JobBudgetLine.job_id == job_id, JobBudgetLine.cost_code == cost_code))
    if line is None:
        line = JobBudgetLine(job_id=job_id, cost_code=cost_code, description=cost_code, estimated_cost=ZERO,
                             source="forecast_only")
        session.add(line)
    line.forecast_to_complete = q2(D(forecast_to_complete))
    line.forecast_note = note[:300]
    audit.record(session, actor, "cost.forecast", "job", job_id, cost_code=cost_code, ftc=str(forecast_to_complete), note=note)
    return job_cost_report(session, actor, job_id)


def job_cost_report(session: Session, actor: Actor, job_id: int) -> dict[str, Any]:
    require(actor, "read:financial")
    require_job_access(session, actor, job_id)
    job = session.get(Job, job_id)
    if job is None:
        raise NotFound("job not found")
    budget: dict[str, Decimal] = defaultdict(lambda: ZERO)
    ftc_manual: dict[str, Decimal] = {}
    for b in session.scalars(select(JobBudgetLine).where(JobBudgetLine.job_id == job_id)):
        budget[b.cost_code] += b.estimated_cost
        if b.forecast_to_complete is not None:
            ftc_manual[b.cost_code] = ftc_manual.get(b.cost_code, ZERO) + b.forecast_to_complete
    committed: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for po in session.scalars(select(PurchaseOrder).where(PurchaseOrder.job_id == job_id,
                                                          PurchaseOrder.status.in_(("approved", "issued", "partially_received", "received")))):
        for ln in po.lines:
            committed[ln.cost_code] += ln.amount
    actual = _actuals(session, job_id)
    codes = sorted(set(budget) | set(committed) | set(actual))
    rows = []
    tot = {"estimated": ZERO, "committed": ZERO, "actual": ZERO, "forecast_to_complete": ZERO, "projected": ZERO}
    for code in codes:
        est, com, act = budget.get(code, ZERO), committed.get(code, ZERO), actual.get(code, ZERO)
        if code in ftc_manual:
            ftc, basis = ftc_manual[code], "manual forecast"
        else:
            ftc, basis = max(ZERO, max(est, com) - act), "max(estimate, committed) - actual"
        proj = act + ftc
        rows.append({"cost_code": code, "estimated": str(q2(est)), "committed": str(q2(com)), "actual": str(q2(act)),
                     "forecast_to_complete": str(q2(ftc)), "forecast_basis": basis, "projected_final": str(q2(proj)),
                     "variance": str(q2(est - proj)), "over_budget": proj > est})
        for k, v in (("estimated", est), ("committed", com), ("actual", act), ("forecast_to_complete", ftc), ("projected", proj)):
            tot[k] += v
    fin = billing.job_financials(session, actor, job_id)
    contract = D(fin["approved_contract_value"])
    estimated_margin = gross_margin(contract, tot["estimated"])
    projected_margin = gross_margin(contract, tot["projected"])
    erosion = None if estimated_margin is None or projected_margin is None else estimated_margin - projected_margin
    return {"job": ref("job", job_id), "name": job.name, "rows": rows, "totals": {k: str(q2(v)) for k, v in tot.items()},
            "approved_contract_value": str(contract), "estimated_gross_margin": None if estimated_margin is None else str(estimated_margin),
            "projected_gross_margin": None if projected_margin is None else str(projected_margin),
            "margin_erosion": None if erosion is None else str(erosion),
            "unreviewed_costs": session.scalar(select(func.count()).select_from(CostEntry).where(
                CostEntry.job_id == job_id, CostEntry.status.in_(("unreviewed", "needs_review")))),
            "definitions": "Gross margin here = (approved contract - job cost) / approved contract; job cost excludes company overhead."}


def exceptions(session: Session, actor: Actor, erosion_threshold: Decimal = Decimal("0.05"),
               include_synthetic: bool = False) -> list[dict[str, Any]]:
    require(actor, "read:financial")
    out = []
    q = select(Job).where(Job.status.in_(("planned", "active", "on_hold", "complete")))
    if not _settings.include_synthetic(session, include_synthetic):
        q = q.where(Job.is_synthetic.is_(False))
    today = timeutil.today_local()
    for job in session.scalars(q):
        r = job_cost_report(session, actor, job.id)
        if r["margin_erosion"] is not None and D(r["margin_erosion"]) >= erosion_threshold:
            out.append({"type": "margin_erosion", "job": ref("job", job.id), "name": job.name,
                        "message": f"{job.name}: projected margin {r['projected_gross_margin']} vs estimated {r['estimated_gross_margin']}"})
        for row in r["rows"]:
            if row["over_budget"] and D(row["estimated"]) > 0:
                out.append({"type": "forecast_overrun", "job": ref("job", job.id), "name": job.name,
                            "message": f"{job.name} {row['cost_code']}: projected {row['projected_final']} vs budget {row['estimated']}"})
        if r["unreviewed_costs"]:
            out.append({"type": "costs_need_review", "job": ref("job", job.id), "name": job.name,
                        "message": f"{job.name}: {r['unreviewed_costs']} cost entries need review"})
        for co in session.scalars(select(ChangeOrder).where(ChangeOrder.job_id == job.id, ChangeOrder.status == "customer_approved")):
            key = f"co-{co.number}-r{co.revision_no}"
            if session.scalar(select(Invoice.id).where(Invoice.job_id == job.id, Invoice.milestone_key == key)) is None:
                out.append({"type": "unbilled_change_order", "job": ref("job", job.id), "name": job.name,
                            "message": f"{job.name}: approved change order #{co.number} (${co.price:,.2f}) not invoiced"})
        missing_receipts = session.scalar(select(func.count()).select_from(CostEntry).where(
            CostEntry.job_id == job.id, CostEntry.kind.in_(("material", "equipment", "other")), CostEntry.document_id.is_(None),
            CostEntry.status != "reversed", CostEntry.source.in_(("manual", "agent"))))
        if missing_receipts:
            out.append({"type": "missing_receipts", "job": ref("job", job.id), "name": job.name,
                        "message": f"{job.name}: {missing_receipts} cost entries have no receipt attached"})
        for inv in session.scalars(select(Invoice).where(Invoice.job_id == job.id, Invoice.status.in_(("issued", "partially_paid")),
                                                         Invoice.due_on < today)):
            out.append({"type": "overdue_invoice", "job": ref("job", job.id), "name": job.name,
                        "message": f"Invoice {inv.number} overdue since {inv.due_on}"})
    return out


# ------------------------------------------------------------------ CSV import (preview, then commit)

REQUIRED_COLS = ("date", "amount", "description")


def preview_import(session: Session, actor: Actor, csv_text: str, mapping: dict[str, str], default_job: str | None = None,
                   source: str = "csv") -> dict[str, Any]:
    """mapping: our field -> CSV column. Fields: date, amount, description, job, cost_code, vendor, kind, external_id."""
    require(actor, "write:billing")
    if len(csv_text) > 2_000_000:
        raise ValidationFailed("CSV too large (2 MB max)")
    reader = csv.DictReader(io.StringIO(csv_text))
    for f in REQUIRED_COLS:
        if f not in mapping or mapping[f] not in (reader.fieldnames or []):
            raise ValidationFailed(f"mapping for '{f}' missing or column not found", columns=reader.fieldnames)
    rows, errors = [], []
    for i, raw in enumerate(reader, start=2):
        if i > 5001:
            errors.append({"row": i, "error": "row limit 5000 reached"})
            break
        try:
            amt = q2(D(raw[mapping["amount"]]))
            date = dt.date.fromisoformat(raw[mapping["date"]].strip()) if "-" in raw[mapping["date"]] else \
                dt.datetime.strptime(raw[mapping["date"]].strip(), "%m/%d/%Y").date()
            job_ref = raw.get(mapping.get("job", ""), "") or default_job
            if not job_ref:
                raise ValueError("no job")
            job_id = parse(job_ref, "job")
            require_job_access(session, actor, job_id)
            if session.get(Job, job_id) is None:
                raise ValueError(f"{job_ref} not found")
            ext = raw.get(mapping.get("external_id", ""), "") or hashlib.sha256(
                "|".join(f"{k}={raw[k]}" for k in sorted(raw)).encode()).hexdigest()[:32]
            dup = session.scalar(select(CostEntry.id).where(CostEntry.source == source, CostEntry.external_id == ext))
            rows.append({"row": i, "job_id": job_id, "date": date.isoformat(), "amount": str(amt),
                         "description": raw[mapping["description"]][:300], "cost_code": raw.get(mapping.get("cost_code", ""), None) or None,
                         "vendor": raw.get(mapping.get("vendor", ""), None) or None,
                         "kind": (raw.get(mapping.get("kind", ""), "") or "material").lower(), "external_id": ext,
                         "duplicate": dup is not None})
        except Exception as exc:  # noqa: BLE001 - every row error is reported, none silently dropped
            errors.append({"row": i, "error": str(exc)[:200]})
    token = hashlib.sha256(repr(rows).encode()).hexdigest()
    return {"rows": rows[:50], "row_count": len(rows), "duplicates": sum(r["duplicate"] for r in rows), "errors": errors,
            "preview_token": token, "note": "Nothing saved. Commit with the same file, mapping and preview_token."}


def commit_import(session: Session, actor: Actor, csv_text: str, mapping: dict[str, str], preview_token: str,
                  default_job: str | None = None, source: str = "csv") -> dict[str, Any]:
    pv = preview_import(session, actor, csv_text, mapping, default_job, source)
    if pv["preview_token"] != preview_token:
        raise Conflict("file or mapping changed since preview; preview again")
    if pv["errors"]:
        raise ValidationFailed("fix row errors before committing", errors=pv["errors"][:20])
    created = 0
    full = _all_rows(session, actor, csv_text, mapping, default_job, source)
    for r in full:
        if r["duplicate"]:
            continue
        log_cost(session, actor, r["job_id"], kind=r["kind"] if r["kind"] in ("labor", "material", "subcontract", "equipment", "other") else "other",
                 amount=r["amount"], description=r["description"], occurred_on=r["date"], cost_code=r["cost_code"],
                 vendor_name=r["vendor"], source=source, external_id=r["external_id"])
        created += 1
    audit.record(session, actor, "cost.import", None, None, created=created, token=preview_token)
    return {"created": created, "skipped_duplicates": pv["duplicates"]}


def _all_rows(session: Session, actor: Actor, csv_text: str, mapping: dict[str, str], default_job: str | None, source: str) -> list[dict[str, Any]]:
    # preview_import truncates its display list; recompute the full list for commit.
    import copy

    m = copy.deepcopy(mapping)
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = []
    for raw in reader:
        amt = q2(D(raw[m["amount"]]))
        dstr = raw[m["date"]].strip()
        date = dt.date.fromisoformat(dstr) if "-" in dstr else dt.datetime.strptime(dstr, "%m/%d/%Y").date()
        job_id = parse(raw.get(m.get("job", ""), "") or default_job, "job")
        ext = raw.get(m.get("external_id", ""), "") or hashlib.sha256(
            "|".join(f"{k}={raw[k]}" for k in sorted(raw)).encode()).hexdigest()[:32]
        dup = session.scalar(select(CostEntry.id).where(CostEntry.source == source, CostEntry.external_id == ext))
        rows.append({"job_id": job_id, "date": date.isoformat(), "amount": str(amt), "description": raw[m["description"]][:300],
                     "cost_code": raw.get(m.get("cost_code", ""), None) or None, "vendor": raw.get(m.get("vendor", ""), None) or None,
                     "kind": (raw.get(m.get("kind", ""), "") or "material").lower(), "external_id": ext, "duplicate": dup is not None})
    return rows
