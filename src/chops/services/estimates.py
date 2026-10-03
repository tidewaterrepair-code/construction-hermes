"""Versioned estimates. Draft revisions are editable; locked revisions are immutable."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import pricing, timeutil
from ..authz import Actor, require
from ..errors import Conflict, InvalidTransition, NotFound, ValidationFailed
from ..formula import FormulaError, evaluate
from ..hashing import content_hash, jsonable
from ..models import ITEM_KINDS, PRICING_MODES, Assembly, Estimate, EstimateItem, EstimateRevision, Lead
from ..money import D, pct
from ..refs import ref
from . import audit, rates, settings

POLICY_FIELDS = ("contingency_pct", "overhead_pct", "profit_method", "profit_pct", "discount_amount", "discount_pct",
                 "tax_mode", "material_tax_pct", "sales_tax_pct", "labor_burden_pct", "quote_type")
PCT_FIELDS = {"contingency_pct", "overhead_pct", "profit_pct", "discount_pct", "material_tax_pct", "sales_tax_pct",
              "labor_burden_pct"}


def _get_estimate(session: Session, estimate_id: int) -> Estimate:
    e = session.get(Estimate, estimate_id)
    if e is None:
        raise NotFound(f"{ref('estimate', estimate_id)} not found")
    return e


def current_revision(session: Session, estimate: Estimate) -> EstimateRevision:
    rev = session.scalar(select(EstimateRevision).where(EstimateRevision.estimate_id == estimate.id)
                         .order_by(EstimateRevision.revision_no.desc()).limit(1))
    if rev is None:
        raise NotFound("estimate has no revisions")
    return rev


def _draft(session: Session, estimate_id: int) -> EstimateRevision:
    e = _get_estimate(session, estimate_id)
    rev = current_revision(session, e)
    if rev.status != "draft":
        raise InvalidTransition(
            f"{ref('estimate', e.id)} r{rev.revision_no} is locked (a proposal was built from it). "
            "Create a new revision to change it.")
    return rev


def create(session: Session, actor: Actor, *, title: str, pricing_mode: str, lead_id: int | None = None,
           job_id: int | None = None, scope_summary: str | None = None) -> dict[str, Any]:
    require(actor, "write:estimate")
    if pricing_mode not in PRICING_MODES:
        raise ValidationFailed(f"pricing_mode must be one of {PRICING_MODES}")
    synthetic = False
    if lead_id is not None:
        lead = session.get(Lead, lead_id)
        if lead is None:
            raise NotFound(f"{ref('lead', lead_id)} not found")
        synthetic = lead.is_synthetic
    e = Estimate(title=title, pricing_mode=pricing_mode, lead_id=lead_id, job_id=job_id, is_synthetic=synthetic,
                 created_by_id=actor.user_id)
    session.add(e)
    session.flush()
    defaults = settings.get(session, "estimating_defaults", {}) or {}
    rev = EstimateRevision(estimate_id=e.id, revision_no=1, status="draft", scope_summary=scope_summary,
                           created_by_id=actor.user_id, dimensions={}, inclusions=[], exclusions=[], assumptions=[],
                           unresolved_conditions=[])
    _apply_policy(rev, defaults, from_defaults=True)
    if pricing_mode == "labor_only":
        rev.exclusions = ["Materials (customer/owner-supplied unless listed)"]
    session.add(rev)
    session.flush()
    audit.record(session, actor, "estimate.create", "estimate", e.id, pricing_mode=pricing_mode, lead=lead_id)
    return view(session, actor, e.id)


def _apply_policy(rev: EstimateRevision, values: dict[str, Any], from_defaults: bool = False) -> dict[str, Any]:
    changed = {}
    for k, v in values.items():
        if k not in POLICY_FIELDS:
            if from_defaults:
                continue
            raise ValidationFailed(f"unknown policy field {k}")
        if v is None:
            continue
        if k in PCT_FIELDS:
            v = pct(v)
        if k in ("discount_amount",):
            v = D(v)
        if k == "profit_method" and v not in ("markup", "margin"):
            raise ValidationFailed("profit_method must be markup or margin")
        if k == "tax_mode" and v not in pricing.TAX_MODES:
            raise ValidationFailed(f"tax_mode must be one of {pricing.TAX_MODES}")
        if k == "quote_type" and v not in ("rough_range", "firm"):
            raise ValidationFailed("quote_type must be rough_range or firm")
        setattr(rev, k, v)
        changed[k] = str(v)
    return changed


def set_policy(session: Session, actor: Actor, estimate_id: int, **values: Any) -> dict[str, Any]:
    require(actor, "write:estimate")
    rev = _draft(session, estimate_id)
    changed = _apply_policy(rev, values)
    session.flush()
    audit.record(session, actor, "estimate.policy", "estimate", estimate_id, revision=rev.revision_no, changed=changed)
    return view(session, actor, estimate_id)


def set_dimension(session: Session, actor: Actor, estimate_id: int, name: str, value: Any, unit: str,
                  source: str, note: str | None = None) -> dict[str, Any]:
    require(actor, "write:estimate")
    if source not in pricing.ALL_DIMENSION_SOURCES:
        raise ValidationFailed(f"source must be one of {sorted(pricing.ALL_DIMENSION_SOURCES)}")
    rev = _draft(session, estimate_id)
    dims = dict(rev.dimensions or {})
    dims[name] = {"value": None if value in (None, "") else str(D(value)), "unit": unit, "source": source, "note": note,
                  "recorded_by": actor.display_name or actor.role, "at": timeutil.now().isoformat()}
    rev.dimensions = dims
    session.flush()
    audit.record(session, actor, "estimate.dimension", "estimate", estimate_id, name=name, value=str(value), source=source)
    return view(session, actor, estimate_id)


def add_line(session: Session, actor: Actor, estimate_id: int, *, kind: str, description: str,
             quantity: Any = None, unit: str | None = None, rate_code: str | None = None,
             unit_cost: Any = None, cost_source: str | None = None, waste_pct: Any = 0,
             taxable: bool | None = None, cost_code: str | None = None, quantity_source: str | None = None,
             customer_text: str | None = None, notes: str | None = None) -> dict[str, Any]:
    require(actor, "write:estimate")
    if kind not in ITEM_KINDS:
        raise ValidationFailed(f"kind must be one of {ITEM_KINDS}")
    rev = _draft(session, estimate_id)
    est = rev.estimate
    item = EstimateItem(revision_id=rev.id, line_no=_next_line(session, rev), kind=kind, description=description,
                        quantity=None if quantity in (None, "") else D(quantity), unit=unit,
                        waste_pct=pct(waste_pct or 0), cost_code=cost_code, quantity_source=quantity_source,
                        customer_visible_text=customer_text, notes=notes,
                        taxable=kind == "material" if taxable is None else taxable)
    _attach_cost(session, actor, item, rate_code, unit_cost, cost_source, est.is_synthetic)
    session.add(item)
    session.flush()
    audit.record(session, actor, "estimate.line_add", "estimate", estimate_id, line=item.line_no, kind=kind,
                 description=description, rate_code=rate_code, unit_cost=None if item.unit_cost is None else str(item.unit_cost))
    return view(session, actor, estimate_id)


def _next_line(session: Session, rev: EstimateRevision) -> int:
    return (session.scalar(select(func.max(EstimateItem.line_no)).where(EstimateItem.revision_id == rev.id)) or 0) + 1


def _attach_cost(session: Session, actor: Actor, item: EstimateItem, rate_code: str | None, unit_cost: Any,
                 cost_source: str | None, synthetic: bool) -> None:
    if rate_code:
        r = rates.find_active(session, rate_code, synthetic=synthetic)
        if r is None:
            item.notes = ((item.notes or "") + f" [rate {rate_code} not in library]").strip()
            item.unit_cost = None
            item.rate_status = None
            return
        item.rate_id = r.id
        item.unit_cost = r.unit_cost
        item.rate_status = rates.effective_status(r)
        item.rate_source = r.source
        item.rate_source_date = r.source_date
        if item.unit and r.unit and item.unit.lower() != r.unit.lower():
            raise ValidationFailed(f"unit mismatch: line uses {item.unit}, rate {rate_code} is per {r.unit}")
        return
    if unit_cost not in (None, ""):
        if not cost_source:
            raise ValidationFailed("a direct unit cost needs cost_source (quote, invoice, owner, ...)")
        item.unit_cost = D(unit_cost)
        # Only the owner/office entering a number directly makes it owner-entered.
        item.rate_status = "owner_entered" if actor.can("rates:verify") else "provisional"
        item.rate_source = cost_source
        item.rate_source_date = timeutil.today_local()


def update_line(session: Session, actor: Actor, estimate_id: int, line_no: int, **fields: Any) -> dict[str, Any]:
    require(actor, "write:estimate")
    rev = _draft(session, estimate_id)
    item = session.scalar(select(EstimateItem).where(EstimateItem.revision_id == rev.id, EstimateItem.line_no == line_no))
    if item is None:
        raise NotFound(f"line {line_no} not found")
    changed = {}
    for k in ("description", "unit", "cost_code", "quantity_source", "customer_text", "notes"):
        if k in fields and fields[k] is not None:
            setattr(item, "customer_visible_text" if k == "customer_text" else k, fields[k])
            changed[k] = fields[k]
    if fields.get("quantity") not in (None, ""):
        item.quantity = D(fields["quantity"])
        changed["quantity"] = str(item.quantity)
    if fields.get("waste_pct") not in (None, ""):
        item.waste_pct = pct(fields["waste_pct"])
        changed["waste_pct"] = str(item.waste_pct)
    if fields.get("rate_code") or fields.get("unit_cost") not in (None, ""):
        _attach_cost(session, actor, item, fields.get("rate_code"), fields.get("unit_cost"), fields.get("cost_source"),
                     rev.estimate.is_synthetic)
        changed["cost"] = None if item.unit_cost is None else str(item.unit_cost)
    if fields.get("remove"):
        session.delete(item)
        changed["removed"] = True
    session.flush()
    audit.record(session, actor, "estimate.line_update", "estimate", estimate_id, line=line_no, changed=changed)
    return view(session, actor, estimate_id)


def apply_assembly(session: Session, actor: Actor, estimate_id: int, assembly_code: str,
                   params: dict[str, Any]) -> dict[str, Any]:
    """Expand a template. ``params`` = {name: {"value": .., "source": ..}} or {name: value}.

    Parameter values become revision dimensions with their source; template defaults are
    recorded with source "assumed" so they block a firm quote until confirmed.
    """
    require(actor, "write:estimate")
    a = session.scalar(select(Assembly).where(Assembly.code == assembly_code))
    if a is None:
        raise NotFound(f"assembly {assembly_code} not found")
    rev = _draft(session, estimate_id)
    values: dict[str, Decimal | None] = {}
    dims = dict(rev.dimensions or {})
    for p in a.parameters:
        name = p["name"]
        raw = params.get(name)
        if isinstance(raw, dict):
            val, src = raw.get("value"), raw.get("source", "assumed")
        elif raw is not None:
            val, src = raw, "assumed"
        elif p.get("default") is not None:
            val, src = p["default"], "assumed"
        else:
            val, src = None, "assumed"
        if src not in pricing.ALL_DIMENSION_SOURCES:
            raise ValidationFailed(f"parameter {name}: source must be one of {sorted(pricing.ALL_DIMENSION_SOURCES)}")
        values[name] = None if val in (None, "") else D(val)
        dims[f"{assembly_code}.{name}"] = {"value": None if values[name] is None else str(values[name]),
                                           "unit": p.get("unit"), "source": src, "label": p.get("label")}
    rev.dimensions = dims
    added = []
    for comp in a.components:
        try:
            qty = evaluate(comp["qty"], values).quantize(Decimal("0.0001"))
            qsrc = f"{assembly_code}: {comp['qty']}"
        except FormulaError as exc:
            qty, qsrc = None, f"{assembly_code}: {exc}"
        item = EstimateItem(revision_id=rev.id, line_no=_next_line(session, rev), kind=comp["kind"],
                            description=comp["description"], quantity=qty, unit=comp.get("unit"),
                            waste_pct=D(comp.get("waste", "0")), cost_code=comp.get("cost_code"),
                            quantity_source=qsrc, taxable=comp["kind"] == "material")
        _attach_cost(session, actor, item, comp.get("rate_code"), None, None, rev.estimate.is_synthetic)
        session.add(item)
        session.flush()
        added.append(item.line_no)
    if a.is_example:
        assumptions = list(rev.assumptions or [])
        note = f"Quantities from example template '{a.name}' - not an engineered design."
        if note not in assumptions:
            assumptions.append(note)
        rev.assumptions = assumptions
    session.flush()
    audit.record(session, actor, "estimate.assembly", "estimate", estimate_id, assembly=assembly_code, lines=added)
    return view(session, actor, estimate_id)


def set_text_lists(session: Session, actor: Actor, estimate_id: int, *, scope_summary: str | None = None,
                   add_inclusion: str | None = None, add_exclusion: str | None = None,
                   add_assumption: str | None = None, add_condition: str | None = None,
                   resolve_condition: int | None = None, resolution: str | None = None) -> dict[str, Any]:
    require(actor, "write:estimate")
    rev = _draft(session, estimate_id)
    if scope_summary is not None:
        rev.scope_summary = scope_summary
    for attr, val in (("inclusions", add_inclusion), ("exclusions", add_exclusion), ("assumptions", add_assumption)):
        if val:
            rev_list = list(getattr(rev, attr) or [])
            rev_list.append(val)
            setattr(rev, attr, rev_list)
    if add_condition:
        conds = list(rev.unresolved_conditions or [])
        conds.append({"condition": add_condition, "resolution": None})
        rev.unresolved_conditions = conds
    if resolve_condition is not None:
        conds = list(rev.unresolved_conditions or [])
        if not (0 <= resolve_condition < len(conds)):
            raise ValidationFailed("no such condition index")
        if not resolution:
            raise ValidationFailed("resolution text required (e.g. 'carried as $X allowance' or 'excluded')")
        conds[resolve_condition] = {**conds[resolve_condition], "resolution": resolution}
        rev.unresolved_conditions = conds
    session.flush()
    audit.record(session, actor, "estimate.text", "estimate", estimate_id)
    return view(session, actor, estimate_id)


def _policy_for(session: Session, rev: EstimateRevision) -> pricing.Policy:
    band = settings.get(session, "rough_range")
    return pricing.Policy(
        pricing_mode=rev.estimate.pricing_mode, quote_type=rev.quote_type, contingency_pct=rev.contingency_pct or D(0),
        overhead_pct=rev.overhead_pct, profit_method=rev.profit_method, profit_pct=rev.profit_pct,
        discount_pct=rev.discount_pct or D(0), discount_amount=rev.discount_amount or D(0), tax_mode=rev.tax_mode,
        material_tax_pct=rev.material_tax_pct, sales_tax_pct=rev.sales_tax_pct, labor_burden_pct=rev.labor_burden_pct,
        dimensions=rev.dimensions or {}, unresolved_conditions=rev.unresolved_conditions or [],
        range_low_pct=pct(band["low_pct"]) if band else D("0.10"),
        range_high_pct=pct(band["high_pct"]) if band else D("0.25"), range_band_configured=bool(band))


def compute_revision(session: Session, rev: EstimateRevision) -> dict[str, Any]:
    lines = [pricing.Line(line_no=i.line_no, kind=i.kind, description=i.description, quantity=i.quantity, unit=i.unit,
                          unit_cost=i.unit_cost, waste_pct=i.waste_pct or D(0), rate_status=i.rate_status,
                          rate_valid_until=(i.rate_id and _rate_valid_until(session, i.rate_id)) or None,
                          taxable=i.taxable, cost_code=i.cost_code)
             for i in rev.items]
    return pricing.compute(lines, _policy_for(session, rev), today=timeutil.today_local())


def _rate_valid_until(session: Session, rate_id: int):
    from ..models import Rate

    r = session.get(Rate, rate_id)
    return r.valid_until if r else None


def revision_snapshot(rev: EstimateRevision) -> dict[str, Any]:
    """Everything that defines the revision's content (hashed for immutability checks)."""
    return jsonable({
        "estimate_id": rev.estimate_id, "revision_no": rev.revision_no, "quote_type": rev.quote_type,
        "dimensions": rev.dimensions, "policy": {f: getattr(rev, f) for f in POLICY_FIELDS},
        "scope_summary": rev.scope_summary, "inclusions": rev.inclusions, "exclusions": rev.exclusions,
        "assumptions": rev.assumptions, "unresolved_conditions": rev.unresolved_conditions,
        "items": [{"line": i.line_no, "kind": i.kind, "description": i.description, "quantity": i.quantity,
                   "unit": i.unit, "waste": i.waste_pct, "unit_cost": i.unit_cost, "rate_id": i.rate_id,
                   "rate_status": i.rate_status, "taxable": i.taxable, "cost_code": i.cost_code,
                   "customer_text": i.customer_visible_text} for i in rev.items],
    })


