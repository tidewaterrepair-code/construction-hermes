# Restartable checkpoint

- Branch: `claude/quirky-ramanujan-gdlehh` (repo `tidewaterrepair-code/construction-hermes`);
  commit: see `git log -1` (this file is updated with each checkpoint commit).
- Hermes pinned: `v2026.9.24` (v0.21.5), commit `f97608f178d1ffeca59860195ab7da295f7c8e5f`;
  image `nousresearch/hermes-agent:v2026.9.24@sha256:fca358f12efd65bfaaca05884166f15c0e2788375ca30d77061ac1ebc96452b7`.
- Migrations: `0001` (schema), `0002` (grants + append-only audit trigger). Head = `0002`.
- Tests: `python -m pytest -q` → 51 passed (needs PostgreSQL superuser URL in `CHOPS_TEST_ADMIN_URL`).
- Deployment state: **not deployed to the VPS**. Verified in the build container: dev services,
  Compose stack (with `INSTALL_PG_CLIENT=0`), official Hermes container ↔ MCP.

## Layout
`src/chops/` service (models, pricing, services/*, mcp_server, web, worker, backup, cli) ·
`hermes/` SOUL.md, config.yaml, skills · `deploy/` Dockerfile, compose, initdb, systemd, scripts ·
`tests/` · `scripts/e2e_demo.py` · `docs/`.

## Blockers (owner)
1. VPS access for this deployment (or run the runbook yourself).
2. Model provider API key + model choice + monthly budget.
3. Telegram bot token + your numeric Telegram user ID.
4. Company facts: legal/display name, contact block, license/insurance text (if to be printed).
5. Commercial terms: payment milestones, proposal validity, terms/warranty text.
6. Estimating policy: overhead %, profit method + %, contingency, labor burden, tax mode/rates.
7. Real rates (with sources) and crew productivity numbers.
8. Off-server backup destination; a safe place for the backup key.
9. Private access method (Tailscale recommended) and whether email (SMTP) should be connected.

## Exact next commands (on the VPS)
```bash
docker compose ls && docker ps && ss -ltnp          # record existing workloads first
sudo git clone -b claude/quirky-ramanujan-gdlehh https://github.com/tidewaterrepair-code/construction-hermes /opt/construction-hermes
cd /opt/construction-hermes/deploy && cp ../.env.example .env && cp hermes.env.example hermes.env && chmod 600 .env hermes.env
# fill secrets, create /etc/construction-hermes/backup.key, then follow docs/RUNBOOK.md "First deployment"
```
