"""Approval-gated actions. Each ``build`` derives the payload from current records (so any
edit changes the hash); each ``execute`` performs internal changes and enqueues external
effects through the outbox (sent only after commit, only in LIVE, never for synthetic data).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .. import timeutil
from ..authz import Actor
from ..errors import InvalidTransition, NotFound, ValidationFailed
from ..models import Approval, ChangeOrder, Contact, Invoice, Job, Proposal, PurchaseOrder
from ..refs import ref
from . import approvals, billing, outbox, settings
from .approvals import ActionSpec


def _contact(session: Session, contact_id: int) -> Contact:
    c = session.get(Contact, contact_id)
    if c is None:
        raise NotFound("contact not found")
    return c


# ------------------------------------------------------------------ proposal.issue


def _build_proposal_issue(session: Session, proposal_id: int, extra: dict[str, Any]) -> dict[str, Any]:
    p = session.get(Proposal, proposal_id)
    if p is None:
        raise NotFound("proposal not found")
    if p.status not in ("draft", "pending_approval"):
        raise InvalidTransition(f"proposal is {p.status}")
    from ..models import Estimate, Lead

    est = session.get(Estimate, p.estimate_id)
    lead = session.get(Lead, est.lead_id) if est.lead_id else None
    email = lead.contact.email if lead else None
    return {
        "payload": {"proposal": ref("proposal", p.id), "content_hash": p.content_hash, "total": str(p.total),
                    "customer": p.content["customer"], "quote_type": p.content.get("quote_type"),
                    "milestones": p.content.get("milestones"), "valid_until": p.content.get("valid_until"),
                    "pdf": ref("document", p.pdf_document_id),
                    "delivery": "email" if email else "manual (no customer email on file)"},
        "target_revision": f"rev{p.estimate_revision_id}:{p.content_hash[:16]}",
        "summary": f"Issue {ref('proposal', p.id)} to {p.content['customer']['name']} for ${p.total:,.2f}",
        "destination": email or "manual delivery",
        "amount": str(p.total),
    }


def _exec_proposal_issue(session: Session, a: Approval, actor: Actor) -> dict[str, Any]:
    p = session.get(Proposal, a.target_id)
    p.status = "approved"
    p.approval_id = a.id
    result: dict[str, Any] = {"proposal": ref("proposal", p.id), "status": "approved"}
    if a.destination and a.destination != "manual delivery":
        job = outbox.enqueue(session, kind="email.send", external_effect=True, approval_id=a.id,
                             idempotency_key=f"approval-{a.id}-proposal-email", synthetic=p.is_synthetic,
                             payload={"to": a.destination,
                                      "subject": f"Proposal from {settings.company_display(session)}",
                                      "body": f"Hello {p.content['customer']['name']},\n\nAttached is our proposal "
                                              f"({ref('proposal', p.id)}). Please let us know if you have questions.\n",
                                      "attachments": [p.pdf_document_id]})
        result["delivery"] = {"outbox": ref("outbox", job.id), "note": "queued; sent only in LIVE mode"}
    else:
        result["delivery"] = {"manual": True, "note": "Download the PDF and deliver it; then mark it issued with evidence."}
    return result


# ------------------------------------------------------------------ proposal.record_acceptance (agent-requested)


def _build_record_acceptance(session: Session, proposal_id: int, extra: dict[str, Any]) -> dict[str, Any]:
    p = session.get(Proposal, proposal_id)
    if p is None:
        raise NotFound("proposal not found")
    if p.status not in ("approved", "issued"):
        raise InvalidTransition(f"proposal is {p.status}")
    evidence = (extra or {}).get("evidence")
    if not evidence:
        raise ValidationFailed("evidence required")
    return {"payload": {"proposal": ref("proposal", p.id), "content_hash": p.content_hash, "total": str(p.total),
                        "evidence": evidence, "_extra": {"evidence": evidence}},
            "target_revision": f"rev{p.estimate_revision_id}:{p.content_hash[:16]}",
            "summary": f"Record customer acceptance of {ref('proposal', p.id)} (${p.total:,.2f}) and create the job",
            "destination": None, "amount": str(p.total)}


def _exec_record_acceptance(session: Session, a: Approval, actor: Actor) -> dict[str, Any]:
    from . import proposals

    owner = Actor(user_id=a.decided_by_id, role="owner", via=actor.via, display_name=actor.display_name)
    res = proposals.record_acceptance(session, owner, a.target_id, a.payload["evidence"])
    return {"proposal": res["proposal"]["ref"], "job": res["job"]["ref"]}


# ------------------------------------------------------------------ invoice.issue


def _build_invoice_issue(session: Session, invoice_id: int, extra: dict[str, Any]) -> dict[str, Any]:
    inv = session.get(Invoice, invoice_id)
    if inv is None:
        raise NotFound("invoice not found")
    if inv.status not in ("draft", "pending_approval"):
        raise InvalidTransition(f"invoice is {inv.status}")
    billing.ensure_issue_ready(inv)
    job = session.get(Job, inv.job_id)
    email = job.customer.email
    return {"payload": {"invoice": ref("invoice", inv.id), "number": inv.number, "job": ref("job", job.id),
                        "customer": job.customer.name, "lines": inv.lines, "total_due": str(inv.total_due),
                        "retainage_held": str(inv.retainage_held), "due_on": timeutil.iso(inv.due_on),
                        "content_hash": inv.content_hash,
                        "delivery": "email" if email else "manual (no customer email on file)"},
            "target_revision": f"inv:{inv.content_hash[:16]}",
            "summary": f"Issue invoice {inv.number} to {job.customer.name} for ${inv.total_due:,.2f}",
            "destination": email or "manual delivery", "amount": str(inv.total_due)}


def _exec_invoice_issue(session: Session, a: Approval, actor: Actor) -> dict[str, Any]:
    inv = session.get(Invoice, a.target_id)
    inv.status = "issued"
    inv.issued_on = timeutil.today_local()
    doc = billing.render_invoice_pdf(session, actor if actor.user_id else Actor(a.decided_by_id, "owner", "worker"), inv)
    result: dict[str, Any] = {"invoice": ref("invoice", inv.id), "status": "issued", "pdf": doc["ref"]}
    if a.destination and a.destination != "manual delivery":
        job = outbox.enqueue(session, kind="email.send", external_effect=True, approval_id=a.id,
                             idempotency_key=f"approval-{a.id}-invoice-email", synthetic=inv.is_synthetic,
                             payload={"to": a.destination, "subject": f"Invoice {inv.number}",
                                      "body": f"Attached is invoice {inv.number}. Amount due: ${inv.total_due:,.2f}.",
                                      "attachments": [doc["id"]]})
        result["delivery"] = {"outbox": ref("outbox", job.id), "note": "queued; sent only in LIVE mode"}
    else:
        result["delivery"] = {"manual": True}
    return result


# ------------------------------------------------------------------ message.send


def _build_message(session: Session, contact_id: int, extra: dict[str, Any]) -> dict[str, Any]:
    c = _contact(session, contact_id)
    channel = extra.get("channel", "email")
    body = (extra.get("body") or "").strip()
    if not body:
        raise ValidationFailed("message body required")
    if channel == "email":
        dest = c.email
        if not dest:
            raise ValidationFailed(f"{c.name} has no email on file")
    elif channel == "sms":
        dest = c.phone
        if not dest:
            raise ValidationFailed(f"{c.name} has no phone on file")
    else:
        raise ValidationFailed("channel must be email or sms")
    subject = (extra.get("subject") or "").strip()[:200]
    return {"payload": {"channel": channel, "to_name": c.name, "subject": subject, "body": body[:5000],
                        "related": extra.get("related"), "_extra": {"channel": channel, "body": body, "subject": subject,
                                                                    "related": extra.get("related")}},
            "target_revision": f"contact{c.id}",
            "summary": f"Send {channel} to {c.name}: {body[:80]}",
            "destination": dest, "amount": None}


def _exec_message(session: Session, a: Approval, actor: Actor) -> dict[str, Any]:
    c = _contact(session, a.target_id)
    kind = "email.send" if a.payload["channel"] == "email" else "sms.send"
    job = outbox.enqueue(session, kind=kind, external_effect=True, approval_id=a.id,
                         idempotency_key=f"approval-{a.id}-message", synthetic=c.is_synthetic,
                         payload={"to": a.destination, "subject": a.payload.get("subject") or "Message",
                                  "body": a.payload["body"]})
    return {"outbox": ref("outbox", job.id), "note": "queued; sent only in LIVE mode with a connected provider"}


# ------------------------------------------------------------------ change_order.issue / purchase_order.issue


def _build_co_issue(session: Session, co_id: int, extra: dict[str, Any]) -> dict[str, Any]:
    from . import change_orders

    co = session.get(ChangeOrder, co_id)
    if co is None:
        raise NotFound("change order not found")
    if co.status not in ("draft", "pending_approval"):
        raise InvalidTransition(f"change order is {co.status}")
    change_orders.ensure_hash(co)
    job = session.get(Job, co.job_id)
    return {"payload": {"change_order": ref("change_order", co.id), "number": co.number, "revision": co.revision_no,
                        "job": ref("job", job.id), "title": co.title, "scope": co.scope, "price": str(co.price),
                        "schedule_impact_days": co.schedule_impact_days, "content_hash": co.content_hash},
            "target_revision": f"r{co.revision_no}:{co.content_hash[:16]}",
            "summary": f"Issue change order #{co.number} r{co.revision_no} on {job.name} for ${co.price:,.2f}",
            "destination": job.customer.email or "manual delivery", "amount": str(co.price)}


def _exec_co_issue(session: Session, a: Approval, actor: Actor) -> dict[str, Any]:
    co = session.get(ChangeOrder, a.target_id)
    co.status = "approved_to_issue"
    return {"change_order": ref("change_order", co.id), "status": co.status,
            "note": "Present to customer; record customer approval with evidence before it counts as revenue."}


def _build_po_issue(session: Session, po_id: int, extra: dict[str, Any]) -> dict[str, Any]:
    from . import procurement

    po = session.get(PurchaseOrder, po_id)
    if po is None:
        raise NotFound("purchase order not found")
    if po.status not in ("draft", "pending_approval"):
        raise InvalidTransition(f"PO is {po.status}")
    procurement.ensure_hash(po)
    vendor = _contact(session, po.vendor_id)
    return {"payload": {"purchase_order": ref("purchase_order", po.id), "number": po.number, "vendor": vendor.name,
                        "job": ref("job", po.job_id), "lines": [{"description": ln.description, "qty": str(ln.quantity),
                                                                 "unit": ln.unit, "unit_cost": str(ln.unit_cost),
                                                                 "amount": str(ln.amount), "cost_code": ln.cost_code}
                                                                for ln in po.lines],
                        "total": str(po.total), "needed_by": timeutil.iso(po.needed_by), "content_hash": po.content_hash},
            "target_revision": f"po:{po.content_hash[:16]}",
            "summary": f"Issue PO {po.number} to {vendor.name} for ${po.total:,.2f}",
            "destination": vendor.email or "manual delivery", "amount": str(po.total)}


def _exec_po_issue(session: Session, a: Approval, actor: Actor) -> dict[str, Any]:
    po = session.get(PurchaseOrder, a.target_id)
    po.status = "approved"
    result: dict[str, Any] = {"purchase_order": ref("purchase_order", po.id), "status": "approved"}
    if a.destination and a.destination != "manual delivery":
        lines = "\n".join(f"- {ln.description}: {ln.quantity} {ln.unit} @ ${ln.unit_cost}" for ln in po.lines)
        job = outbox.enqueue(session, kind="email.send", external_effect=True, approval_id=a.id,
                             idempotency_key=f"approval-{a.id}-po-email", synthetic=po.is_synthetic,
                             payload={"to": a.destination, "subject": f"Purchase order {po.number}",
                                      "body": f"Please confirm availability and delivery for PO {po.number}:\n{lines}\n"})
        result["delivery"] = {"outbox": ref("outbox", job.id)}
    return result


approvals.register("proposal.issue", ActionSpec("proposal", _build_proposal_issue, _exec_proposal_issue))
approvals.register("proposal.record_acceptance",
                   ActionSpec("proposal", _build_record_acceptance, _exec_record_acceptance, allow_standing_policy=False))
approvals.register("invoice.issue", ActionSpec("invoice", _build_invoice_issue, _exec_invoice_issue))
approvals.register("message.send", ActionSpec("contact", _build_message, _exec_message))
approvals.register("change_order.issue", ActionSpec("change_order", _build_co_issue, _exec_co_issue))
approvals.register("purchase_order.issue", ActionSpec("purchase_order", _build_po_issue, _exec_po_issue,
                                                      money_movement=True))
