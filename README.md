# Construction Hermes

A construction operations partner for Jimmy Blackwell: capture leads, price work, run jobs,
track cost and cash, and see what needs attention — from a phone.

- **Hermes Agent** (Nous Research, pinned `v2026.9.24`) is the conversational coordinator
  (Telegram or CLI). It has no shell, file, browser, web or code-execution tools; its only
  business path is the typed **construction tools** served over MCP.
- The **operations service** (`src/chops`, Python + PostgreSQL) owns every record,
  calculation, approval and outbound action. The phone dashboard and Hermes call the same
  service functions, so the rules are the same on both.

Status: **BLOCKED for deployment** (no VPS access from the build session; no model provider key).
The software is built, tested, and ready to commission in **SHADOW** once those exist. See
[`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md) and [`docs/CHECKPOINT.md`](docs/CHECKPOINT.md).

## Install on your VPS (one command)

SSH into the server and run:

```bash
curl -fsSL https://raw.githubusercontent.com/tidewaterrepair-code/construction-hermes/HEAD/install.sh | sudo bash
```

It checks the server, asks a few questions (your login, company name, Telegram, phone access,
AI usage cap), installs Docker if missing, builds and starts everything in its own isolated
Docker project, connects Hermes to the construction tools, walks you through picking an AI
model, sets up nightly encrypted backups, starts in **SHADOW** mode (nothing is sent), and prints
a summary. Have ready (optional, can be added later by re-running):

- an AI provider API key (Anthropic, OpenRouter, ...) — a Claude.ai/ChatGPT subscription is not an API key
- a Telegram bot token from @BotFather and your numeric user ID from @userinfobot
- a free Tailscale account if you want to open the dashboard from your phone

Re-running the same command upgrades the install and keeps your data and settings. It never
touches other containers, SSH or firewall settings, and opens no public ports.

## What it does

| Area | Highlights |
|---|---|
| Leads | Manual/web/channel capture, provider event-ID dedupe, cautious contact matching (never auto-merges), pipeline with reasons, tentative vs confirmed site visits, missing-info follow-up checklist |
| Estimating | Versioned revisions, labor-only vs turnkey, example assemblies (deck, framing, sunroom, repair), rate provenance (verified / owner-entered / provisional / expired), dimension sources, firm-quote gating, rough ranges, markup vs margin, PDF proposals with no internal costs |
| Jobs | Job from accepted proposal (contract + budget frozen), tasks, dependencies, crew capacity, conflict + prerequisite detection, DST-safe times, reschedule proposals, daily logs with original notes, change orders (not revenue until customer-approved) |
| Procurement | Like-for-like quote comparison with pack conversion and expiry, takeoffs, POs tied to cost codes, subcontractor COI/license tracking |
| Money | Estimated / committed / actual / forecast cost, margin erosion, milestone invoices, retainage, reported vs verified payments, receivables, previewed CSV import |
| Compliance | Permits, inspections (unverified until owner-confirmed), RFIs, punch list, allowlisted official-source research with citations |
| Documents | Content-addressed storage, archive/active-content defenses, full-text search with file/page/revision citations, untrusted-data envelopes |
| Control | One approval inbox (dashboard + chat), payload-hash-bound approvals, kill switch, BUILD/SHADOW/LIVE modes, transactional outbox, audit trail, encrypted backups with verified restore |

## Five things to say to Hermes

1. "What needs me today?"
2. "New lead: Dana at 757-555-0142, wants a 12x16 composite deck in Kempsville, sometime in spring."
3. "Draft a labor-only deck estimate for LEAD-12, 16 by 12, measured on site."
4. "Log this receipt to the Saunders Road job" (with a photo of the receipt)
5. "Show jobs losing margin."

## Quick start (development)

```bash
uv venv -p 3.11 .venv && . .venv/bin/activate && uv pip install -e ".[dev]"
export CHOPS_DATABASE_URL=postgresql+psycopg://chops_app:...@127.0.0.1:5433/chops
export CHOPS_MIGRATE_DATABASE_URL=postgresql+psycopg://chops_owner:...@127.0.0.1:5433/chops
chops migrate && chops bootstrap-owner
chops serve            # dashboard  127.0.0.1:8640
chops serve-mcp        # MCP tools  127.0.0.1:8641 (internal only)
chops worker           # outbox + routines
python -m pytest -q    # needs a PostgreSQL superuser URL in CHOPS_TEST_ADMIN_URL
```

Production deployment (Docker Compose, isolated project/network/volumes) is in
[`docs/RUNBOOK.md`](docs/RUNBOOK.md).

## Docs

- [Architecture & capability matrix](docs/ARCHITECTURE.md)
- [Configuration](docs/CONFIGURATION.md) · [Operating policy](docs/OPERATING_POLICY.md)
- [Threat model](docs/THREAT_MODEL.md) · [Runbook (deploy, backup, rollback, upgrade)](docs/RUNBOOK.md)
- [Integration status](docs/INTEGRATIONS.md) · [Acceptance report](docs/ACCEPTANCE.md)
- [Known limitations](docs/KNOWN_LIMITATIONS.md) · [Checkpoint](docs/CHECKPOINT.md)
