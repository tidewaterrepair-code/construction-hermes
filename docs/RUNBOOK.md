# Runbook

All commands run on the VPS as an admin (deployment privileges are separate from the running
agent: Hermes has no Docker socket, shell or host mounts).

## First deployment (one command)
```bash
curl -fsSL https://raw.githubusercontent.com/tidewaterrepair-code/construction-hermes/HEAD/install.sh | sudo bash
```
`install.sh` does everything below plus onboarding, verification and a summary
(`/root/construction-hermes-install.txt`; log `/var/log/construction-hermes-install.log`).
Unattended: set `CH_NONINTERACTIVE=1` and the `CH_*` variables listed at the top of `install.sh`.

## First deployment (manual alternative)
```bash
sudo mkdir -p /opt/construction-hermes /etc/construction-hermes /var/backups/construction-hermes
sudo git clone https://github.com/tidewaterrepair-code/construction-hermes /opt/construction-hermes
cd /opt/construction-hermes/deploy
# Check for conflicts first: nothing else should own this project name or 127.0.0.1:8640
docker compose ls; ss -ltnp | grep 8640 || true
cp ../.env.example .env && cp hermes.env.example hermes.env && chmod 600 .env hermes.env   # fill secrets
sudo python3 -c "import base64,os;open('/etc/construction-hermes/backup.key','w').write(base64.b64encode(os.urandom(32)).decode())"
sudo chmod 600 /etc/construction-hermes/backup.key      # ALSO store a copy off this server
docker compose -p construction-hermes up -d --build db migrate web mcp worker
docker compose -p construction-hermes exec web chops bootstrap-owner          # prompts for password
docker compose -p construction-hermes exec web chops issue-agent-token        # paste into hermes.env as CHOPS_MCP_TOKEN
docker compose -p construction-hermes exec web chops mode SHADOW
docker compose -p construction-hermes up -d hermes-init hermes
docker compose -p construction-hermes exec hermes hermes mcp test construction  # expect 31 tools
docker compose -p construction-hermes exec hermes hermes tools list --platform telegram  # terminal/file/browser/web disabled
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now construction-hermes.service construction-hermes-backup.timer construction-hermes-usage-guard.timer
```

## Private phone access (choose one; nothing is public by default)
- **Tailscale/WireGuard (recommended)**: join the VPS and phone to the tailnet; serve with
  `tailscale serve --https=443 http://127.0.0.1:8640`; set `CHOPS_BASE_URL` to that URL.
- **Reverse proxy** (only with an explicit decision to expose): TLS, a 30 MB body limit,
  rate limiting, and optionally an SSO/basic-auth layer in front of the app login.

## Health
`docker compose -p construction-hermes exec web chops health` (JSON; exit 1 on failure),
`GET /healthz`, dashboard → More → System (integrations, outbound queue, routines, audit).

## "Hermes is shutting down" in Telegram
Hermes sends that notice whenever its gateway stops while it is working on a message. Find out
why (read-only): `sudo bash /opt/construction-hermes/deploy/scripts/why-hermes-stopped.sh`.
It shows Docker's stop/start times, Hermes' own shutdown records and the usage-guard history,
then names the cause:
- **Same bot used by another program** (OpenClaw, another Hermes): Telegram allows one reader
  per bot, so Hermes gives up after ~3 minutes of conflicts and shuts down. Make a separate bot
  with @BotFather and put its token in `deploy/hermes.env`, then `up -d --force-recreate hermes`.
- **Usage guard** passed the monthly cap: the owner gets a Telegram message first; raise
  `CHOPS_MONTHLY_TOKEN_CAP` in `/etc/construction-hermes/usage-guard.env`, start Hermes, release
  the kill switch.
- **Stopped from outside** (installer re-run, `docker compose restart`, reboot): harmless; send
  any message once it is back.

## Backups and restore
- Nightly timer: encrypted archive (AES-256-GCM, chunked) of DB + documents, retention
  `CHOPS_BACKUP_KEEP` (default 14), then an automatic restore test into a temporary database
  that is forced into a no-send state and dropped afterwards.
- Manual: `docker compose -p construction-hermes --profile ops run --rm backup`
  and `... run --rm restore-test /backups/<file>`.
- **Off-server copy is not configured** → disaster recovery is INCOMPLETE until archives and
  the key are copied to separate off-server locations (e.g. rclone to object storage).
- Real restore (disaster): stop `web mcp worker hermes`; create an empty DB; `pg_restore` the
  archive's `db.dump`; restore `documents/` into the `chops_data` volume; engage the kill switch;
  start services; review the outbound queue before releasing.

## Kill switch
Dashboard → More → System → Engage, or `docker compose exec web chops kill-switch on --reason "..."`.
Release: owner only (`kill-switch off`). Then review UNKNOWN/blocked items in the queue.

## Rollback
1. `chops kill-switch on`; `docker compose -p construction-hermes stop hermes worker`.
2. Take a backup.
3. `git checkout <previous tag>`; `docker compose -p construction-hermes up -d --build web mcp worker`.
4. Migrations are forward-only; if the new release added a migration, restore the pre-upgrade
   backup into a fresh database instead of downgrading in place.
5. Hermes: set the image tag back in `docker-compose.yml`; `up -d hermes`.

## Upgrade
- **Service**: backup → `git pull` → `docker compose up -d --build migrate web mcp worker` →
  `chops health` → run `python -m pytest` in CI or a staging copy first.
- **Hermes**: read release notes; bump the pinned tag (and record its commit in
  `docs/ARCHITECTURE.md`); in a staging profile run `hermes mcp test construction` and
  `hermes tools list --platform telegram`; confirm disabled toolsets still hold; then deploy.
  Never use `hermes update` inside the container.

## Host (non-Docker) Hermes install
The installer script uses sudo/apt; prefer the pinned container. For a host install, use an
editable checkout of tag `v2026.9.24` in its own venv, copy `hermes/` files into a dedicated
`HERMES_HOME`, and put `CHOPS_MCP_TOKEN` and `CHOPS_MCP_URL=http://127.0.0.1:8641/mcp` in
`HERMES_HOME/.env` (mode 600).

## Rotating the Hermes token
`chops issue-agent-token --revoke-existing`, update `hermes.env`, `up -d hermes`.
