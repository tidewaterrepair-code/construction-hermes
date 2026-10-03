"""Shared synthetic scenario builders."""

from chops import fixtures
from chops.services import approvals, estimates, leads, proposals, rates


def demo_setup(session, owner):
    fixtures.configure_demo_settings(session, owner)
    fixtures.load_demo_rates(session, owner)
    rates.load_example_assemblies(session)
    session.commit()


def make_job(session, owner, agent, name="Pat Synthetic", event="evt-job", email="pat@example.invalid"):
    lead = leads.capture(session, agent, name=name, email=email, phone=None, address=f"{event} Example Rd",
                         city="Virginia Beach", state="VA", job_type="deck", scope="deck", provider="webform",
                         provider_event_id=event, synthetic=True)["lead"]
    est = estimates.create(session, agent, title=f"Deck for {name}", pricing_mode="turnkey", lead_id=lead["id"])
    estimates.apply_assembly(session, agent, est["id"], "deck-basic", {
        k: {"value": v, "source": "field_measured"} for k, v in
        {"length_ft": "16", "width_ft": "12", "joist_spacing_in": "16", "board_face_in": "5.5", "footing_count": "6",
         "labor_hours_per_sqft": "0.35"}.items()})
    estimates.set_policy(session, agent, est["id"], quote_type="firm")
    prop = proposals.create_from_estimate(session, agent, est["id"])["proposal"]
    req = proposals.request_issue(session, agent, prop["id"])["approval"]
    approvals.decide(session, owner, req["id"], "approve", presented_hash=req["payload_hash"])
    acc = proposals.record_acceptance(session, owner, prop["id"], evidence="signed (synthetic)")
    session.commit()
    return acc["job"]["id"]
