"""Acceptance: one synthetic lead -> draft estimate -> approved proposal revision -> job -> invoice draft."""

from decimal import Decimal as D

import pytest

from chops import fixtures
from chops.errors import Blocked, Conflict, Forbidden, InvalidTransition
from chops.models import AuditEvent, Job, OutboxJob, Proposal
from chops.services import approvals, billing, estimates, leads, proposals, rates


@pytest.fixture
def demo(session, owner):
    fixtures.configure_demo_settings(session, owner)
    fixtures.load_demo_rates(session, owner)
    rates.load_example_assemblies(session)
    session.commit()
    return owner


def _deck_estimate(session, actor, lead_id, measured=True):
    est = estimates.create(session, actor, title="12x16 deck", pricing_mode="turnkey", lead_id=lead_id)
    src = "field_measured" if measured else "photo_estimate"
    estimates.apply_assembly(session, actor, est["id"], "deck-basic", {
        "length_ft": {"value": "16", "source": src}, "width_ft": {"value": "12", "source": src},
        "joist_spacing_in": {"value": "16", "source": "plans"}, "board_face_in": {"value": "5.5", "source": "plans"},
        "footing_count": {"value": "6", "source": "plans"},
        "labor_hours_per_sqft": {"value": "0.35", "source": "plans"},
    })
    estimates.set_policy(session, actor, est["id"], quote_type="firm")
    return est["id"]


def test_lead_to_invoice_draft_end_to_end(session, demo, agent):
    owner = demo
    cap = leads.capture(session, agent, name="Pat Synthetic", phone="757-555-0101", email="pat@example.invalid",
                        address="100 Example Road", city="Virginia Beach", state="VA", job_type="deck",
                        scope="New 12x16 deck off back door", source="website", provider="webform",
                        provider_event_id="evt-1", synthetic=True)
    lead_id = cap["lead"]["id"]
    est_id = _deck_estimate(session, agent, lead_id)
    view = estimates.view(session, agent, est_id)
    t = view["totals"]
    assert t["firm_quote_ready"], t["firm_blockers"]
    assert t["total"] is not None

    prop = proposals.create_from_estimate(session, agent, est_id)["proposal"]
    assert prop["status"] == "draft"
    # The agent can request but never approve.
    req = proposals.request_issue(session, agent, prop["id"])["approval"]
    with pytest.raises(Forbidden):
        approvals.decide(session, agent, req["id"], "approve", presented_hash=req["payload_hash"])
    res = approvals.decide(session, owner, req["id"], "approve", presented_hash=req["payload_hash"])
    assert res["approval"]["status"] == "executed"
    p = session.get(Proposal, prop["id"])
    assert p.status == "approved"
    # Synthetic + SHADOW: email is queued, never live-sent.
    obx = session.query(OutboxJob).filter_by(approval_id=req["id"]).one()
    assert obx.is_synthetic and obx.external_effect

    acc = proposals.record_acceptance(session, owner, prop["id"], evidence="Signed PDF received 10/3 (synthetic)")
    job = session.get(Job, acc["job"]["id"])
    assert job.contract_value == p.total
    assert job.source_proposal_id == p.id
    # Idempotent job creation
    from chops.services import jobs
    assert jobs.create_from_proposal(session, owner, p.id)["id"] == job.id

    inv = billing.draft_milestone_invoice(session, agent, job.id, "deposit")["invoice"]
    assert inv["status"] == "draft"
    assert D(inv["subtotal"]) == (p.total * D("0.30")).quantize(D("0.01"))
    assert D(inv["retainage_held"]) == (D(inv["subtotal"]) * D("0.05")).quantize(D("0.01"))
    # Idempotent per milestone
    assert billing.draft_milestone_invoice(session, agent, job.id, "deposit")["reused"]
    session.commit()

    # Traceability: every hop is in the audit trail with stable IDs.
    actions = [a.action for a in session.query(AuditEvent).order_by(AuditEvent.id)]
    for needed in ("lead.capture", "estimate.create", "proposal.create", "approval.request", "approval.approved",
                   "approval.executed", "proposal.accepted", "job.create", "invoice.draft"):
        assert needed in actions


