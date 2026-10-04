#!/usr/bin/env bash
# Explains why the Hermes gateway stopped or restarted (for example after Telegram showed
# "Hermes is shutting down"). Read-only: it changes nothing. Run on the server:
#   sudo bash /opt/construction-hermes/deploy/scripts/why-hermes-stopped.sh
set -uo pipefail
cd "$(dirname "$0")/.."
P=construction-hermes
DC=(docker compose -p "$P" --project-directory . -f docker-compose.yml)
say() { printf '%s\n' "$*"; }
found=0

say "== Hermes container"
cid="$(timeout 20 "${DC[@]}" ps -a -q hermes 2>/dev/null | head -1)"
if [ -z "$cid" ]; then
  say "  No Hermes container exists. Re-run the installer."
  exit 1
fi
docker inspect -f '  state={{.State.Status}}  started={{.State.StartedAt}}  restarts={{.RestartCount}}  last_exit={{.State.ExitCode}}  out_of_memory={{.State.OOMKilled}}' "$cid"
image="$(docker inspect -f '{{.Config.Image}}' "$cid")"
running="$(docker inspect -f '{{.State.Running}}' "$cid")"

say
say "== Stops and restarts seen by Docker (last 48 hours)"
timeout 5 docker events --since 48h --until "$(date +%s)" --filter "container=$cid" \
  --filter event=kill --filter event=stop --filter event=die --filter event=start --filter event=oom \
  --format '  {{.Time}}  {{.Action}}  {{index .Actor.Attributes "signal"}}{{index .Actor.Attributes "exitCode"}}' 2>/dev/null \
  | while read -r t a rest; do say "  $(date -d "@$t" '+%F %T %Z')  $a $rest"; done
say "  (A container that was re-created, e.g. by re-running the installer, starts a new history.)"

say
say "== Hermes' own record of why it stopped (last 15 events; times in UTC)"
# Read Hermes' log from its volume without touching the running container.
vol="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/opt/data"}}{{.Name}}{{end}}{{end}}' "$cid")"
pattern='Received SIG|planned gateway stop|No connected messaging platforms|polling conflict|Telegram polling could not recover|Fatal telegram|telegram_auth_error|exited UNCLEANLY|Exiting with code|Refusing to start|Sent shutdown notification'
events="$(timeout 60 docker run --rm --network none --user 0 --entrypoint sh -v "$vol:/d:ro" "$image" \
  -c "cat /d/logs/gateway.log.1 /d/logs/gateway.log 2>/dev/null | grep -E '$pattern' | tail -n 15" 2>/dev/null)"
if [ -n "$events" ]; then printf '%s\n' "$events" | cut -c1-260 | sed 's/^/  /'; else say "  (nothing recorded)"; fi

say
say "== Usage guard (local monthly token cap)"
guard=""
if command -v journalctl >/dev/null; then
  guard="$(journalctl -u construction-hermes-usage-guard.service --since '-30 days' --no-pager -o short-iso 2>/dev/null | grep -E 'usage-guard:' | tail -n 5)"
fi
if [ -n "$guard" ]; then printf '%s\n' "$guard" | sed 's/^/  /'; else say "  (no runs recorded yet)"; fi

say
say "== What this means"
if printf '%s' "$events" | grep -qE 'polling conflict|Telegram polling could not recover'; then
  found=1
  say "  * Another program is using the SAME Telegram bot token (for example OpenClaw or another"
  say "    Hermes on this or another server). Telegram lets only one program read a bot at a time,"
  say "    so Hermes gives up and shuts down. Fix: in Telegram, open @BotFather, send /newbot to make a"
  say "    bot just for Construction Hermes, then put its token in $(pwd)/hermes.env"
  say "    (TELEGRAM_BOT_TOKEN=...) and run:  docker compose -p $P up -d --force-recreate hermes"
fi
if printf '%s' "$guard" | grep -q 'cap exceeded'; then
  found=1
  say "  * The usage guard stopped Hermes because the monthly token cap was passed, and froze"
  say "    outbound actions (kill switch). To raise the cap: edit /etc/construction-hermes/usage-guard.env"
  say "    (CHOPS_MONTHLY_TOKEN_CAP=...), then:  docker compose -p $P start hermes"
  say "    and release the kill switch in the dashboard (More -> System)."
fi
if [ "$(docker inspect -f '{{.State.OOMKilled}}' "$cid")" = "true" ]; then
  found=1
  say "  * Hermes ran out of memory (limit 1.5 GB) and was killed. Check free memory: free -m"
fi
if printf '%s' "$events" | grep -q 'telegram_auth_error'; then
  found=1
  say "  * Telegram rejected the bot token (revoked or mistyped). Get the token again from @BotFather."
fi
if [ "$found" = 0 ] && printf '%s' "$events" | grep -q 'Received SIG'; then
  found=1
  say "  * Hermes was told to stop from outside (SIGTERM). That happens when the installer is run"
  say "    (it restarts Hermes), on 'docker compose restart/stop', a server reboot, or a Docker"
  say "    restart. Compare the times above with when you ran those. Nothing is wrong if it came back."
fi
[ "$found" = 1 ] || say "  * No known cause found in the logs above."
if [ "$running" = "true" ]; then say "  Hermes is running now: send it a message in Telegram to continue."
else say "  Hermes is NOT running now. Start it:  cd $(pwd) && docker compose -p $P start hermes"; fi
