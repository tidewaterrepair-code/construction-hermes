import re

import pytest
from fastapi.testclient import TestClient

from helpers import demo_setup, make_job

PW = "correct horse battery staple"


@pytest.fixture
def client(fresh_db, session, owner, agent):
    demo_setup(session, owner)
    from chops.web import app

    return TestClient(app, base_url="http://127.0.0.1")


def _login(c, user="jimmy", pw=PW):
    r = c.post("/login", data={"username": user, "password": pw, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303
    return r


def _csrf(c, path):
    html = c.get(path).text
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, path
    return m.group(1)


def test_unauthenticated_redirects_and_headers(client):
    r = client.get("/jobs", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    r = client.get("/login")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"


def test_login_failure_and_rate_limit(client):
    for _ in range(10):
        r = client.post("/login", data={"username": "jimmy", "password": "nope"}, follow_redirects=False)
    r = client.post("/login", data={"username": "jimmy", "password": PW}, follow_redirects=False)
    assert "Too+many" in r.headers["location"] or "Too%20many" in r.headers["location"]


def test_agent_identity_cannot_log_in_to_dashboard(client, session, owner):
    from chops.services import users
    u = users.create_user(session, owner, username="botlogin", display_name="bot", role="agent", password="x" * 14)
    session.commit()
    r = client.post("/login", data={"username": "botlogin", "password": "x" * 14}, follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_owner_pages_render_and_csrf_enforced(client, session, owner, agent):
    job_id = make_job(session, owner, agent)
    _login(client)
    for path in ("/", "/leads", "/leads/new", "/jobs", f"/jobs/{job_id}", "/approvals", "/money", "/system",
                 "/settings", "/upload"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text[:300])
    # Missing CSRF token -> refused
    r = client.post("/leads/new", data={"name": "No Csrf"}, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]
    token = _csrf(client, "/leads/new")
    r = client.post("/leads/new", data={"csrf": token, "name": "Form Synthetic", "phone": "757-555-0177"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/leads/")
    assert client.get(r.headers["location"]).status_code == 200


def test_dashboard_approval_requires_presented_hash(client, session, owner, agent):
    from chops.services import billing
    job_id = make_job(session, owner, agent)
    inv = billing.draft_milestone_invoice(session, agent, job_id, "deposit")["invoice"]
    req = billing.request_issue(session, agent, inv["id"])["approval"]
    session.commit()
    _login(client)
    page = client.get(f"/approvals/{req['id']}").text
    assert "Exactly what will happen" in page and req["payload_hash"] in page
    token = _csrf(client, f"/approvals/{req['id']}")
    r = client.post(f"/approvals/{req['id']}/decide", data={"csrf": token, "decision": "approve",
                                                             "payload_hash": "0" * 64}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    r = client.post(f"/approvals/{req['id']}/decide", data={"csrf": token, "decision": "approve",
                                                             "payload_hash": req["payload_hash"]}, follow_redirects=False)
    assert "executed" in r.headers["location"]
    # Replay of the same approval is refused.
    r = client.post(f"/approvals/{req['id']}/decide", data={"csrf": token, "decision": "approve",
                                                             "payload_hash": req["payload_hash"]}, follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_foreman_sees_only_assigned_job_without_money(client, session, owner, agent):
    from chops.services import jobs, users
    j1 = make_job(session, owner, agent)
    j2 = make_job(session, owner, agent, name="Second Synthetic", event="evt-2", email="s@example.invalid")
    fu = users.create_user(session, owner, username="foreman", display_name="Foreman", role="foreman", password=PW)
    jobs.assign_user(session, owner, j1, fu.id, "foreman")
    session.commit()
    _login(client, "foreman")
    r = client.get(f"/jobs/{j1}")
    assert r.status_code == 200 and "Approved contract" not in r.text
    assert client.get(f"/jobs/{j2}").status_code == 403
    assert client.get("/money").status_code == 403
    assert client.get("/", follow_redirects=False).status_code == 303


def test_malformed_form_input_is_a_message_not_a_crash(client, session, owner, agent):
    job_id = make_job(session, owner, agent)
    _login(client)
    token = _csrf(client, f"/jobs/{job_id}")
    r = client.post(f"/jobs/{job_id}/action", data={"csrf": token, "action": "cost", "kind": "material",
                                                   "amount": "twelve", "description": "x"}, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]
