#!/usr/bin/env bash
# Local cap on attributable Hermes model usage (month-to-date tokens).
# Runs on the HOST (systemd timer), outside the agent's control. When the cap is exceeded it
# stops the Hermes gateway container and engages the operations kill switch.
# This is NOT a guarantee about provider billing: set a hard spending limit at the provider too.
set -euo pipefail
cd "$(dirname "$0")/.."
CAP="${CHOPS_MONTHLY_TOKEN_CAP:-2000000}"
DAY="$(date +%-d)"
OUT="$(docker compose -p construction-hermes exec -T hermes hermes insights --days "$DAY" 2>/dev/null || true)"
TOKENS="$(printf '%s\n' "$OUT" | sed -n 's/.*Total tokens:[^0-9]*\([0-9,]*\).*/\1/p' | head -1 | tr -d ,)"
if [ -z "$TOKENS" ]; then
  echo "usage-guard: could not read Hermes usage; leaving services as they are" >&2
  exit 2
fi
echo "usage-guard: month-to-date tokens=$TOKENS cap=$CAP"
if [ "$TOKENS" -gt "$CAP" ]; then
  docker compose -p construction-hermes stop hermes
  docker compose -p construction-hermes exec -T web chops kill-switch on --reason "usage-guard: ${TOKENS} tokens > cap ${CAP}"
  echo "usage-guard: cap exceeded; Hermes stopped and kill switch engaged" >&2
fi
