"""End-to-end demonstration against running services (demo database only).

Agent steps go through the MCP HTTP endpoint exactly as Hermes calls it; owner steps go
through the dashboard over HTTP (login, CSRF-protected forms). Prints every record ref.

  python scripts/e2e_demo.py --mcp http://127.0.0.1:8641 --web http://127.0.0.1:8640 \
      --token-file $HERMES_HOME/.env --owner jimmy --password-env DEMO_OWNER_PASSWORD
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import uuid

import httpx


async def mcp_calls(url: str, token: str, calls: list[tuple[str, dict]]) -> list[dict]:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    client = create_mcp_http_client(headers={"Authorization": f"Bearer {token}", "X-Hermes-Profile": "e2e"})
    out = []
    async with streamable_http_client(f"{url}/mcp", http_client=client) as streams:
        async with ClientSession(streams[0], streams[1]) as cs:
            await cs.initialize()
            for name, args in calls:
                r = await cs.call_tool(name, args)
                res = json.loads(r.content[0].text)
                if not res.get("ok"):
                    raise SystemExit(f"{name} failed: {res}")
                out.append(res["result"])
    return out


def mcp(url, token, tool, **args):
    return asyncio.run(mcp_calls(url, token, [(tool, args)]))[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mcp", default="http://127.0.0.1:8641")
    ap.add_argument("--web", default="http://127.0.0.1:8640")
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--owner", default="jimmy")
    ap.add_argument("--password-env", default="DEMO_OWNER_PASSWORD")
    a = ap.parse_args()
    token = re.search(r"CHOPS_MCP_TOKEN=(\S+)", open(a.token_file).read()).group(1)
    run = uuid.uuid4().hex[:6]
    trace: dict = {"run": run}

    lead = mcp(a.mcp, token, "capture_lead", name=f"E2E Customer {run} (synthetic)", email=f"e2e-{run}@example.invalid",
               address=f"{run} Demo Lane", city="Virginia Beach", state="VA", job_type="deck",
               scope="12x12 deck, ground level", source="telegram", provider="telegram", provider_event_id=f"e2e-{run}")
    dup = mcp(a.mcp, token, "capture_lead", name=f"E2E Customer {run} (synthetic)", email=f"e2e-{run}@example.invalid",
              provider="telegram", provider_event_id=f"e2e-{run}")
    assert dup["duplicate_event"] and dup["lead"]["ref"] == lead["lead"]["ref"]
    trace["lead"] = lead["lead"]["ref"]
    est = mcp(a.mcp, token, "create_estimate", title=f"12x12 deck {run}", pricing_mode="turnkey", lead=trace["lead"])
    trace["estimate"] = est["ref"]
    params = {k: {"value": v, "source": "field_measured"} for k, v in {
        "length_ft": "12", "width_ft": "12", "joist_spacing_in": "16", "board_face_in": "5.5", "footing_count": "4",
        "labor_hours_per_sqft": "0.35"}.items()}
    mcp(a.mcp, token, "estimate_apply_assembly", estimate=trace["estimate"], assembly_code="deck-basic", params=params)
    view = mcp(a.mcp, token, "estimate_configure", estimate=trace["estimate"], quote_type="firm")
    trace["estimate_total"] = view["totals"]["total"]
    trace["firm_quote_ready"] = view["totals"]["firm_quote_ready"]
    prop = mcp(a.mcp, token, "create_proposal", estimate=trace["estimate"])["proposal"]
    trace["proposal"] = prop["ref"]
    appr = mcp(a.mcp, token, "request_approval", action="proposal.issue", target=trace["proposal"])
    trace["approval"] = appr["approval"]["ref"]
    aid = appr["approval"]["id"]

    # Owner approves on the dashboard (verified session + CSRF + presented hash).
    with httpx.Client(base_url=a.web, follow_redirects=False) as w:
        r = w.post("/login", data={"username": a.owner, "password": os.environ[a.password_env], "next": "/"})
        assert r.status_code == 303 and "err=" not in r.headers["location"], r.headers.get("location")
        page = w.get(f"/approvals/{aid}").text
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        phash = re.search(r'name="payload_hash" value="([0-9a-f]{64})"', page).group(1)
        r = w.post(f"/approvals/{aid}/decide", data={"csrf": csrf, "decision": "approve", "payload_hash": phash})
        assert "executed" in r.headers["location"], r.headers["location"]
        pid = prop["id"]
        page = w.get(f"/proposals/{pid}").text
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        r = w.post(f"/proposals/{pid}/action", data={"csrf": csrf, "action": "accept",
                                                      "evidence": f"Signed proposal received (synthetic e2e {run})"})
        loc = r.headers["location"]
        assert loc.startswith("/jobs/"), loc
        trace["job"] = "JOB-" + loc.split("/")[2].split("?")[0]

    inv = mcp(a.mcp, token, "billing_action", action="draft_milestone_invoice", job=trace["job"], milestone="deposit")
    trace["invoice_draft"] = inv["invoice"]["ref"]
    trace["invoice_number"] = inv["invoice"]["number"]
    trace["invoice_total_due"] = inv["invoice"]["total_due"]
    trace["invoice_status"] = inv["invoice"]["status"]
    fin = mcp(a.mcp, token, "job_report", job=trace["job"], section="financials")
    trace["job_contract_value"] = fin["approved_contract_value"]
    trace["proposal_record"] = mcp(a.mcp, token, "get_record", ref=trace["proposal"])["status"]
    json.dump(trace, sys.stdout, indent=1)
    print()


if __name__ == "__main__":
    main()
