"""Invoices, payments, retainage, and receivables.

Vocabulary kept distinct everywhere:
- contract value: accepted proposal + customer-approved change orders
- invoiced: sum of issued invoices (draft/pending invoices are not invoiced)
- collected cash: verified/reconciled payments only ("reported" payments are not cash)
- retainage held: withheld on issued invoices, receivable until released and paid
Accounting revenue recognition is out of scope; this is not a tax ledger.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import pdf, timeutil
from ..authz import Actor, require
from ..errors import Conflict, InvalidTransition, NotFound, ValidationFailed
from ..hashing import content_hash, jsonable
from ..models import ChangeOrder, Invoice, Job, Payment, Proposal
from ..money import ZERO, D, fmt, q2
from ..refs import ref
from . import audit, documents, jobs, settings


def invoice_view(inv: Invoice) -> dict[str, Any]:
    return {"ref": ref("invoice", inv.id), "id": inv.id, "number": inv.number, "job": ref("job", inv.job_id),
            "kind": inv.kind, "milestone": inv.milestone_key, "status": inv.status, "lines": inv.lines,
            "subtotal": str(inv.subtotal), "tax": str(inv.tax), "retainage_pct": str(inv.retainage_pct),
            "retainage_held": str(inv.retainage_held), "total_due": str(inv.total_due),
            "issued_on": timeutil.iso(inv.issued_on), "due_on": timeutil.iso(inv.due_on),
            "content_hash": inv.content_hash, "external": inv.external_id and f"{inv.external_system}:{inv.external_id}",
            "synthetic": inv.is_synthetic}


def _next_number(session: Session) -> str:
    n = (session.scalar(select(func.count()).select_from(Invoice)) or 0) + 1
    year = timeutil.today_local().year
    while True:
        candidate = f"{year}-{n:04d}"
        if session.scalar(select(Invoice.id).where(Invoice.number == candidate)) is None:
            return candidate
        n += 1


def _invoice_hash(inv: Invoice) -> str:
    return content_hash({"job": inv.job_id, "number": inv.number, "lines": inv.lines, "subtotal": inv.subtotal,
                         "tax": inv.tax, "retainage_pct": inv.retainage_pct, "retainage_held": inv.retainage_held,
                         "total_due": inv.total_due, "due_on": inv.due_on})


def draft_milestone_invoice(session: Session, actor: Actor, job_id: int, milestone_key: str) -> dict[str, Any]:
    """Draft an invoice for a contract payment milestone. Idempotent per (job, milestone)."""
    require(actor, "write:billing")
    job = jobs.get(session, actor, job_id)
    existing = session.scalar(select(Invoice).where(Invoice.job_id == job.id, Invoice.milestone_key == milestone_key,
                                                    Invoice.status != "void"))
    if existing is not None:
        return {"invoice": invoice_view(existing), "reused": True}
    p = session.get(Proposal, job.source_proposal_id) if job.source_proposal_id else None
    if p is None:
        raise ValidationFailed("job has no source proposal; use a manual invoice")
    ms = {m["key"]: m for m in p.content.get("milestones") or []}
    if milestone_key not in ms:
        raise NotFound(f"milestone {milestone_key!r} not in contract; available: {sorted(ms)}")
    m = ms[milestone_key]
    amount = D(m["amount_value"])
    return _create(session, actor, job, kind="milestone", milestone_key=milestone_key,
                   lines=[{"description": f"{m['label']} — {m['pct_display']} of contract ({ref('proposal', p.id)})",
                           "amount": str(amount)}])


def draft_change_order_invoice(session: Session, actor: Actor, change_order_id: int) -> dict[str, Any]:
    require(actor, "write:billing")
    co = session.get(ChangeOrder, change_order_id)
    if co is None:
        raise NotFound("change order not found")
    if co.status != "customer_approved":
        raise InvalidTransition("only customer-approved change orders can be invoiced")
    job = jobs.get(session, actor, co.job_id)
    key = f"co-{co.number}-r{co.revision_no}"
    existing = session.scalar(select(Invoice).where(Invoice.job_id == job.id, Invoice.milestone_key == key,
                                                    Invoice.status != "void"))
    if existing is not None:
        return {"invoice": invoice_view(existing), "reused": True}
    return _create(session, actor, job, kind="change_order", milestone_key=key, change_order_id=co.id,
                   lines=[{"description": f"Change order #{co.number} r{co.revision_no}: {co.title}", "amount": str(co.price)}])


def _create(session: Session, actor: Actor, job: Job, *, kind: str, milestone_key: str, lines: list[dict[str, Any]],
            change_order_id: int | None = None) -> dict[str, Any]:
    subtotal = q2(sum((D(ln["amount"]) for ln in lines), ZERO))
    retainage_held = q2(subtotal * job.retainage_pct)
    days = settings.get(session, "invoice_terms_days")
    inv = Invoice(job_id=job.id, number=_next_number(session), kind=kind, milestone_key=milestone_key, status="draft",
                  lines=jsonable(lines), subtotal=subtotal, tax=ZERO, retainage_pct=job.retainage_pct,
                  retainage_held=retainage_held, total_due=subtotal - retainage_held,
                  due_on=None if days is None else timeutil.today_local() + dt.timedelta(days=int(days)),
                  change_order_id=change_order_id, content_hash="", is_synthetic=job.is_synthetic,
                  created_by_id=actor.user_id)
    session.add(inv)
    session.flush()
    inv.content_hash = _invoice_hash(inv)
    audit.record(session, actor, "invoice.draft", "invoice", inv.id, job=job.id, total_due=str(inv.total_due))
    return {"invoice": invoice_view(inv), "reused": False,
            "note": "Draft only. Issuing to the customer requires owner approval."
            + ("" if days is not None else " Invoice terms (invoice_terms_days) not configured: no due date set.")}


def get_invoice(session: Session, actor: Actor, invoice_id: int) -> Invoice:
    require(actor, "read:financial")
    inv = session.get(Invoice, invoice_id)
    if inv is None:
        raise NotFound(f"{ref('invoice', invoice_id)} not found")
    jobs.get(session, actor, inv.job_id)
    return inv


def request_issue(session: Session, actor: Actor, invoice_id: int) -> dict[str, Any]:
    from . import approvals

    inv = get_invoice(session, actor, invoice_id)
    if inv.status not in ("draft", "pending_approval"):
        raise InvalidTransition(f"invoice is {inv.status}")
    res = approvals.request(session, actor, "invoice.issue", inv.id, synthetic=inv.is_synthetic)
    inv.status = "pending_approval"
    return res


def render_invoice_pdf(session: Session, actor: Actor, inv: Invoice) -> dict[str, Any]:
    job = session.get(Job, inv.job_id)
    c = {"company": {"name": settings.company_display(session), "contact": settings.get(session, "company_contact"),
                     "license": settings.get(session, "license_info")},
         "number": inv.number, "date": timeutil.today_local().isoformat(),
         "due": timeutil.iso(inv.due_on) or "on receipt", "customer": {"name": job.customer.name},
         "job_name": job.name, "job_ref": ref("job", job.id),
         "lines": [{"description": ln["description"], "amount": fmt(D(ln["amount"]))} for ln in inv.lines],
         "subtotal": fmt(inv.subtotal), "retainage_pct": f"{(inv.retainage_pct * 100).normalize()}%",
         "retainage_held": fmt(inv.retainage_held), "total_due": fmt(inv.total_due), "synthetic": inv.is_synthetic}
    return documents.store(session, actor, pdf.render_invoice(c), filename=f"invoice-{inv.number}.pdf",
                           kind="invoice_pdf", title=f"Invoice {inv.number}", job_id=job.id, synthetic=inv.is_synthetic)


# ------------------------------------------------------------------ payments


def payment_view(p: Payment) -> dict[str, Any]:
    return {"ref": ref("payment", p.id), "job": ref("job", p.job_id), "invoice": ref("invoice", p.invoice_id),
            "amount": str(p.amount), "received_on": timeutil.iso(p.received_on), "method": p.method,
            "status": p.status, "verification_source": p.verification_source,
            "applies_to_retainage": p.applies_to_retainage, "synthetic": p.is_synthetic}


def record_payment(session: Session, actor: Actor, *, job_id: int, amount: Any, received_on: str,
                   invoice_id: int | None = None, method: str | None = None, verification_source: str | None = None,
                   applies_to_retainage: bool = False, external_system: str | None = None,
                   external_id: str | None = None) -> dict[str, Any]:
    """Record a payment. Only owner/office with a verification source can mark it verified;
    anything else (including the agent relaying "customer says paid") is reported_unverified."""
    require(actor, "write:billing")
    job = jobs.get(session, actor, job_id)
    amt = q2(D(amount))
    if amt <= 0:
        raise ValidationFailed("amount must be positive")
    if external_system and external_id:
        dup = session.scalar(select(Payment).where(Payment.external_system == external_system,
                                                   Payment.external_id == external_id))
        if dup is not None:
            return {"payment": payment_view(dup), "duplicate": True}
    if invoice_id is not None:
        inv = get_invoice(session, actor, invoice_id)
        if inv.job_id != job.id:
            raise ValidationFailed("invoice belongs to a different job")
        if inv.status not in ("issued", "partially_paid", "paid"):
            raise InvalidTransition(f"payments apply to issued invoices (this one is {inv.status})")
    verified = bool(verification_source) and actor.can("payment:verify")
    p = Payment(job_id=job.id, invoice_id=invoice_id, amount=amt, received_on=dt.date.fromisoformat(received_on),
                method=method, status="verified" if verified else "reported_unverified",
                verification_source=verification_source if verified else None, applies_to_retainage=applies_to_retainage,
                external_system=external_system, external_id=external_id, is_synthetic=job.is_synthetic,
                created_by_id=actor.user_id)
    session.add(p)
    session.flush()
    if invoice_id is not None:
        _refresh_invoice_status(session, invoice_id)
    audit.record(session, actor, "payment.record", "payment", p.id, amount=str(amt), status=p.status,
                 source=verification_source)
    note = None if verified else "Recorded as REPORTED, not collected cash. Owner must verify against a deposit."
    return {"payment": payment_view(p), "duplicate": False, "note": note}


def verify_payment(session: Session, actor: Actor, payment_id: int, verification_source: str) -> dict[str, Any]:
    require(actor, "payment:verify")
    p = session.get(Payment, payment_id)
    if p is None:
        raise NotFound("payment not found")
    if p.status != "reported_unverified":
        raise InvalidTransition(f"payment is {p.status}")
    if not verification_source or len(verification_source.strip()) < 5:
        raise ValidationFailed("verification source required (e.g. bank deposit ref)")
    p.status = "verified"
    p.verification_source = verification_source.strip()[:300]
    if p.invoice_id:
        _refresh_invoice_status(session, p.invoice_id)
    audit.record(session, actor, "payment.verify", "payment", p.id, source=verification_source)
    return payment_view(p)


def _refresh_invoice_status(session: Session, invoice_id: int) -> None:
    inv = session.get(Invoice, invoice_id)
    if inv.status not in ("issued", "partially_paid", "paid"):
        return
    paid = session.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.invoice_id == inv.id, Payment.status.in_(("verified", "reconciled")),
        Payment.applies_to_retainage.is_(False))) or ZERO
    if paid >= inv.total_due:
        inv.status = "paid"
    elif paid > 0:
        inv.status = "partially_paid"
    else:
        inv.status = "issued"


def job_financials(session: Session, actor: Actor, job_id: int) -> dict[str, Any]:
    require(actor, "read:financial")
    job = jobs.get(session, actor, job_id)
    approved_cos = session.scalar(select(func.coalesce(func.sum(ChangeOrder.price), 0)).where(
        ChangeOrder.job_id == job.id, ChangeOrder.status == "customer_approved")) or ZERO
    pending_cos = session.scalar(select(func.coalesce(func.sum(ChangeOrder.price), 0)).where(
        ChangeOrder.job_id == job.id, ChangeOrder.status.in_(("draft", "pending_approval", "approved_to_issue", "issued")))) or ZERO
    issued = list(session.scalars(select(Invoice).where(Invoice.job_id == job.id,
                                                        Invoice.status.in_(("issued", "partially_paid", "paid")))))
    invoiced = sum((i.subtotal for i in issued), ZERO)
    retainage_held = sum((i.retainage_held for i in issued), ZERO)
    drafts = session.scalar(select(func.coalesce(func.sum(Invoice.total_due), 0)).where(
        Invoice.job_id == job.id, Invoice.status.in_(("draft", "pending_approval", "approved")))) or ZERO
    pays = list(session.scalars(select(Payment).where(Payment.job_id == job.id, Payment.status != "reversed")))
    collected = sum((p.amount for p in pays if p.status in ("verified", "reconciled")), ZERO)
    reported = sum((p.amount for p in pays if p.status == "reported_unverified"), ZERO)
    retainage_collected = sum((p.amount for p in pays if p.status in ("verified", "reconciled") and p.applies_to_retainage), ZERO)
    approved_contract = job.contract_value + approved_cos
    return {
        "job": ref("job", job.id),
        "original_contract": str(job.contract_value),
        "approved_change_orders": str(q2(approved_cos)),
        "approved_contract_value": str(q2(approved_contract)),
        "pending_change_orders_not_revenue": str(q2(pending_cos)),
        "invoiced": str(q2(invoiced)),
        "draft_invoices_not_invoiced": str(q2(drafts)),
        "collected_cash_verified": str(q2(collected)),
        "reported_payments_unverified": str(q2(reported)),
        "retainage_held": str(q2(retainage_held)),
        "retainage_receivable": str(q2(retainage_held - retainage_collected)),
        "accounts_receivable": str(q2(invoiced - collected)),
        "left_to_invoice": str(q2(approved_contract - invoiced)),
        "definitions": "Collected cash counts verified payments only; pending change orders are not revenue.",
    }


def receivables(session: Session, actor: Actor, include_synthetic: bool = False) -> list[dict[str, Any]]:
    require(actor, "read:financial")
    q = select(Invoice).where(Invoice.status.in_(("issued", "partially_paid"))).order_by(Invoice.due_on.asc().nulls_last())
    if not include_synthetic:
        q = q.where(Invoice.is_synthetic.is_(False))
    out = []
    today = timeutil.today_local()
    for inv in session.scalars(q):
        paid = session.scalar(select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.invoice_id == inv.id, Payment.status.in_(("verified", "reconciled")),
            Payment.applies_to_retainage.is_(False))) or ZERO
        open_amt = inv.total_due - paid
        out.append({"invoice": ref("invoice", inv.id), "number": inv.number, "job": ref("job", inv.job_id),
                    "open_amount": str(q2(open_amt)), "due_on": timeutil.iso(inv.due_on),
                    "days_overdue": (today - inv.due_on).days if inv.due_on and inv.due_on < today else 0})
    return out


def ensure_issue_ready(inv: Invoice) -> None:
    if inv.content_hash != _invoice_hash(inv):
        raise Conflict("invoice content changed; re-draft before issuing")
