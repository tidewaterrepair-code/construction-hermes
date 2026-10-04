"""Outbox, approvals and routine durability (fake provider adapters; mocked contract tests)."""

import datetime as dt

import pytest
from sqlalchemy import text

from chops import timeutil
from chops.errors import Ambiguous, Blocked, InvalidTransition, NotFound
from chops.models import Approval, Integration, Notification, OutboxJob, Routine
from chops.services import approvals, billing, integrations, outbox, settings
from helpers import demo_setup, make_job


class FakeAdapter(integrations.Adapter):
    name = "fake"
    idempotent = False

    def __init__(self, behavior):
        self.behavior = behavior
        self.sent = []

    def configured(self):
        return True

    def is_connected(self, session):
        return True

    def send(self, session, payload, key):
        b = self.behavior.pop(0) if self.behavior else "ok"
        if b == "timeout":
            raise integrations.AmbiguousError("read timeout after send")
        if b == "ratelimit":
            raise integrations.TransientError("429", retry_after=dt.timedelta(seconds=30))
        if b == "5xx":
            raise integrations.TransientError("503")
        if b == "permanent":
            raise integrations.PermanentError("400 bad recipient")
        self.sent.append(key)
        return {"provider_ref": f"msg-{len(self.sent)}"}


@pytest.fixture
def live(session, owner):
    """A production-environment database in LIVE mode with a fake provider."""
    settings._write(session, owner, "mode", "LIVE")
    integrations.ensure_rows(session)
    session.add(Integration(name="fake", kind="test", status="connected"))
    session.commit()
    return owner


def _enqueue(session, key="k1", synthetic=False):
    j = outbox.enqueue(session, kind="fake.send", payload={"to": "x"}, idempotency_key=key, external_effect=True,
                       synthetic=synthetic)
    session.commit()
    return j.id


def _run(session, monkeypatch, adapter, job_id):
    monkeypatch.setitem(integrations.ADAPTERS, "fake.send", adapter)
    session.execute(text("UPDATE outbox_jobs SET next_attempt_at = now() WHERE id = :i"), {"i": job_id})
    ids = outbox.lease(session, "w1", 10, 60)
    for i in ids:
        outbox.dispatch(session, i)
    session.commit()
    session.expire_all()
    return session.get(OutboxJob, job_id)


def test_idempotent_enqueue(session, live):
    a = _enqueue(session, "same")
    b = _enqueue(session, "same")
    assert a == b and session.query(OutboxJob).count() == 1


def test_rate_limit_and_5xx_retry_with_backoff_then_dead(session, live, monkeypatch):
    jid = _enqueue(session)
    ad = FakeAdapter(["ratelimit"])
    j = _run(session, monkeypatch, ad, jid)
    assert j.status == "pending" and j.next_attempt_at > timeutil.now() + dt.timedelta(seconds=20)
    assert session.get(Integration, "fake").status == "degraded"
    session.execute(text("UPDATE outbox_jobs SET max_attempts = 2 WHERE id = :i"), {"i": jid})
    j = _run(session, monkeypatch, FakeAdapter(["5xx"]), jid)
    assert j.status == "dead"                                  # gave up visibly
    assert session.query(Notification).filter(Notification.kind == "dispatch_dead").count() == 1


def test_ambiguous_timeout_marks_unknown_and_never_resends(session, live, monkeypatch):
    jid = _enqueue(session)
    ad = FakeAdapter(["timeout"])
    j = _run(session, monkeypatch, ad, jid)
    assert j.status == "unknown"
    j = _run(session, monkeypatch, ad, jid)                    # another worker pass
    assert j.status == "unknown" and ad.sent == []             # not resent
    # Owner investigates and explicitly allows exactly one resend.
    outbox.resolve_unknown(session, live, jid, "resend", "checked provider log: not delivered")
    session.commit()
    j = _run(session, monkeypatch, ad, jid)
    assert j.status == "succeeded" and len(ad.sent) == 1


def test_kill_switch_rechecked_at_dispatch(session, live, monkeypatch, agent):
    jid = _enqueue(session)
    settings.engage_kill_switch(session, agent, "suspicious")  # the agent may engage...
    session.commit()
    ad = FakeAdapter([])
    j = _run(session, monkeypatch, ad, jid)
    assert j.status == "blocked" and ad.sent == []
    from chops.errors import Forbidden
    with pytest.raises(Forbidden):
        settings.release_kill_switch(session, agent, "nope")  # ...but never release it


def test_shadow_and_synthetic_never_send(session, owner, monkeypatch):
    settings._write(session, owner, "mode", "SHADOW")
    session.commit()
    jid = _enqueue(session)
    ad = FakeAdapter([])
    j = _run(session, monkeypatch, ad, jid)
    assert j.status == "simulated" and ad.sent == []
    settings._write(session, owner, "mode", "LIVE")
    session.commit()
    jid2 = _enqueue(session, "syn", synthetic=True)
    j = _run(session, monkeypatch, ad, jid2)
    assert j.status == "simulated" and ad.sent == []


def test_crash_mid_dispatch_recovers_without_duplicate(session, live, monkeypatch):
    jid = _enqueue(session)
    internal = outbox.enqueue(session, kind="inbox.notify", payload={"title": "t", "body": "b"},
                              idempotency_key="internal-1", external_effect=False)
    session.commit()
    # Worker leases both, then "crashes" before finishing.
    outbox.lease(session, "dead-worker", 10, 1)
    session.commit()
    session.execute(text("UPDATE outbox_jobs SET lease_expires_at = now() - interval '1 minute'"))
    session.commit()
    res = outbox.recover_expired_leases(session)
    session.commit()
    session.expire_all()
    assert res == {"retried": 1, "unknown": 1}
    assert session.get(OutboxJob, jid).status == "unknown"            # external: investigate, don't resend
    assert session.get(OutboxJob, internal.id).status == "pending"    # internal: safe to retry
    ids = outbox.lease(session, "w2", 10, 60)
    for i in ids:
        outbox.dispatch(session, i)
    session.commit()
    assert session.get(OutboxJob, internal.id).status == "succeeded"
    assert session.query(Notification).filter(Notification.fingerprint == "internal-1").count() == 1