def test_duplicate_inbound_event_creates_nothing(session, demo, agent):
    a = leads.capture(session, agent, name="Dup Test", phone="757-555-0199", provider="webform",
                      provider_event_id="evt-dup", synthetic=True)
    b = leads.capture(session, agent, name="Dup Test", phone="757-555-0199", provider="webform",
                      provider_event_id="evt-dup", synthetic=True)
    assert b["duplicate_event"] is True
    assert b["lead"]["id"] == a["lead"]["id"]
    from chops.models import Contact, Lead
    assert session.query(Lead).count() == 1
    assert session.query(Contact).count() == 1


def test_ambiguous_contact_is_not_merged(session, demo, agent):
    leads.capture(session, agent, name="Sam Alpha", phone="757-555-0111", synthetic=True)
    r = leads.capture(session, agent, name="Different Person", phone="757-555-0111", synthetic=True)
    assert r["dedupe"]["contact"] == "new_possible_duplicate"
    assert r["lead"]["needs_review"] is True
    # Two contacts now share the phone: even the original name is not auto-matched.
    again = leads.capture(session, agent, name="Sam Alpha", phone="(757) 555-0111", synthetic=True)
    assert again["dedupe"]["contact"] == "new_possible_duplicate"
    # A single unambiguous match with a compatible name is reused.
    leads.capture(session, agent, name="Lee Beta", email="lee@example.invalid", synthetic=True)
    same = leads.capture(session, agent, name="Lee Beta", email="LEE@example.invalid", synthetic=True)
    assert same["dedupe"]["contact"] == "matched_existing"


def test_unverified_dimensions_block_firm_proposal(session, demo, agent):
    lead_id = leads.capture(session, agent, name="Photo Only", synthetic=True)["lead"]["id"]
    est_id = _deck_estimate(session, agent, lead_id, measured=False)
    with pytest.raises(Blocked) as exc:
        proposals.create_from_estimate(session, agent, est_id)
    assert any("photo_estimate" in b for b in exc.value.detail["blockers"])
    # A rough range is allowed and is labeled as such, but cannot be accepted as a contract.
    estimates.set_policy(session, agent, est_id, quote_type="rough_range")
    prop = proposals.create_from_estimate(session, agent, est_id)["proposal"]
    assert session.get(Proposal, prop["id"]).content["quote_type"] == "rough_range"


def test_locked_revision_requires_new_revision_and_new_approval(session, demo, agent):
    owner = demo
    lead_id = leads.capture(session, agent, name="Rev Test", email="rev@example.invalid", synthetic=True)["lead"]["id"]
    est_id = _deck_estimate(session, agent, lead_id)
    prop = proposals.create_from_estimate(session, agent, est_id)["proposal"]
    req = proposals.request_issue(session, agent, prop["id"])["approval"]
    # Revision is locked: editing in place is refused.
    with pytest.raises(InvalidTransition):
        estimates.add_line(session, agent, est_id, kind="other", description="Extra", quantity="1", unit="ls",
                           unit_cost="100", cost_source="test")
    # A new revision supersedes the proposal; the pending approval can no longer execute.
    estimates.new_revision(session, agent, est_id, "customer wants stairs")
    estimates.add_line(session, agent, est_id, kind="other", description="Stairs", quantity="1", unit="ls",
                       unit_cost="900", cost_source="agent guess")
    # An agent-entered cost is provisional and blocks a firm quote...
    with pytest.raises(Blocked):
        proposals.create_from_estimate(session, agent, est_id)
    # ...until the owner enters the number.
    estimates.update_line(session, owner, est_id, 8, unit_cost="900", cost_source="owner: stair kit quote")
    proposals.create_from_estimate(session, agent, est_id)
    with pytest.raises(Conflict):
        approvals.decide(session, owner, req["id"], "approve", presented_hash=req["payload_hash"])
    assert session.get(Proposal, prop["id"]).status == "superseded"
