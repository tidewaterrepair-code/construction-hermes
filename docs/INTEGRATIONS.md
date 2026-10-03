# Integration status

Verification levels: **contract test** (mocked provider), **live read** (real read-only
check), **live delivery** (authorized real send). Nothing below is live-delivery verified.

| Integration | Purpose | Status | Verification | Blocker / next step |
|---|---|---|---|---|
| Hermes Agent v2026.9.24 ↔ MCP | Coordinator ↔ tools | Connected (build container) | Live: connect + 31 tools discovered, skills loaded, toolset restrictions | Deploy on VPS |
| Model provider | Hermes reasoning | **Disconnected** | Hermes fails visibly without a key | Owner provides an API key + model choice + budget |
| Telegram (Hermes gateway) | Phone chat | **Disconnected** | — | `TELEGRAM_BOT_TOKEN` + owner numeric ID in `TELEGRAM_ALLOWED_USERS` |
| Chat approvals (MCP elicitation) | Approve from chat | Off by default | Contract test (mocked client) | Enable after Telegram works and allowlist = owner only; then live-test |
| Telegram owner alerts (worker) | Routine digests | **Disconnected** | Contract tests (timeouts, 429) | Bot token + owner chat id; LIVE mode |
| SMTP email | Customer proposals/invoices/messages | **Disconnected** | Contract tests (ambiguous timeout → UNKNOWN) | SMTP host/user/password/from; sandbox mailbox test first |
| SMS | Customer texts | **Disconnected** (no provider) | — | Choose provider; adapter not built |
| Accounting (QuickBooks/Xero) | Ledger of record | **Disconnected** | — | Owner decision on system of record; CSV import/export works now |
| Calendar | Visit/task sync | **Disconnected** | — | Choose calendar; tasks/appointments live in the app now |
| Transcription | Voice notes | **Disconnected** | — | Provider + budget; voice notes are stored meanwhile |
| Weather | Advisories | **Disconnected** | — | Weather-sensitive tasks show "check forecast" |
| Official-source research | Permit/code citations | Implemented (allowlisted fetch) | Contract test (mock transport) | Live fetch from VPS |
| Off-server backup | Disaster recovery | **Not configured** | Local encrypted backup + restore verified | Destination + credentials (DR incomplete until then) |
