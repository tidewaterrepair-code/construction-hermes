# Known limitations

- **ChatGPT sign-in** works through Hermes' `openai-codex` provider. Hermes' docs do not say which
  ChatGPT plans qualify or how Hermes usage counts against plan limits; watch `hermes usage`. If a
  token refresh is permanently rejected, Hermes asks you to sign in again (re-run the installer or
  the `hermes auth add openai-codex` command).
- **No model provider configured.** Hermes cannot converse until you sign in (ChatGPT) or add an API key, and pick a model.
  Natural-language extraction quality (leads from texts, daily logs from notes) is therefore
  untested; the tools accept per-field confidence and flag uncertain fields for review.
- **Not deployed to the VPS.** Everything was built and verified in an ephemeral build
  container. VPS facts (OS, free RAM/disk, existing services, ports, Docker version, backup
  arrangements) are unknown and must be checked at deploy time (runbook pre-checks).
- **Integrations are disconnected**: email, SMS (no adapter), Telegram, accounting, calendar,
  transcription, weather, off-server backup. Manual entry, uploads and CSV import/export work.
- **Chat approvals** rely on Hermes routing MCP elicitation to its human approval prompt and on
  the Telegram allowlist containing only the owner. Contract-tested, not live-tested; off by default.
- **Usage cap** is a local month-to-date *token* cap read from `hermes insights` (text parsing)
  and enforced by stopping the Hermes container. It is not a billing guarantee; also set a hard
  limit at the provider. Cost data may show "unknown" for some providers.
- **Assemblies are examples**, not local pricing or engineered designs. No rates are loaded in
  production; synthetic demo rates exist only in demo databases.
- **Tax** treatment is owner-selected per estimate (`tax_mode`); the system does not determine
  Virginia tax law.
- **Jurisdiction** is stored as "unconfirmed" unless the owner sets it; official research is
  limited to an allowlist of government/code-publisher domains and fetches one page at a time.
- **Audit log** is append-only for the application role only; a database superuser or server
  administrator can alter it.
- **Accounting**: no sync; the app is not a tax ledger. External IDs and uniqueness constraints
  exist for future reconciliation.
- **Document extraction** covers PDF text and plain text/CSV. Images are stored but not OCR'd;
  zip-based Office files are refused (export to PDF).
- **Single company / single owner** tenancy. Hermes talks to one owner; field users should use
  the dashboard (the agent identity cannot distinguish chat senders).
- **Learning** produces suggestions only (labor productivity vs estimate); no automatic
  application, and it needs ≥3 completed jobs with labor hours logged.
- Dashboard has no in-app user management or job-assignment UI (CLI: `chops create-user`,
  `chops assign-job`).
