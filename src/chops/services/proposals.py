"""Customer proposals: immutable snapshots of a locked estimate revision."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import pdf, timeutil
from ..authz import Actor, require
from ..errors import Blocked, InvalidTransition, NotFound, ValidationFailed
from ..hashing import content_hash, jsonable
from ..models import Estimate, EstimateRevision, Lead, Proposal
from ..money import ZERO, D, allocate, fmt, pct
from ..refs import ref
from . import audit, documents, estimates, leads, settings

GROUP_LABELS = {
    "labor": "Labor", "material": "Materials", "subcontract": "Subcontracted work",
    "equipment": "Equipment, delivery and disposal", "delivery": "Equipment, delivery and disposal",
    "disposal": "Equipment, delivery and disposal", "allowance": "Allowances", "other": "Other",
}


def proposal_view(p: Proposal, *, include_content: bool = False) -> dict[str, Any]:
    out = {"ref": ref("proposal", p.id), "id": p.id, "estimate": ref("estimate", p.estimate_id),
           "estimate_revision_id": p.estimate_revision_id, "proposal_no": p.proposal_no, "status": p.status,
           "total": str(p.total), "content_hash": p.content_hash, "pdf": ref("document", p.pdf_document_id),
           "approval": ref("approval", p.approval_id), "issued_at": timeutil.iso(p.issued_at),
           "accepted_at": timeutil.iso(p.accepted_at), "acceptance_evidence": p.acceptance_evidence,
           "quote_type": p.content.get("quote_type"), "valid_until": p.content.get("valid_until"),
           "synthetic": p.is_synthetic}
    if include_content:
        out["content"] = p.content
    return out


def get(session: Session, actor: Actor, proposal_id: int) -> Proposal:
    require(actor, "read:financial")
    p = session.get(Proposal, proposal_id)
    if p is None:
        raise NotFound(f"{ref('proposal', proposal_id)} not found")
    return p


def _milestones(session: Session, total: Decimal) -> tuple[list[dict[str, Any]], list[str]]:
    terms = settings.get(session, "commercial_terms") or {}
    ms = terms.get("payment_milestones") or []
    problems = []
    if not ms:
        problems.append("payment milestones not configured (commercial_terms.payment_milestones)")
        return [], problems
    fracs = [pct(m["pct"]) for m in ms]
    if sum(fracs) != Decimal("1"):
        problems.append(f"payment milestones sum to {sum(fracs)} instead of 100%")
        return [], problems
    amounts = allocate(total, fracs)
    # allocate() puts rounding remainder on the largest share; keep it deterministic.
    return [{"key": m["key"], "label": m["label"], "pct": str(f), "pct_display": f"{(f * 100).normalize()}%",
             "amount": fmt(a), "amount_value": str(a)} for m, f, a in zip(ms, fracs, amounts)], problems


def build_content(session: Session, rev: EstimateRevision, presentation: str) -> dict[str, Any]:
    est = rev.estimate
    totals = rev.totals or {}
    lead = session.get(Lead, est.lead_id) if est.lead_id else None
    terms = settings.get(session, "commercial_terms") or {}
    price_before = D(totals["price_before_discount"])
    rows = totals["lines"]
    items = {i.line_no: i for i in rev.items}
    priced = [r for r in rows if r["state"] == "priced"]
    if presentation == "line_items":
        amounts = allocate(price_before, [D(r["cost"]) for r in priced])
        lines = [{"text": (items[r["line"]].customer_visible_text or r["description"]), "amount": fmt(a)}
                 for r, a in zip(priced, amounts)]
    else:
        groups: dict[str, Decimal] = {}
        for r in priced:
            label = GROUP_LABELS.get(r["kind"], "Other")
            groups[label] = groups.get(label, ZERO) + D(r["cost"])
        labels = list(groups)
        amounts = allocate(price_before, [groups[k] for k in labels])
        lines = [{"text": k, "amount": fmt(a)} for k, a in zip(labels, amounts)]
    allowances = [f"{(items[r['line']].customer_visible_text or r['description'])}"
                  for r in priced if r["kind"] == "allowance"]
    assumptions = list(rev.assumptions or [])
    for name, dim in (rev.dimensions or {}).items():
        if dim.get("source") not in ("field_measured", "plans"):
            label = dim.get("label") or name
            assumptions.append(f"{label}: {dim.get('value')} {dim.get('unit') or ''} ({dim.get('source')}; to be field-verified)")
    unpriced = [r["description"] for r in rows if r["state"] == "missing"]
    conditions = list(rev.unresolved_conditions or [])
    for u in unpriced:
        conditions.append({"condition": f"Not yet priced: {u}", "resolution": None})
    exclusions = list(rev.exclusions or []) + [f"Customer-supplied: {x}" for x in totals.get("excluded_customer_supplied", [])]
    total = D(totals["total"])
    milestones, _ = _milestones(session, total)
    validity_days = terms.get("validity_days")
    today = timeutil.today_local()
    site = lead.site if lead else None
    contact = settings.get(session, "company_contact")
    lic = settings.get(session, "license_info")
    return jsonable({
        "company": {"name": settings.company_display(session), "contact": contact, "license": lic},
        "customer": {"name": lead.contact.name if lead else "[customer]",
                     "site": f"{site.address_line}{', ' + site.city if site and site.city else ''}" if site else None},
        "title": est.title, "scope_summary": rev.scope_summary, "presentation": presentation,
        "estimate_ref": ref("estimate", est.id), "revision": rev.revision_no, "pricing_mode": est.pricing_mode,
        "quote_type": rev.quote_type, "range": totals.get("rough_range") and {
            "low": fmt(D(totals["rough_range"]["low"])), "high": fmt(D(totals["rough_range"]["high"]))},
        "lines": lines, "subtotal": fmt(price_before), "discount": fmt(D(totals["discount"] or 0)),
        "sales_tax": fmt(D(totals["sales_tax"] or 0)), "total": fmt(total), "total_value": str(total),
        "inclusions": rev.inclusions or [], "exclusions": exclusions, "allowances": allowances,
        "assumptions": assumptions, "conditions": conditions, "milestones": milestones,
        "date": today.isoformat(),
        "valid_until": (today + dt.timedelta(days=int(validity_days))).isoformat() if validity_days else None,
        "terms_text": terms.get("terms_text"), "warranty_text": terms.get("warranty_text"),
        "synthetic": est.is_synthetic, "estimate_revision_hash": rev.content_hash,
    })


def create_from_estimate(session: Session, actor: Actor, estimate_id: int, presentation: str = "summary") -> dict[str, Any]:
    require(actor, "write:estimate")
    if presentation not in ("summary", "line_items"):
        raise ValidationFailed("presentation must be summary or line_items")
    est = session.get(Estimate, estimate_id)
    if est is None:
        raise NotFound(f"{ref('estimate', estimate_id)} not found")
    rev = estimates.current_revision(session, est)
    if rev.status == "superseded":
        raise InvalidTransition("current revision is superseded")
    existing = session.scalar(select(Proposal).where(Proposal.estimate_revision_id == rev.id,
                                                     Proposal.status.not_in(("superseded", "void", "declined"))))
    if existing is not None:
        return {"proposal": proposal_view(existing), "reused": True}
    totals = estimates.compute_revision(session, rev)
    if totals["total"] is None:
        raise Blocked("cannot build a proposal without a price", blockers=totals["firm_blockers"])
    if rev.quote_type == "firm" and not totals["firm_quote_ready"]:
        raise Blocked("firm quote blocked; resolve these or switch to a rough range", blockers=totals["firm_blockers"])
    estimates.lock_revision(session, actor, rev)
    content = build_content(session, rev, presentation)
    chash = content_hash(content)
    # Older live proposals of this estimate are superseded by the new revision.
    for old in session.scalars(select(Proposal).where(Proposal.estimate_id == est.id,
                                                      Proposal.status.in_(("draft", "pending_approval", "approved", "issued")))):
        old.status = "superseded"
    no = (session.scalar(select(func.max(Proposal.proposal_no)).where(Proposal.estimate_id == est.id)) or 0) + 1
    p = Proposal(estimate_id=est.id, estimate_revision_id=rev.id, proposal_no=no, status="draft", content=content,
                 content_hash=chash, total=D(content["total_value"]), is_synthetic=est.is_synthetic,
                 created_by_id=actor.user_id)
    session.add(p)
    session.flush()
    pdf_bytes = pdf.render_proposal({**content, "proposal_ref": ref("proposal", p.id), "content_hash": chash})
    doc = documents.store(session, actor, pdf_bytes, filename=f"proposal-{p.id}.pdf", kind="proposal_pdf",
                          title=f"Proposal {ref('proposal', p.id)} ({est.title})", lead_id=est.lead_id,
                          synthetic=est.is_synthetic)
    p.pdf_document_id = doc["id"]
    est.status = "proposed"
    if est.lead_id:
        _advance_lead(session, actor, est.lead_id, "proposal")
    session.flush()
    audit.record(session, actor, "proposal.create", "proposal", p.id, estimate=est.id, revision=rev.revision_no,
                 content_hash=chash, total=str(p.total))
    return {"proposal": proposal_view(p), "reused": False}


def _advance_lead(session: Session, actor: Actor, lead_id: int, target: str) -> None:
    lead = session.get(Lead, lead_id)
    if lead is None or lead.status == target:
        return
    path = {"proposal": ["estimating", "proposal"], "won": ["won"]}[target]
    for step in path:
        if lead.status == step:
            continue
        if step in leads.TRANSITIONS.get(lead.status, set()):
            leads.transition(session, actor, lead_id, step, reason="automatic: proposal workflow")


def request_issue(session: Session, actor: Actor, proposal_id: int) -> dict[str, Any]:
    from . import approvals

    p = get(session, actor, proposal_id)
    if p.status not in ("draft", "pending_approval"):
        raise InvalidTransition(f"{ref('proposal', p.id)} is {p.status}")
    terms = settings.get(session, "commercial_terms") or {}
    _, problems = _milestones(session, p.total)
    if problems or not terms.get("validity_days"):
        raise Blocked("owner-approved commercial terms are required before a proposal can be issued",
                      missing=problems + ([] if terms.get("validity_days") else ["commercial_terms.validity_days"]))
    if p.content.get("company", {}).get("name", "").startswith("[Company name"):
        raise Blocked("company name is not configured")
    res = approvals.request(session, actor, "proposal.issue", p.id, synthetic=p.is_synthetic)
    p.status = "pending_approval"
    return res


def mark_issued(session: Session, actor: Actor, proposal_id: int, evidence: str) -> dict[str, Any]:
    """Record manual delivery (e.g. handed over at site visit). Evidence required."""
    require(actor, "payment:verify")  # owner/office only
    p = get(session, actor, proposal_id)
    if p.status != "approved":
        raise InvalidTransition(f"only an approved proposal can be marked issued ({p.status})")
    if not evidence or len(evidence.strip()) < 5:
        raise ValidationFailed("delivery evidence is required")
    p.status = "issued"
    p.issued_at = timeutil.now()
    audit.record(session, actor, "proposal.issued_manual", "proposal", p.id, evidence=evidence)
    return proposal_view(p)


def record_acceptance(session: Session, actor: Actor, proposal_id: int, evidence: str,
                      signed_document_id: int | None = None, accept_after_expiry: bool = False) -> dict[str, Any]:
    """Owner/office record of customer acceptance. The agent must request approval instead."""
    from . import jobs

    require(actor, "payment:verify")
    p = get(session, actor, proposal_id)
    if p.status not in ("approved", "issued"):
        raise InvalidTransition(f"{ref('proposal', p.id)} is {p.status}; only an approved/issued proposal can be accepted")
    if p.content.get("quote_type") != "firm":
        raise Blocked("a rough-range proposal cannot be accepted as a contract; issue a firm revision")
    vu = p.content.get("valid_until")
    if vu and dt.date.fromisoformat(vu) < timeutil.today_local() and not accept_after_expiry:
        raise Blocked(f"proposal expired {vu}; confirm acceptance after expiry explicitly")
    if not evidence or len(evidence.strip()) < 5:
        raise ValidationFailed("acceptance evidence is required (signed PDF, email, etc.)")
    p.status = "accepted"
    p.accepted_at = timeutil.now()
    p.acceptance_evidence = evidence.strip()[:500]
    p.acceptance_document_id = signed_document_id
    est = session.get(Estimate, p.estimate_id)
    est.status = "accepted"
    if est.lead_id:
        _advance_lead(session, actor, est.lead_id, "won")
    session.flush()
    audit.record(session, actor, "proposal.accepted", "proposal", p.id, evidence=evidence, content_hash=p.content_hash)
    job = jobs.create_from_proposal(session, actor, p.id)
    return {"proposal": proposal_view(p), "job": job}


def list_proposals(session: Session, actor: Actor, status: str | None = None, include_synthetic: bool = False) -> list[dict[str, Any]]:
    require(actor, "read:financial")
    q = select(Proposal).order_by(Proposal.id.desc()).limit(100)
    if status:
        q = q.where(Proposal.status == status)
    if not include_synthetic:
        q = q.where(Proposal.is_synthetic.is_(False))
    return [proposal_view(p) for p in session.scalars(q)]
