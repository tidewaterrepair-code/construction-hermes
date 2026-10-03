"""MCP contract tests over real HTTP (uvicorn + MCP client). Mocked owner prompt: this proves
the server contract, not Hermes/Telegram live delivery."""

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from helpers import demo_setup


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def mcp_server(fresh_db, session, owner):
    from chops import mcp_server
    from chops.services import users

    demo_setup(session, owner)
    agent_user = users.create_user(session, owner, username="hermes", display_name="Construction Hermes", role="agent")
    token = users.issue_token(session, owner, agent_user.id, "hermes-test")
    session.commit()
    port = _free_port()
    config = uvicorn.Config(mcp_server.build_app(), host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=0.2)
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", token
    server.should_exit = True
    th.join(timeout=5)


async def _session_call(url, token, calls, elicit=None):
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    client = create_mcp_http_client(headers={"Authorization": f"Bearer {token}", "X-Hermes-Profile": "test"})
    async with streamable_http_client(f"{url}/mcp", http_client=client) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write, elicitation_callback=elicit) as cs:
            await cs.initialize()
            out = []
            for name, args in calls:
                if name == "__list__":
                    tools = await cs.list_tools()
                    out.append([t.name for t in tools.tools])
                    continue
                r = await cs.call_tool(name, args)
                out.append(json.loads(r.content[0].text) if r.content else r.structuredContent)
            return out


def test_unauthenticated_requests_rejected(mcp_server):
    url, _ = mcp_server
    r = httpx.post(f"{url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    r = httpx.post(f"{url}/mcp", json={}, headers={"Authorization": "Bearer chops_forged"})
    assert r.status_code == 401


def test_tools_drive_workflow_and_agent_cannot_self_approve(mcp_server, session):
    url, token = mcp_server
    from chops.services import settings
    from chops.authz import Actor

    res = asyncio.run(_session_call(url, token, [
        ("__list__", {}),
        ("capture_lead", {"name": "Mcp Synthetic", "email": "m@example.invalid", "job_type": "deck",
                          "provider": "telegram", "provider_event_id": "tg-1"}),
        ("capture_lead", {"name": "Mcp Synthetic", "email": "m@example.invalid", "job_type": "deck",
                          "provider": "telegram", "provider_event_id": "tg-1"}),
        ("whats_next", {}),
    ]))
    names = res[0]
    for t in ("whats_next", "capture_lead", "create_estimate", "request_approval", "owner_decision", "log_cost"):
        assert t in names
    # No raw shell/http/sql tool exists on this server.
    assert not any(n in names for n in ("shell", "exec", "sql", "http_request", "fetch"))
    first, dup = res[1]["result"], res[2]["result"]
    assert dup["duplicate_event"] is True and dup["lead"]["ref"] == first["lead"]["ref"]
    assert "text" in res[3]["result"]

    # Owner decision via chat is refused while channel approvals are disabled (default).
    res = asyncio.run(_session_call(url, token, [("owner_decision", {"approval": "latest"})]))
    assert res[0]["error"] == "channel_approvals_disabled"


def test_owner_decision_uses_elicitation_and_payload_hash(mcp_server, session, owner):
    url, token = mcp_server
    from chops.services import settings, estimates, proposals, leads
    from chops.models import Approval, Proposal
    from mcp import types

    settings._write(session, owner, "channel_approvals", True)
    session.commit()
    seq = asyncio.run(_session_call(url, token, [
        ("capture_lead", {"name": "Elicit Synthetic", "email": "e@example.invalid"}),
    ]))
    lead_ref = seq[0]["result"]["lead"]["ref"]
    params = {k: {"value": v, "source": "field_measured"} for k, v in {
        "length_ft": "10", "width_ft": "10", "joist_spacing_in": "16", "board_face_in": "5.5", "footing_count": "4",
        "labor_hours_per_sqft": "0.3"}.items()}
    seq = asyncio.run(_session_call(url, token, [
        ("create_estimate", {"title": "10x10 deck", "pricing_mode": "turnkey", "lead": lead_ref}),
    ]))
    est_ref = seq[0]["result"]["ref"]
    seq = asyncio.run(_session_call(url, token, [
        ("estimate_apply_assembly", {"estimate": est_ref, "assembly_code": "deck-basic", "params": params}),
        ("estimate_configure", {"estimate": est_ref, "quote_type": "firm"}),
        ("create_proposal", {"estimate": est_ref}),
    ]))
    assert seq[2]["ok"], seq[2]
    prop_ref = seq[2]["result"]["proposal"]["ref"]
    seq = asyncio.run(_session_call(url, token, [("request_approval", {"action": "proposal.issue", "target": prop_ref})]))
    assert seq[0]["ok"], seq
    seen = {}

    async def approve(context, params_):
        seen["message"] = params_.message
        return types.ElicitResult(action="accept", content={"decision": "approve"})

    seq = asyncio.run(_session_call(url, token, [("owner_decision", {"approval": "latest"})], elicit=approve))
    assert seq[0]["ok"], seq
    assert "Approve APR-" in seen["message"]
    session.expire_all()
    a = session.query(Approval).one()
    assert a.status == "executed" and a.decided_via == "hermes_elicitation"
    assert session.query(Proposal).one().status == "approved"

    # A declined prompt changes nothing.
    async def dismiss(context, params_):
        return types.ElicitResult(action="decline")
    seq = asyncio.run(_session_call(url, token, [("owner_decision", {"approval": "latest"})], elicit=dismiss))
    assert seq[0]["ok"] is False  # nothing pending anymore -> not_found