def recalculate(session: Session, actor: Actor, estimate_id: int) -> dict[str, Any]:
    require(actor, "write:estimate")
    rev = _draft(session, estimate_id)
    session.refresh(rev)
    rev.totals = compute_revision(session, rev)
    rev.content_hash = content_hash(revision_snapshot(rev))
    session.flush()
    return view(session, actor, estimate_id)


def lock_revision(session: Session, actor: Actor, rev: EstimateRevision) -> EstimateRevision:
    if rev.status != "draft":
        return rev
    session.refresh(rev)
    rev.totals = compute_revision(session, rev)
    rev.content_hash = content_hash(revision_snapshot(rev))
    rev.status = "locked"
    rev.locked_at = timeutil.now()
    session.flush()
    audit.record(session, actor, "estimate.lock", "estimate", rev.estimate_id, revision=rev.revision_no,
                 content_hash=rev.content_hash)
    return rev


def new_revision(session: Session, actor: Actor, estimate_id: int, reason: str) -> dict[str, Any]:
    require(actor, "write:estimate")
    e = _get_estimate(session, estimate_id)
    old = current_revision(session, e)
    if old.status == "draft":
        raise Conflict(f"r{old.revision_no} is still a draft; edit it directly")
    new = EstimateRevision(estimate_id=e.id, revision_no=old.revision_no + 1, status="draft",
                           quote_type=old.quote_type, dimensions=dict(old.dimensions or {}),
                           scope_summary=old.scope_summary, inclusions=list(old.inclusions or []),
                           exclusions=list(old.exclusions or []), assumptions=list(old.assumptions or []),
                           unresolved_conditions=list(old.unresolved_conditions or []), notes=reason,
                           created_by_id=actor.user_id)
    for f in POLICY_FIELDS:
        setattr(new, f, getattr(old, f))
    session.add(new)
    session.flush()
    for i in old.items:
        session.add(EstimateItem(revision_id=new.id, line_no=i.line_no, kind=i.kind, description=i.description,
                                 cost_code=i.cost_code, quantity=i.quantity, unit=i.unit, quantity_source=i.quantity_source,
                                 waste_pct=i.waste_pct, rate_id=i.rate_id, unit_cost=i.unit_cost, rate_status=i.rate_status,
                                 rate_source=i.rate_source, rate_source_date=i.rate_source_date, taxable=i.taxable,
                                 customer_visible_text=i.customer_visible_text, notes=i.notes))
    old.status = "superseded"
    if e.status == "proposed":
        e.status = "draft"
    session.flush()
    audit.record(session, actor, "estimate.new_revision", "estimate", e.id, revision=new.revision_no, reason=reason)
    return view(session, actor, e.id)


