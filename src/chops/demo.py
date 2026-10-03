"""Synthetic demonstration dataset for the demo database (never production)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy.orm import Session

from . import fixtures, timeutil
from .authz import Actor
from .errors import Blocked
from .services import (approvals, billing, costing, estimates, field, leads, permits, proposals, rates, schedule,
                       settings)


def seed(session: Session, owner: Actor) -> dict[str, Any]:
    if settings.environment(session) not in ("prod", "demo") or (
            settings.environment(session) == "prod" and settings.get(session, "company_name")):
        raise Blocked("this database already has real configuration; refusing to seed synthetic data")
    fixtures.configure_demo_settings(session, owner)
    fixtures.load_demo_rates(session, owner)
    rates.load_example_assemblies(session)
    agent = owner  # seed as owner; records are synthetic via environment=demo
    out: dict[str, Any] = {}

    # A won job with schedule, change order, invoice and costs.
    lead = leads.capture(session, agent, name="Sample Customer A (synthetic)", phone="757-555-0100",
                         email="customer-a@example.invalid", address="1 Saunders Road (synthetic)",
                         city="Virginia Beach", state="VA", job_type="deck", scope="16x12 deck off kitchen",
                         source="website", provider="demo", provider_event_id="demo-1")["lead"]
    est = estimates.create(session, agent, title="16x12 PT deck - Saunders Road (synthetic)", pricing_mode="turnkey",
                           lead_id=lead["id"])
    estimates.apply_assembly(session, agent, est["id"], "deck-basic", {
        k: {"value": v, "source": "field_measured"} for k, v in {
            "length_ft": "16", "width_ft": "12", "joist_spacing_in": "16", "board_face_in": "5.5",
            "footing_count": "6", "labor_hours_per_sqft": "0.35"}.items()})
    estimates.set_policy(session, agent, est["id"], quote_type="firm")
    prop = proposals.create_from_estimate(session, agent, est["id"])["proposal"]
    req = proposals.request_issue(session, agent, prop["id"])["approval"]
    approvals.decide(session, owner, req["id"], "approve", presented_hash=req["payload_hash"])
    job = proposals.record_acceptance(session, owner, prop["id"], evidence="Signed proposal (synthetic demo)")["job"]
    job_id = job["id"]
    crew = schedule.add_crew_member(session, owner, "Crew Lead (synthetic)", daily_capacity_hours="8")
    crew_id = int(crew["ref"].split("-")[1])
    d1 = timeutil.today_local() + dt.timedelta(days=1)
    t1 = schedule.add_task(session, owner, job_id, name="Layout and footings", phase="Foundation",
                           starts_at=f"{d1}T07:30:00", ends_at=f"{d1}T12:00:00", estimated_hours="4.5",
                           crew_ids=[crew_id], weather_sensitive=True)["task"]
    pm = permits.add_permit(session, owner, job_id, permit_type="Building permit - deck", jurisdiction="unconfirmed")
    schedule.add_task(session, owner, job_id, name="Framing", phase="Framing", starts_at=f"{d1}T11:00:00",
                      ends_at=f"{d1}T16:00:00", estimated_hours="5", crew_ids=[crew_id], depends_on=[t1["id"]])
    co = field.create_change_order(session, owner, job_id, title="Add 3-step stair (synthetic)", scope="3-step stair",
                                   price="1150.00", estimated_cost="640.00", cost_code="06-framing")
    inv = billing.draft_milestone_invoice(session, owner, job_id, "deposit")["invoice"]
    costing.log_cost(session, owner, job_id, kind="material", amount="486.20", description="Lumber pickup (synthetic)",
                     cost_code="06-framing", vendor_name="Synthetic Lumber Yard")
    field.draft_daily_log(session, owner, job_id, original_note="Laid out deck, marked footings. Rain after 2pm. (synthetic)")
    billing.request_issue(session, owner, inv["id"])
    out["job"] = job["ref"]
    out["permit"] = pm["ref"]
    out["change_order"] = co["ref"]

    # Open leads in different states.
    for i, (name, jt, status) in enumerate([("Sample Customer B (synthetic)", "sunroom", "qualified"),
                                            ("Sample Customer C (synthetic)", "repair", "inquiry")], start=2):
        lv = leads.capture(session, agent, name=name, phone=f"757-555-01{i:02d}", job_type=jt,
                           scope=f"{jt} inquiry (synthetic)", source="phone", provider="demo",
                           provider_event_id=f"demo-{i}")["lead"]
        if status != "inquiry":
            leads.transition(session, agent, lv["id"], status)
    leads.capture(session, agent, name="Unknown caller (synthetic)", phone="757-555-0199", source="voicemail",
                  extraction={"name": {"value": "Unknown", "confidence": 0.3, "source": "voicemail"}},
                  provider="demo", provider_event_id="demo-9")
    out["note"] = "synthetic demo data loaded; environment=demo; LIVE mode is blocked for this database"
    return out