def test_expired_and_replayed_approvals_cannot_execute(session, owner, agent):
    demo_setup(session, owner)
    job_id = make_job(session, owner, agent)
    inv = billing.draft_milestone_invoice(session, agent, job_id, "deposit")["invoice"]
    req = billing.request_issue(session, agent, inv["id"])["approval"]
    session.execute(text("UPDATE approvals SET expires_at = now() - interval '1 second' WHERE id = :i"), {"i": req["id"]})
    session.commit()
    with pytest.raises(InvalidTransition):
        approvals.decide(session, owner, req["id"], "approve", presented_hash=req["payload_hash"])
    session.commit()
    assert session.get(Approval, req["id"]).status == "expired"
    # Fresh request; approve once; a second approval/execution is refused.
    inv2 = session.get(__import__("chops.models", fromlist=["Invoice"]).Invoice, inv["id"])
    inv2.status = "draft"
    session.commit()
    req2 = billing.request_issue(session, agent, inv["id"])["approval"]
    approvals.decide(session, owner, req2["id"], "approve", presented_hash=req2["payload_hash"])
    with pytest.raises(InvalidTransition):
        approvals.decide(session, owner, req2["id"], "approve", presented_hash=req2["payload_hash"])
    with pytest.raises(InvalidTransition):
        approvals._execute(session, session.get(Approval, req2["id"]), owner)


def test_bare_yes_must_be_unambiguous(session, owner, agent):
    demo_setup(session, owner)
    job_id = make_job(session, owner, agent)
    with pytest.raises(NotFound):
        approvals.resolve_bare_confirmation(session, agent)
    i1 = billing.draft_milestone_invoice(session, agent, job_id, "deposit")["invoice"]
    billing.request_issue(session, agent, i1["id"])
    assert approvals.resolve_bare_confirmation(session, agent).target_id == i1["id"]
    i2 = billing.draft_milestone_invoice(session, agent, job_id, "progress")["invoice"]
    billing.request_issue(session, agent, i2["id"])
    with pytest.raises(Ambiguous):
        approvals.resolve_bare_confirmation(session, agent)


def test_changed_destination_invalidates_message_approval(session, owner, agent):
    from chops.models import Contact
    from chops.services import leads
    lead = leads.capture(session, agent, name="Msg Synthetic", email="old@example.invalid")["lead"]
    cid = int(lead["customer"]["ref"].split("-")[1])
    req = approvals.request(session, agent, "message.send", cid,
                            extra={"channel": "email", "subject": "Follow up", "body": "Hi, when can we visit?"})["approval"]
    session.get(Contact, cid).email = "attacker@example.invalid"
    session.commit()
    from chops.errors import Conflict
    with pytest.raises(Conflict):
        approvals.decide(session, owner, req["id"], "approve", presented_hash=req["payload_hash"])


def test_standing_policy_never_covers_money_movement(session, owner, agent):
    demo_setup(session, owner)
    job_id = make_job(session, owner, agent)
    settings._write(session, owner, "standing_policies", [{"action_type": "purchase_order.issue", "max_amount": "100000"}])
    from chops.services import procurement
    v = procurement.add_vendor(session, agent, name="Vendor (synthetic)")
    po = procurement.draft_po(session, agent, job_id, v["id"], lines=[{"description": "screws", "qty": "1", "unit_cost": "10",
                                                                        "cost_code": "06"}])
    res = procurement.request_po_issue(session, agent, po["id"])
    assert res["approval"]["status"] == "pending" and "auto_approved_by_policy" not in res


def test_routine_due_catch_up_and_dst():
    from chops.worker import routine_due

    r = Routine(name="t", description="t", local_time="06:45", weekdays="0,1,2,3,4,5,6", enabled=True, last_run_at=None)
    # 2026-11-01 is the fall-back DST day in New York: 06:45 local = 11:45 UTC (EST).
    before = dt.datetime(2026, 11, 1, 11, 44, tzinfo=dt.timezone.utc)
    after = dt.datetime(2026, 11, 1, 11, 46, tzinfo=dt.timezone.utc)
    assert not routine_due(r, before) and routine_due(r, after)
    # Summer: 06:45 EDT = 10:45 UTC
    assert routine_due(r, dt.datetime(2026, 7, 1, 10, 46, tzinfo=dt.timezone.utc))
    # Missed run (worker was down) catches up once later that day, then not again.
    late = dt.datetime(2026, 7, 1, 15, 0, tzinfo=dt.timezone.utc)
    assert routine_due(r, late)
    r.last_run_at = late
    assert not routine_due(r, late + dt.timedelta(hours=3))
    assert routine_due(r, dt.datetime(2026, 7, 2, 10, 46, tzinfo=dt.timezone.utc))


def test_routines_send_nothing_when_nothing_changed(session, owner, fresh_db):
    from chops import worker

    worker.ensure_routines(session)
    r = session.get(Routine, "morning_priorities")
    r.enabled = True
    session.commit()
    # A Tuesday evening (the morning routine runs Mon-Sat), independent of today's date.
    ran = worker.run_routines(dt.datetime(2026, 10, 6, 23, 59, tzinfo=dt.timezone.utc))
    session.expire_all()
    r = session.get(Routine, "morning_priorities")
    assert ran == [] and "nothing new" in (r.last_result or "")