def view(session: Session, actor: Actor, estimate_id: int, revision_no: int | None = None) -> dict[str, Any]:
    require(actor, "read:financial")
    e = _get_estimate(session, estimate_id)
    if revision_no is None:
        rev = current_revision(session, e)
    else:
        rev = session.scalar(select(EstimateRevision).where(EstimateRevision.estimate_id == e.id,
                                                            EstimateRevision.revision_no == revision_no))
        if rev is None:
            raise NotFound("revision not found")
    session.refresh(rev)
    totals = rev.totals if rev.status != "draft" else compute_revision(session, rev)
    return {
        "ref": ref("estimate", e.id), "id": e.id, "title": e.title, "status": e.status,
        "pricing_mode": e.pricing_mode, "lead": ref("lead", e.lead_id), "job": ref("job", e.job_id),
        "revision": rev.revision_no, "revision_status": rev.status, "quote_type": rev.quote_type,
        "content_hash": rev.content_hash, "scope_summary": rev.scope_summary,
        "dimensions": rev.dimensions, "inclusions": rev.inclusions, "exclusions": rev.exclusions,
        "assumptions": rev.assumptions, "unresolved_conditions": rev.unresolved_conditions,
        "policy": {f: (None if getattr(rev, f) is None else str(getattr(rev, f))) for f in POLICY_FIELDS},
        "items": [{"line": i.line_no, "kind": i.kind, "description": i.description,
                   "quantity": None if i.quantity is None else str(i.quantity), "unit": i.unit,
                   "waste_pct": str(i.waste_pct), "unit_cost": None if i.unit_cost is None else str(i.unit_cost),
                   "rate": ref("rate", i.rate_id), "rate_status": i.rate_status, "rate_source": i.rate_source,
                   "rate_source_date": timeutil.iso(i.rate_source_date), "cost_code": i.cost_code,
                   "quantity_source": i.quantity_source, "notes": i.notes} for i in rev.items],
        "totals": totals,
        "revisions": [{"revision": r.revision_no, "status": r.status} for r in e.revisions],
        "synthetic": e.is_synthetic,
    }


def list_estimates(session: Session, actor: Actor, include_synthetic: bool = False, limit: int = 50) -> list[dict[str, Any]]:
    require(actor, "read:financial")
    q = select(Estimate).order_by(Estimate.id.desc()).limit(min(limit, 200))
    if not include_synthetic:
        q = q.where(Estimate.is_synthetic.is_(False))
    out = []
    for e in session.scalars(q):
        rev = current_revision(session, e)
        totals = rev.totals or {}
        out.append({"ref": ref("estimate", e.id), "title": e.title, "status": e.status, "revision": rev.revision_no,
                    "revision_status": rev.status, "pricing_mode": e.pricing_mode, "total": totals.get("total"),
                    "firm_quote_ready": totals.get("firm_quote_ready"), "lead": ref("lead", e.lead_id)})
    return out
