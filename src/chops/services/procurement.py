"""Vendors, compliance documents, quotes, takeoffs and purchase orders."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import timeutil
from ..authz import Actor, require, require_job_access
from ..errors import Conflict, InvalidTransition, NotFound, ValidationFailed
from ..hashing import content_hash, jsonable
from ..models import Contact, EstimateRevision, Job, POLine, PurchaseOrder, VendorDocument, VendorQuote
from ..money import ZERO, D, q2
from ..refs import ref
from ..units import packs_needed
from . import audit, leads

REQUIRED_SUB_DOCS = ("insurance_coi", "license")


def add_vendor(session: Session, actor: Actor, *, name: str, kind: str = "vendor", email: str | None = None,
               phone: str | None = None, company: str | None = None, trade: str | None = None) -> dict[str, Any]:
    require(actor, "write:procurement")
    if kind not in ("vendor", "subcontractor"):
        raise ValidationFailed("kind must be vendor or subcontractor")
    from . import settings as _settings

    c = Contact(kind=kind, name=name, company=company, email=email, email_normalized=leads.normalize_email(email),
                phone=phone, phone_normalized=leads.normalize_phone(phone), trade=trade, created_by_id=actor.user_id,
                is_synthetic=_settings.environment(session) != "prod")
    session.add(c)
    session.flush()
    audit.record(session, actor, "vendor.add", "contact", c.id, kind=kind)
    return {"ref": ref("contact", c.id), "id": c.id, "name": c.name, "kind": kind, "trade": trade}


def add_vendor_document(session: Session, actor: Actor, contact_id: int, *, doc_type: str,
                        expires_on: str | None = None, identifier: str | None = None, document_id: int | None = None,
                        verification_status: str = "unverified", note: str | None = None) -> dict[str, Any]:
    require(actor, "write:procurement")
    if verification_status != "unverified" and not actor.can("rates:verify"):
        raise ValidationFailed("only the owner/office can record a verification status")
    vd = VendorDocument(contact_id=contact_id, doc_type=doc_type, document_id=document_id, identifier=identifier,
                        expires_on=dt.date.fromisoformat(expires_on) if expires_on else None,
                        verification_status=verification_status, verification_note=note, created_by_id=actor.user_id)
    session.add(vd)
    session.flush()
    audit.record(session, actor, "vendor_document.add", "vendor_document", vd.id, doc_type=doc_type)
    return {"ref": ref("vendor_document", vd.id), "doc_type": doc_type, "expires_on": expires_on,
            "verification_status": verification_status,
            "note": "Recorded as supplied. This system does not certify insurance or licensing."}


def compliance(session: Session, actor: Actor, warn_days: int = 30) -> list[dict[str, Any]]:
    require(actor, "read:all")
    today = timeutil.today_local()
    out = []
    for c in session.scalars(select(Contact).where(Contact.kind.in_(("vendor", "subcontractor")))):
        docs = list(session.scalars(select(VendorDocument).where(VendorDocument.contact_id == c.id)))
        flags = []
        if c.kind == "subcontractor":
            for req in REQUIRED_SUB_DOCS:
                if not any(d.doc_type == req for d in docs):
                    flags.append(f"missing {req}")
        for d in docs:
            if d.expires_on and d.expires_on < today:
                flags.append(f"{d.doc_type} expired {d.expires_on}")
            elif d.expires_on and d.expires_on <= today + dt.timedelta(days=warn_days):
                flags.append(f"{d.doc_type} expires {d.expires_on}")
            if d.verification_status == "unverified":
                flags.append(f"{d.doc_type} unverified")
        if flags:
            out.append({"vendor": ref("contact", c.id), "name": c.name, "kind": c.kind, "flags": flags})
    return out


def record_quote(session: Session, actor: Actor, vendor_id: int, *, lines: list[dict[str, Any]], received_on: str,
                 job_id: int | None = None, expires_on: str | None = None, delivery_cost: Any = 0,
                 tax_assumption: str = "not stated", tax_amount: Any = None, availability: str | None = None,
                 reference: str | None = None, document_id: int | None = None, is_subcontract: bool = False,
                 scope: str | None = None) -> dict[str, Any]:
    """lines: [{item_key, description, qty, unit, pack_size (default 1), unit_price (per pack)}]."""
    require(actor, "write:procurement")
    if job_id is not None:
        require_job_access(session, actor, job_id)
    clean = []
    for ln in lines:
        if not ln.get("item_key"):
            raise ValidationFailed("each line needs item_key (the exact spec being priced) for like-for-like comparison")
        clean.append({"item_key": ln["item_key"], "description": ln.get("description", ln["item_key"]),
                      "qty": str(D(ln.get("qty", 1))), "unit": ln.get("unit", "ea"),
                      "pack_size": str(D(ln.get("pack_size", 1))), "unit_price": str(D(ln["unit_price"]))})
    prev = session.scalar(select(func.max(VendorQuote.revision_no)).where(
        VendorQuote.vendor_id == vendor_id, VendorQuote.reference == reference)) if reference else None
    q = VendorQuote(vendor_id=vendor_id, job_id=job_id, reference=reference, revision_no=(prev or 0) + 1,
                    received_on=dt.date.fromisoformat(received_on),
                    expires_on=dt.date.fromisoformat(expires_on) if expires_on else None, lines=jsonable(clean),
                    delivery_cost=q2(D(delivery_cost or 0)), tax_assumption=tax_assumption,
                    tax_amount=None if tax_amount in (None, "") else q2(D(tax_amount)), availability=availability,
                    document_id=document_id, is_subcontract=is_subcontract, scope=scope, created_by_id=actor.user_id)
    session.add(q)
    session.flush()
    audit.record(session, actor, "quote.record", "vendor_quote", q.id, vendor=vendor_id, job=job_id)
    return {"ref": ref("vendor_quote", q.id), "revision": q.revision_no, "lines": clean}


def compare_quotes(session: Session, actor: Actor, need: list[dict[str, Any]], job_id: int | None = None) -> dict[str, Any]:
    """need: [{item_key, qty, unit}]. Compares only identical item_keys (no substitutions)."""
    require(actor, "read:financial")
    today = timeutil.today_local()
    q = select(VendorQuote)
    if job_id is not None:
        q = q.where((VendorQuote.job_id == job_id) | (VendorQuote.job_id.is_(None)))
    quotes = list(session.scalars(q))
    results = []
    for qt in quotes:
        vendor = session.get(Contact, qt.vendor_id)
        by_key = {ln["item_key"]: ln for ln in qt.lines}
        missing = [n["item_key"] for n in need if n["item_key"] not in by_key]
        rows, subtotal, caveats = [], ZERO, []
        for n in need:
            ln = by_key.get(n["item_key"])
            if ln is None:
                continue
            if ln["unit"].lower() != n.get("unit", ln["unit"]).lower():
                caveats.append(f"{n['item_key']}: quoted per {ln['unit']}, needed in {n.get('unit')}")
                continue
            packs = packs_needed(n["qty"], ln["pack_size"])
            cost = q2(D(packs) * D(ln["unit_price"]))
            per_unit = D(ln["unit_price"]) / D(ln["pack_size"])
            rows.append({"item_key": n["item_key"], "needed": str(n["qty"]), "packs": packs, "pack_size": ln["pack_size"],
                         "per_unit": str(per_unit.quantize(Decimal("0.0001"))), "cost": str(cost),
                         "overage_units": str(D(packs) * D(ln["pack_size"]) - D(n["qty"]))})
            subtotal += cost
        expired = qt.expires_on is not None and qt.expires_on < today
        if expired:
            caveats.append(f"quote expired {qt.expires_on}")
        if qt.tax_amount is None:
            caveats.append(f"tax: {qt.tax_assumption}")
        if not qt.availability:
            caveats.append("availability not stated (no current inventory evidence)")
        results.append({"quote": ref("vendor_quote", qt.id), "vendor": vendor.name, "revision": qt.revision_no,
                        "received_on": qt.received_on.isoformat(), "expires_on": timeutil.iso(qt.expires_on),
                        "expired": expired, "complete": not missing, "missing_items": missing, "lines": rows,
                        "subtotal": str(q2(subtotal)), "delivery": str(qt.delivery_cost),
                        "tax": None if qt.tax_amount is None else str(qt.tax_amount),
                        "total_known": str(q2(subtotal + qt.delivery_cost + (qt.tax_amount or ZERO))), "caveats": caveats})
    valid = [r for r in results if r["complete"] and not r["expired"]]
    best = min(valid, key=lambda r: D(r["total_known"])) if valid else None
    return {"comparison": sorted(results, key=lambda r: (not r["complete"], r["expired"], D(r["total_known"]))),
            "lowest_complete_current": best and best["quote"],
            "note": "Like-for-like only (same item_key). Structural materials are never substituted on price alone."}


def material_takeoff(session: Session, actor: Actor, job_id: int) -> dict[str, Any]:
    require(actor, "read:financial")
    require_job_access(session, actor, job_id)
    job = session.get(Job, job_id)
    if not job.source_estimate_revision_id:
        raise ValidationFailed("job has no source estimate")
    rev = session.get(EstimateRevision, job.source_estimate_revision_id)
    items = []
    for i in rev.items:
        if i.kind != "material" or i.quantity is None:
            continue
        with_waste = (i.quantity * (1 + i.waste_pct)).quantize(Decimal("0.01"))
        items.append({"line": i.line_no, "description": i.description, "quantity": str(i.quantity),
                      "waste_pct": str(i.waste_pct), "order_quantity": str(with_waste), "unit": i.unit,
                      "cost_code": i.cost_code, "rate": ref("rate", i.rate_id)})
    return {"job": ref("job", job_id), "estimate_revision_id": rev.id, "items": items,
            "note": "Quantities come from the accepted estimate revision; verify against plans before ordering."}


def _po_hash(po: PurchaseOrder) -> str:
    return content_hash({"job": po.job_id, "vendor": po.vendor_id, "number": po.number,
                         "lines": [[ln.description, ln.quantity, ln.unit, ln.unit_cost, ln.cost_code] for ln in po.lines],
                         "delivery": po.delivery_cost, "tax": po.tax_amount, "total": po.total, "needed_by": po.needed_by})


def ensure_hash(po: PurchaseOrder) -> None:
    if po.content_hash != _po_hash(po):
        raise Conflict("PO content changed since it was hashed")


def draft_po(session: Session, actor: Actor, job_id: int, vendor_id: int, *, lines: list[dict[str, Any]],
             delivery_cost: Any = 0, tax_amount: Any = 0, needed_by: str | None = None, lead_time_days: int | None = None,
             vendor_quote_id: int | None = None, notes: str | None = None) -> dict[str, Any]:
    require(actor, "write:procurement")
    require_job_access(session, actor, job_id, write=True)
    job = session.get(Job, job_id)
    if session.get(Contact, vendor_id) is None:
        raise NotFound("vendor not found")
    n = (session.scalar(select(func.count()).select_from(PurchaseOrder)) or 0) + 1
    po = PurchaseOrder(job_id=job_id, vendor_id=vendor_id, number=f"PO-{job_id}-{n:04d}", status="draft",
                       vendor_quote_id=vendor_quote_id, needed_by=dt.date.fromisoformat(needed_by) if needed_by else None,
                       lead_time_days=lead_time_days, delivery_cost=q2(D(delivery_cost or 0)),
                       tax_amount=q2(D(tax_amount or 0)), notes=notes, is_synthetic=job.is_synthetic,
                       created_by_id=actor.user_id)
    session.add(po)
    session.flush()
    subtotal = ZERO
    for i, ln in enumerate(lines, start=1):
        if not ln.get("cost_code"):
            raise ValidationFailed(f"line {i}: cost_code required (POs are tied to job cost codes)")
        qty, cost = D(ln["qty"]), D(ln["unit_cost"])
        amt = q2(qty * cost)
        subtotal += amt
        po.lines.append(POLine(line_no=i, description=ln["description"], quantity=qty, unit=ln.get("unit", "ea"),
                               unit_cost=cost, cost_code=ln["cost_code"], amount=amt))
    po.subtotal = subtotal
    po.total = subtotal + po.delivery_cost + po.tax_amount
    session.flush()
    po.content_hash = _po_hash(po)
    audit.record(session, actor, "po.draft", "purchase_order", po.id, total=str(po.total))
    return po_view(po)


def po_view(po: PurchaseOrder) -> dict[str, Any]:
    return {"ref": ref("purchase_order", po.id), "id": po.id, "number": po.number, "job": ref("job", po.job_id),
            "vendor": ref("contact", po.vendor_id), "status": po.status, "total": str(po.total),
            "needed_by": timeutil.iso(po.needed_by), "lead_time_days": po.lead_time_days,
            "lines": [{"description": ln.description, "qty": str(ln.quantity), "unit": ln.unit,
                       "unit_cost": str(ln.unit_cost), "amount": str(ln.amount), "cost_code": ln.cost_code} for ln in po.lines]}


def request_po_issue(session: Session, actor: Actor, po_id: int) -> dict[str, Any]:
    from . import approvals

    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise NotFound("PO not found")
    require_job_access(session, actor, po.job_id)
    res = approvals.request(session, actor, "purchase_order.issue", po.id, synthetic=po.is_synthetic)
    po.status = "pending_approval"
    return res


def receive_po(session: Session, actor: Actor, po_id: int, partial: bool = False, note: str | None = None) -> dict[str, Any]:
    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise NotFound("PO not found")
    require_job_access(session, actor, po.job_id, write=True)
    if po.status not in ("approved", "issued", "partially_received"):
        raise InvalidTransition(f"PO is {po.status}")
    po.status = "partially_received" if partial else "received"
    audit.record(session, actor, "po.receive", "purchase_order", po.id, partial=partial, note=note)
    return po_view(po)
