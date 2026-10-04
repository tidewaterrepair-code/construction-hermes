# Architecture

```
 Phone ──HTTPS(private)──► reverse proxy / VPN ──► web  (chops serve, :8640)  ─┐
                                                                               ├─► PostgreSQL 16 (backend net only)
 Telegram ◄──► hermes (Hermes Agent gateway) ──MCP/HTTP + bearer──► mcp (:8641) ┤
                    │ model provider (egress)                                  ├─► /data documents volume
                                                     worker (outbox, routines) ┘     (content-addressed)
                                                       └─► SMTP / Telegram (egress, LIVE only)
```

One owner-facing Hermes coordinator; specialist roles (intake, estimating, coordination,
procurement, job costing, documents, owner reporting) are **skills**, not separate agents.

## Decisions

| # | Decision | Why |
|---|---|---|
| ADR-1 | Hermes connects to the business layer only through an **MCP server** (`mcp_servers.construction`, HTTP + bearer token). Construction logic lives in this repo, not a Hermes fork or core plugin. | MCP is a documented, stable Hermes extension point (verified: `hermes mcp test` discovers all 31 tools). Hermes' AGENTS.md asks third-party integrations to stay out of core. |
| ADR-2 | **Disable** Hermes' terminal, file, browser, web, search, code_execution, delegation, cronjob, connections, computer_use (+ media/smart-home) toolsets via `agent.disabled_toolsets` and `platform_toolsets`. | The agent must have no path that bypasses server-side approval (verified with `hermes tools list --platform telegram`). |
| ADR-3 | Deterministic service owns rules, math, approvals; **PostgreSQL** is the system of record; Hermes memory holds only preferences/refs. | Business records must be durable, queryable and auditable; model output never is. |
| ADR-4 | Money is `NUMERIC`/`Decimal`; floats rejected. Pricing is a pure function (`chops.pricing`). | Exact cents, testable totals. |
| ADR-5 | Approvals bind a SHA-256 of the exact action payload (content, destination, amount, revision); decisions require the owner over a verified channel and the presented hash; execution re-derives and compares, then flips `approved→executing` atomically. | Edits, replays and stale approvals cannot execute. |
| ADR-6 | Chat approvals use **MCP elicitation** (Hermes routes it to its human approval prompt), off by default. | A model's "yes" is not authorization; the prompt reaches the human directly. |
| ADR-7 | **Transactional outbox** + lease-based worker; ambiguous sends become `UNKNOWN`; BUILD/SHADOW and synthetic records are simulated; kill switch rechecked at dispatch. | No sends before commit; no blind resends; safe restart. |
| ADR-8 | Server-rendered dashboard (Jinja2, no SPA), sessions in DB, CSRF tokens, strict CSP. | Fast on phones, simple to secure, no build chain. |
| ADR-9 | Docker Compose with its own project name, networks (`backend` internal), volumes and limits; only the dashboard is published, on `127.0.0.1`. Official Hermes image pinned. | Coexists with Trinity/OpenClaw/websites without touching them. |
| ADR-10 | No Redis, vector DB, or embeddings: PostgreSQL FTS and `FOR UPDATE SKIP LOCKED` cover the measured needs. | Fewer moving parts. |
| ADR-11 | Two DB roles: `chops_owner` (migrations) and `chops_app` (runtime DML; cannot alter schema, update/delete/truncate audit). | Least privilege; audit append-only for the app. |

## Capability matrix (Hermes v2026.9.24, commit f97608f)

| Requirement | Native Hermes | Extension used | Evidence | Status |
|---|---|---|---|---|
| Persistent persona | `SOUL.md` in `HERMES_HOME` | `hermes/SOUL.md` | docs `features/personality.md` | Implemented |
| Typed business tools | MCP client (HTTP, headers, `${VAR}` interpolation, timeouts) | `chops.mcp_server` (31 tools) | `hermes mcp test construction` → 31 tools, from a host install and from the pinned official container over the Compose network | Implemented, live-verified (connect/discover) |
| Narrow tool surface | `platform_toolsets`, `agent.disabled_toolsets` | `hermes/config.yaml` | `hermes tools list --platform telegram` | Implemented, live-verified |
| Skills per role | `SKILL.md` skills | 7 `construction-*` skills | `hermes skills list` | Implemented, live-verified (loaded) |
| Learned-skill review | `skills.write_approval: true` | config | docs `features/skills.md` | Configured |
| Owner approval in chat | MCP elicitation → approval surface | `owner_decision` tool | contract test (mocked client callback) | Implemented; Telegram path not live-verified |
| Telegram channel | Gateway + `TELEGRAM_ALLOWED_USERS` | `deploy/hermes.env` | docs `messaging/telegram.md` | Configured; blocked on bot token |
| Scheduling | `cronjob` toolset | Not used (disabled): routines run in `chops.worker` with durable state and catch-up | tests | Implemented in service |
| Memory | built-in memory (char-limited) | small limits | config | Configured |
| Spend cap | none native (provider-side only) | host `usage-guard.sh` (token cap → stop Hermes + kill switch) | script parse test | Implemented; not live-verified |
| Model provider | many providers incl. ChatGPT sign-in (`openai-codex`, device code) | installer option 1 (default) | `hermes auth add openai-codex` starts the device flow in v2026.9.24 | **Blocked: owner must sign in** |
