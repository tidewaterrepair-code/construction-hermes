# Configuration

Three layers; secrets never live in git.

## 1. Process environment (`deploy/.env`, mode 600)

See [`.env.example`](../.env.example). Database passwords, `CHOPS_SECRET_KEY`, `CHOPS_BASE_URL`,
cookie security, backup paths, and optional SMTP / Telegram-alert credentials. An integration
with missing credentials stays **disconnected** and its actions are `blocked`, never faked.

## 2. Business settings (database, owner-only, Dashboard → More → Settings)

Absent = "not configured" and shown that way. Nothing is guessed.

| Key | Example | Effect when missing |
|---|---|---|
| `company_name`, `company_contact`, `license_info`, `insurance_info` | text | Documents print "[Company name not configured]"; proposals cannot be issued |
| `commercial_terms` | `{"payment_milestones":[{"key":"deposit","label":"Deposit","pct":"0.30"},...], "validity_days":30, "terms_text":"...", "warranty_text":"..."}` | Proposals cannot be issued |
| `estimating_defaults` | `{"overhead_pct":"0.12","profit_method":"margin","profit_pct":"0.25","contingency_pct":"0.05","labor_burden_pct":"0.22","tax_mode":"contractor_pays_material_tax","material_tax_pct":"0.06"}` | Estimates show "overhead/profit/tax not set" and cannot be firm |
| `rough_range` | `{"low_pct":"0.10","high_pct":"0.25"}` | Uses a disclosed default band |
| `service_area` | `{"text":"Hampton Roads","confirmed":false}` | Jurisdiction stays "unconfirmed" |
| `invoice_terms_days`, `retainage_default_pct` | `15`, `"0.05"` | No due date / no retainage |
| `budget`, `local_usage_cap_usd` | `{"monthly_usd":75}` | Model-using routines stay disabled |
| `work_calendar` | `{"workdays":[0,1,2,3,4],"holidays":["2026-12-25"]}` | Working-day checks skipped (advisory shown) |
| `standing_policies` | `[{"action_type":"message.send","destination":"x@y","expires":"2026-12-31"}]` | Every action needs a fresh approval (POs/money never covered) |
| `channel_approvals` | on/off | Off: approvals only on the dashboard |
| `mode` | BUILD / SHADOW / LIVE | Default BUILD; LIVE needs typed confirmation; demo/restore DBs cannot go LIVE |

Tax treatment: the system does not decide Virginia tax rules. Choose a `tax_mode`
(`none`, `contractor_pays_material_tax`, `sales_tax_on_price`) with your accountant.

## 3. Hermes (`HERMES_HOME`, Docker volume `hermes_data`)

- `config.yaml` from [`hermes/config.yaml`](../hermes/config.yaml): MCP server URL + bearer
  header, disabled toolsets, skill write approval, telemetry off, external-login adoption off.
- `SOUL.md`, `skills/construction/*` seeded once by `hermes-init` (your later edits are kept).
- `deploy/hermes.env`: `CHOPS_MCP_TOKEN`, one provider API key, `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_ALLOWED_USERS` (numeric IDs). Set the model:
  `docker compose exec hermes hermes config set model.default <provider/model>`.
- Hermes picks a provider automatically from available credentials (`provider: auto`). Only
  put the credential you intend to pay for in `hermes.env`.
- **ChatGPT sign-in (default in the installer).** Hermes supports signing in with a ChatGPT
  account as provider `openai-codex` (verified in v2026.9.24 docs and CLI): device-code login on a
  headless server — `docker compose -p construction-hermes exec hermes hermes auth add openai-codex --type oauth`,
  open the link, enter the code; then `... exec hermes hermes model` → **ChatGPT or Codex
  Subscription** → model; then `docker compose -p construction-hermes restart hermes`. Hermes keeps its own
  login in `auth.json` inside the `hermes_data` volume (survives restarts and upgrades; not in the
  app backups, so after a full server loss you sign in again). `auth.adopt_external_logins: false`
  only stops Hermes from borrowing a Codex CLI login on the same machine; its own login is unaffected.
  Plan limits: `... exec hermes hermes usage` shows the 5-hour/weekly windows. Hermes' docs do not
  state which ChatGPT plans qualify or how usage counts against them. If the login fails with a
  TLS error, the installer offers Hermes' documented workaround (classic TLS groups via
  `OPENSSL_CONF`), applied to the Hermes container only.

## Users and roles

| Role | Can |
|---|---|
| owner | everything, incl. approvals, verifying payments/permits/inspections, settings, kill-switch release, export |
| office | drafts, records, verify payments, rates; no approvals/settings |
| agent (Hermes) | read all; draft; request approvals; engage kill switch; cannot approve, verify money, release kill switch, change policy, or log in to the dashboard |
| foreman | assigned jobs only: tasks, daily logs, photos, receipts; no financials |
| crew | assigned jobs: own tasks, photos |
| viewer | read-only |

Create users: `docker compose exec web chops create-user NAME --display-name "..." --role foreman`,
then `docker compose exec web chops assign-job JOB-12 NAME --role foreman`.
