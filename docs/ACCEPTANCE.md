# Acceptance report

Run on 2026-10-03 in the build container (Ubuntu 24.04, PostgreSQL 16.14, Python 3.11,
hermes-agent v2026.9.24). **Not run on Jimmy's VPS** (no access from this session).
`python -m pytest -q` → **51 passed**. Test levels are labeled: *unit/integration* (real
PostgreSQL), *contract* (mocked provider or client), *live* (real component).

| # | Requirement | Result | Evidence |
|---|---|---|---|
| 1 | Synthetic lead → draft estimate → approved proposal revision → job → invoice draft, traceable IDs | **Pass** (integration + live E2E over MCP HTTP and dashboard HTTP) | `test_lead_to_invoice_draft_end_to_end`; [`acceptance/e2e-run.json`](acceptance/e2e-run.json): LEAD-5 → EST-2 → PROP-2 (APR-3) → JOB-2 → INV-2 |
| 2 | Duplicate inbound events create no duplicate records/actions | **Pass** | `test_duplicate_inbound_event_creates_nothing`, MCP `tg-1` replay, `test_idempotent_enqueue`, E2E replay |
| 3 | Missing rates/dimensions stay visible; no silent firm quote | **Pass** | `test_missing_rates_and_unverified_dimensions_block_firm_quote`, `test_unverified_dimensions_block_firm_proposal`, agent-entered cost provisional |
| 4 | Labor-only/turnkey totals, conversions, rounding, markup/margin, discounts | **Pass** | `test_money_pricing.py` ($10,000 @30% margin = $14,285.71; @30% markup = $13,000.00) |
| 5 | Accepted revisions preserved; changes need new approval | **Pass** | `test_locked_revision_requires_new_revision_and_new_approval` |
| 6 | Schedule conflicts and prerequisites detected | **Pass** | `test_schedule_conflicts_and_prerequisites`, `test_dst_and_local_time` |
| 7 | Pending change orders don't inflate revenue; partial payments and retainage distinct | **Pass** | `test_change_orders_do_not_inflate_revenue_and_retainage_is_separate` |
| 8 | Malicious document cannot send, disclose secrets, or authorize | **Pass** (service + tool boundary) | `test_malicious_document_is_contained`; agent cannot decide approvals; Hermes has no shell/file/web tools (live `hermes tools list`). *Not tested: a live model reading the document (no model key).* |
| 9 | Unauthorized users / cross-job access fail at API/tool boundary | **Pass** | `test_foreman_cross_job_access_denied`, `test_foreman_sees_only_assigned_job_without_money`, `test_unauthenticated_requests_rejected`, agent dashboard login refused |
| 10 | Expired/replayed approvals and changed payloads cannot execute | **Pass** | `test_expired_and_replayed_approvals_cannot_execute`, `test_changed_destination_invalidates_message_approval`, `test_dashboard_approval_requires_presented_hash` |
| 11 | Provider timeout, rate limit, ambiguous dispatch → safe visible states | **Pass** (contract, fake adapter) | `test_rate_limit_and_5xx_retry_with_backoff_then_dead`, `test_ambiguous_timeout_marks_unknown_and_never_resends`, `test_kill_switch_rechecked_at_dispatch` |
| 12 | Restart recovers durable work without avoidable duplicates | **Pass** | `test_crash_mid_dispatch_recovers_without_duplicate`; live restart run [`acceptance/restart-run.txt`](acceptance/restart-run.txt) |
| 13 | Backup restores into isolated env; docs, records, pending approvals preserved; restored copy cannot send | **Pass** (real pg_dump/pg_restore) | `test_backup_restore_isolated_and_cannot_send`, `test_tampered_or_truncated_backup_is_rejected`. *Off-server copy not configured → DR incomplete.* |
| 14 | Mobile dashboard supports the real workflow at phone width | **Pass** | Chromium 390×844: 12 pages, 0 horizontal overflow, 0 tap targets < 36 px, 0 console errors; [`screenshots/`](screenshots/); `test_web.py` |
| 15 | Existing unrelated VPS services remain healthy after deployment | **Blocked: no VPS access** | Compose isolation verified here ([`acceptance/compose-run.txt`](acceptance/compose-run.txt)); must re-check on the VPS (`docker compose ls`, `docker ps`, `ss -ltnp`) before/after |
| — | Hermes coordinator wired to tools; persists through restart | **Partial** | Live: host install and official pinned container both connect (31 tools), skills load, toolset restricted, reconnect after restart. **Blocked:** a real conversation needs a model provider key |
| — | Chat approval via Hermes prompt | **Contract only** | `test_owner_decision_uses_elicitation_and_payload_hash` (mocked client callback). Live Telegram path blocked on bot token |

## Not claimed
- No integration is live-delivery verified (no SMTP/Telegram/SMS/accounting credentials).
- No LIVE mode commissioning; system commissioned for **SHADOW** only.
- Image build with `postgresql-client-16` not completed here (sandbox network policy denied
  apt mirrors); the wiring build used `INSTALL_PG_CLIENT=0`.
