"""Lead capture, deduplication, qualification pipeline, site visits, follow-ups."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import timeutil
from ..authz import Actor, require
from ..errors import InvalidTransition, NotFound, ValidationFailed
from ..hashing import jsonable
from ..models import Appointment, Contact, IntegrationEvent, Lead, LeadTransition, Site
from ..refs import ref
from . import audit

TRANSITIONS: dict[str, set[str]] = {
    "inquiry": {"qualified", "site_visit", "estimating", "lost"},
    "qualified": {"site_visit", "estimating", "lost"},
    "site_visit": {"estimating", "lost"},
    "estimating": {"proposal", "site_visit", "lost"},
    "proposal": {"follow_up", "won", "lost", "estimating"},
    "follow_up": {"proposal", "won", "lost", "estimating"},
    "won": set(),
    "lost": {"inquiry"},
}
REASON_REQUIRED = {"lost", "inquiry"}
CLOSED = {"won", "lost"}

_STREET = {"street": "st", "road": "rd", "avenue": "ave", "drive": "dr", "lane": "ln", "court": "ct",
           "boulevard": "blvd", "circle": "cir", "place": "pl", "parkway": "pkwy", "highway": "hwy",
           "north": "n", "south": "s", "east": "e", "west": "w", "terrace": "ter", "trail": "trl"}


def normalize_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) >= 7 else None


def normalize_email(email: str | None) -> str | None:
    if not email:
        return None
    e = email.strip().lower()
    return e if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e) else None


def normalize_address(addr: str) -> str:
    words = re.sub(r"[^\w\s]", " ", addr.lower()).split()
    return " ".join(_STREET.get(w, w) for w in words)


def _name_compatible(a: str, b: str) -> bool:
    ta = {t for t in re.sub(r"[^\w\s]", " ", a.lower()).split() if len(t) > 1}
    tb = {t for t in re.sub(r"[^\w\s]", " ", b.lower()).split() if len(t) > 1}
    return bool(ta & tb)


def lead_view(lead: Lead, *, include_extraction: bool = True) -> dict[str, Any]:
    c, s = lead.contact, lead.site
    out = {
        "ref": ref("lead", lead.id), "id": lead.id, "status": lead.status,
        "customer": {"ref": ref("contact", c.id), "name": c.name, "phone": c.phone, "email": c.email},
        "site": None if s is None else {"ref": ref("site", s.id), "address": s.address_line, "city": s.city,
                                        "state": s.state, "jurisdiction": s.jurisdiction,
                                        "jurisdiction_status": s.jurisdiction_status},
        "job_type": lead.job_type, "scope": lead.scope_text, "timing": lead.timing_text,
        "budget": lead.budget_text, "source": lead.source,
        "next_action": lead.next_action, "next_action_due": timeutil.iso(lead.next_action_due),
        "needs_review": lead.needs_review,
        "possible_duplicate_of": ref("lead", lead.possible_duplicate_of_id),
        "lost_reason": lead.lost_reason, "synthetic": lead.is_synthetic,
        "created_at": timeutil.iso(lead.created_at),
    }
    if include_extraction:
        out["extraction"] = lead.extraction
    missing = [f for f, v in (("address", s), ("phone_or_email", c.phone or c.email), ("job_type", lead.job_type),
                              ("scope", lead.scope_text), ("timing", lead.timing_text)) if not v]
    out["missing_information"] = missing
    return out


def capture(
    session: Session,
    actor: Actor,
    *,
    name: str,
    phone: str | None = None,
    email: str | None = None,
    address: str | None = None,
    city: str | None = None,
    state: str | None = None,
    postal_code: str | None = None,
    job_type: str | None = None,
    scope: str | None = None,
    timing: str | None = None,
    budget: str | None = None,
    source: str = "manual",
    provider: str | None = None,
    provider_event_id: str | None = None,
    extraction: dict[str, Any] | None = None,
    next_action: str | None = None,
    synthetic: bool = False,
) -> dict[str, Any]:
    """Create a lead from any channel. Idempotent on (provider, provider_event_id).

    ``extraction`` carries per-field confidence from the caller (e.g. Hermes reading a voice
    note): {"field": {"value": ..., "confidence": 0..1, "source": "voice_note DOC-4"}}. Any field
    below 0.8 confidence marks the lead for owner review instead of being treated as fact.
    """
    require(actor, "write:crm")
    if not name or not name.strip():
        raise ValidationFailed("customer name is required (use 'Unknown' explicitly if not provided)")

    event: IntegrationEvent | None = None
    if provider_event_id:
        if not provider:
            raise ValidationFailed("provider is required with provider_event_id")
        existing = session.scalar(select(IntegrationEvent).where(
            IntegrationEvent.provider == provider, IntegrationEvent.external_id == provider_event_id))
        if existing is not None and existing.result_id is not None:
            lead = session.get(Lead, existing.result_id)
            return {"duplicate_event": True, "lead": lead_view(lead), "message": "event already processed; no new records"}
        if existing is None:
            event = IntegrationEvent(provider=provider, external_id=provider_event_id, kind="lead",
                                     payload=jsonable({"name": name, "phone": phone, "email": email, "address": address,
                                                       "scope": scope, "source": source}))
            try:
                with session.begin_nested():
                    session.add(event)
                    session.flush()
            except IntegrityError:
                # Concurrent delivery of the same event: the other transaction owns it.
                existing = session.scalar(select(IntegrationEvent).where(
                    IntegrationEvent.provider == provider, IntegrationEvent.external_id == provider_event_id))
                lead = session.get(Lead, existing.result_id) if existing and existing.result_id else None
                return {"duplicate_event": True, "lead": lead_view(lead) if lead else None,
                        "message": "event is being processed by another worker"}
        else:
            event = existing

    pn, em = normalize_phone(phone), normalize_email(email)
    if email and not em:
        raise ValidationFailed(f"email looks invalid: {email!r}")

    dedupe: dict[str, Any] = {"contact": "new", "candidates": []}
    review_reasons: list[str] = []
    candidates: list[Contact] = []
    conds = []
    if pn:
        conds.append(Contact.phone_normalized == pn)
    if em:
        conds.append(Contact.email_normalized == em)
    if conds:
        candidates = list(session.scalars(select(Contact).where(
            or_(*conds), Contact.merged_into_id.is_(None), Contact.is_synthetic.is_(synthetic))))
    contact: Contact | None = None
    if len(candidates) == 1 and _name_compatible(candidates[0].name, name):
        c = candidates[0]
        conflicting = (pn and c.phone_normalized and c.phone_normalized != pn) or (em and c.email_normalized and c.email_normalized != em)
        if not conflicting:
            contact = c
            dedupe["contact"] = "matched_existing"
            dedupe["matched"] = ref("contact", c.id)
    if contact is None and candidates:
        dedupe["contact"] = "new_possible_duplicate"
        dedupe["candidates"] = [{"ref": ref("contact", c.id), "name": c.name} for c in candidates]
        review_reasons.append("contact details overlap an existing contact; not merged automatically")
    if contact is None:
        contact = Contact(kind="customer", name=name.strip(), phone=phone, phone_normalized=pn, email=email,
                          email_normalized=em, is_synthetic=synthetic, created_by_id=actor.user_id)
        session.add(contact)
        session.flush()

    site = None
    if address:
        norm = normalize_address(address)
        site = session.scalar(select(Site).where(Site.address_normalized == norm, Site.contact_id == contact.id))
        if site is None:
            site = Site(contact_id=contact.id, address_line=address.strip(), city=city, state=(state or None),
                        postal_code=postal_code, address_normalized=norm, is_synthetic=synthetic,
                        created_by_id=actor.user_id)
            session.add(site)
            session.flush()

    dup_lead = None
    if site is not None:
        dup_lead = session.scalar(select(Lead).where(Lead.site_id == site.id, Lead.status.not_in(CLOSED)))
        if dup_lead is not None:
            review_reasons.append(f"open lead {ref('lead', dup_lead.id)} exists for the same customer and address")

    low_conf = []
    for field, info in (extraction or {}).items():
        try:
            if isinstance(info, dict) and float(info.get("confidence", 1)) < 0.8:
                low_conf.append(field)
        except (TypeError, ValueError):
            low_conf.append(field)
    if low_conf:
        review_reasons.append("uncertain extraction: " + ", ".join(sorted(low_conf)))

    lead = Lead(
        contact_id=contact.id, site_id=site.id if site else None, status="inquiry", job_type=job_type,
        scope_text=scope, timing_text=timing, budget_text=budget, source=source,
        source_ref=f"{provider}:{provider_event_id}" if provider_event_id else None,
        next_action=next_action or "Review and qualify", next_action_due=timeutil.now() + dt.timedelta(days=1),
        extraction=jsonable(extraction) if extraction else None, needs_review=bool(review_reasons),
        possible_duplicate_of_id=dup_lead.id if dup_lead else None, is_synthetic=synthetic,
        created_by_id=actor.user_id,
    )
    session.add(lead)
    session.flush()
    session.add(LeadTransition(lead_id=lead.id, from_status=None, to_status="inquiry", reason=f"captured via {source}",
                               actor_id=actor.user_id))
    if event is not None:
        event.status = "processed"
        event.result_entity = "lead"
        event.result_id = lead.id
    audit.record(session, actor, "lead.capture", "lead", lead.id, source=source, dedupe=dedupe,
                 review_reasons=review_reasons)
    return {"duplicate_event": False, "lead": lead_view(lead), "dedupe": dedupe, "review_reasons": review_reasons}


def get(session: Session, actor: Actor, lead_id: int) -> Lead:
    require(actor, "read:all")
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise NotFound(f"{ref('lead', lead_id)} not found")
    return lead


def transition(session: Session, actor: Actor, lead_id: int, to_status: str, reason: str | None = None,
               expected_version: int | None = None) -> dict[str, Any]:
    require(actor, "write:crm")
    lead = get(session, actor, lead_id)
    if expected_version is not None and lead.version != expected_version:
        raise InvalidTransition("lead changed since you loaded it; reload and retry",
                                current_version=lead.version)
    if to_status not in TRANSITIONS.get(lead.status, set()):
        raise InvalidTransition(f"cannot move lead from {lead.status} to {to_status}",
                                allowed=sorted(TRANSITIONS.get(lead.status, set())))
    if to_status in REASON_REQUIRED and not (reason and reason.strip()):
        raise ValidationFailed(f"a reason is required to move a lead to {to_status}")
    frm = lead.status
    lead.status = to_status
    if to_status == "lost":
        lead.lost_reason = reason
        lead.next_action = None
        lead.next_action_due = None
    session.add(LeadTransition(lead_id=lead.id, from_status=frm, to_status=to_status, reason=reason, actor_id=actor.user_id))
    session.flush()
    audit.record(session, actor, "lead.transition", "lead", lead.id, frm=frm, to=to_status, reason=reason)
    return lead_view(lead)


def update(session: Session, actor: Actor, lead_id: int, **fields: Any) -> dict[str, Any]:
    """Correct extracted fields. Clears ``needs_review`` only when the owner says so."""
    require(actor, "write:crm")
    lead = get(session, actor, lead_id)
    allowed = {"job_type": "job_type", "scope": "scope_text", "timing": "timing_text", "budget": "budget_text",
               "next_action": "next_action"}
    changes = {}
    for k, v in fields.items():
        if k in allowed and v is not None:
            changes[k] = {"old": getattr(lead, allowed[k]), "new": v}
            setattr(lead, allowed[k], v)
    if "next_action_due" in fields and fields["next_action_due"]:
        lead.next_action_due = timeutil.parse_local(fields["next_action_due"])
        changes["next_action_due"] = fields["next_action_due"]
    if fields.get("mark_reviewed"):
        lead.needs_review = False
        changes["needs_review"] = False
    if not changes:
        raise ValidationFailed("no recognized fields to update")
    session.flush()
    audit.record(session, actor, "lead.update", "lead", lead.id, changes=changes)
    return lead_view(lead)


def history(session: Session, actor: Actor, lead_id: int) -> list[dict[str, Any]]:
    get(session, actor, lead_id)
    rows = session.scalars(select(LeadTransition).where(LeadTransition.lead_id == lead_id).order_by(LeadTransition.id))
    return [{"from": r.from_status, "to": r.to_status, "reason": r.reason, "at": timeutil.iso(r.at)} for r in rows]


def list_leads(session: Session, actor: Actor, *, status: str | None = None, overdue_only: bool = False,
               include_closed: bool = False, include_synthetic: bool = False, limit: int = 50) -> list[dict[str, Any]]:
    require(actor, "read:all")
    q = select(Lead).order_by(Lead.next_action_due.asc().nulls_last(), Lead.id.desc()).limit(min(limit, 200))
    if status:
        q = q.where(Lead.status == status)
    elif not include_closed:
        q = q.where(Lead.status.not_in(CLOSED))
    if not include_synthetic:
        q = q.where(Lead.is_synthetic.is_(False))
    if overdue_only:
        q = q.where(Lead.next_action_due < timeutil.now())
    return [lead_view(x, include_extraction=False) for x in session.scalars(q)]


# ------------------------------------------------------------------ site visits


def schedule_site_visit(session: Session, actor: Actor, lead_id: int, starts_at: str, duration_minutes: int = 60,
                        notes: str | None = None) -> dict[str, Any]:
    require(actor, "write:crm")
    lead = get(session, actor, lead_id)
    start = timeutil.parse_local(starts_at)
    appt = Appointment(lead_id=lead.id, kind="site_visit", starts_at=start,
                       ends_at=start + dt.timedelta(minutes=duration_minutes), status="tentative", notes=notes,
                       is_synthetic=lead.is_synthetic, created_by_id=actor.user_id)
    session.add(appt)
    session.flush()
    audit.record(session, actor, "appointment.tentative", "appointment", appt.id, lead=lead.id)
    return appointment_view(appt)


def confirm_appointment(session: Session, actor: Actor, appointment_id: int, evidence: str) -> dict[str, Any]:
    """Confirmed only with evidence (e.g. 'customer replied YES by SMS 10/3 4:12pm')."""
    require(actor, "write:crm")
    appt = session.get(Appointment, appointment_id)
    if appt is None:
        raise NotFound("appointment not found")
    if not evidence or len(evidence.strip()) < 5:
        raise ValidationFailed("confirmation evidence is required")
    appt.status = "confirmed"
    appt.confirmation_evidence = evidence.strip()[:500]
    session.flush()
    audit.record(session, actor, "appointment.confirm", "appointment", appt.id, evidence=evidence)
    return appointment_view(appt)


def appointment_view(a: Appointment) -> dict[str, Any]:
    return {"ref": ref("appointment", a.id), "kind": a.kind, "status": a.status,
            "starts_at_local": timeutil.fmt_local(a.starts_at), "starts_at": timeutil.iso(a.starts_at),
            "lead": ref("lead", a.lead_id), "job": ref("job", a.job_id),
            "confirmation_evidence": a.confirmation_evidence}


FOLLOWUP_BY_TYPE = {
    "deck": ["Approximate deck size (length x width) and height off the ground?",
             "Replacing an existing deck, or new? If replacing, is demolition/haul-off included?",
             "Material preference: pressure-treated, composite, or undecided?",
             "Stairs, railings, or lighting needed?", "Is the home in an HOA that requires approval?"],
    "sunroom": ["Approximate footprint and attachment wall?", "Existing slab/deck to build on, or new foundation?",
                "Three-season or conditioned (HVAC) space?", "HOA approval required?"],
    "framing": ["Do you have plans or engineering drawings?", "Labor only, or labor and materials?",
                "Who is the GC / point of contact on site?"],
    "repair": ["Can you send photos of the damage?", "Any water intrusion or soft/rotted areas?",
               "Is this an insurance claim?"],
    "remodel": ["Which rooms and roughly what scope?", "Any plumbing/electrical relocation expected?",
                "Target budget range and start date?"],
}


def followup_questions(session: Session, actor: Actor, lead_id: int) -> dict[str, Any]:
    """Deterministic checklist of missing information; Hermes words the actual message."""
    lead = get(session, actor, lead_id)
    v = lead_view(lead)
    qs: list[str] = []
    if "address" in v["missing_information"]:
        qs.append("What is the property address?")
    if "timing" in v["missing_information"]:
        qs.append("When are you hoping to have the work done?")
    if not lead.budget_text:
        qs.append("Do you have a budget range in mind? (optional)")
    jt = (lead.job_type or "").lower()
    for key, extra in FOLLOWUP_BY_TYPE.items():
        if key in jt:
            qs.extend(extra)
            break
    return {"lead": v["ref"], "questions": qs[:7], "note": "Draft only. Sending requires owner approval."}
