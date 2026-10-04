#!/usr/bin/env bash
# Local cap on attributable Hermes model usage (month-to-date tokens).
# Runs on the HOST (systemd timer), outside the agent's control. When the cap is exceeded it
# tells the owner on Telegram why, stops the Hermes gateway container and engages the
# operations kill switch.
# Counted: new input + output tokens. Prompt-cache re-reads are not counted: every tool step
# re-sends the same instructions, and counting those would trip the cap after a few chats.
# This is NOT a guarantee about provider billing: set a hard spending limit at the provider too.
set -euo pipefail
cd "$(dirname "$0")/.."
CAP="${CHOPS_MONTHLY_TOKEN_CAP:-2000000}"
DAY="$(date +%-d)"
DOCKER="${DOCKER:-docker}"
OUT="$("$DOCKER" compose -p construction-hermes exec -T hermes hermes insights --days "$DAY" 2>/dev/null </dev/null || true)"
num() { printf '%s\n' "$OUT" | sed -n "s/.*$1:[^0-9]*\([0-9,]*\).*/\1/p" | head -1 | tr -d ,; }
IN="$(num 'Input tokens')"; OUTP="$(num 'Output tokens')"
if [ -z "$IN" ] || [ -z "$OUTP" ]; then
  echo "usage-guard: could not read Hermes usage; leaving services as they are" >&2
  exit 2
fi
TOKENS=$((IN + OUTP))
echo "usage-guard: month-to-date tokens=$TOKENS (input $IN + output $OUTP) cap=$CAP"
[ "$TOKENS" -gt "$CAP" ] || exit 0

# Tell the owner first, so the "Hermes is shutting down" notice is not a mystery.
henv=hermes.env
tg_token="$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' "$henv" 2>/dev/null | tail -1)"
owner="$(sed -n 's/^TELEGRAM_ALLOWED_USERS=//p' "$henv" 2>/dev/null | tail -1 | cut -d, -f1 | tr -d ' ')"
if [ -n "$tg_token" ] && [ -n "$owner" ]; then
  msg="Construction Hermes usage guard: this month's AI usage (${TOKENS} tokens) passed your cap (${CAP}). Hermes is being stopped and outbound actions are frozen. To continue, raise CHOPS_MONTHLY_TOKEN_CAP in /etc/construction-hermes/usage-guard.env on the server, run: docker compose -p construction-hermes start hermes, then release the kill switch in the dashboard (More -> System)."
  # Token goes through curl's stdin config, not the command line (not visible in ps).
  printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$tg_token" |
    curl -sS -m 15 -o /dev/null -K - --data-urlencode "chat_id=${owner}" --data-urlencode "text=${msg}" ||
    echo "usage-guard: could not send the Telegram notice" >&2
fi
"$DOCKER" compose -p construction-hermes stop hermes
"$DOCKER" compose -p construction-hermes exec -T web chops kill-switch on --reason "usage-guard: ${TOKENS} tokens > cap ${CAP}" </dev/null
echo "usage-guard: cap exceeded; Hermes stopped and kill switch engaged" >&2
