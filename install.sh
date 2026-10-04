#!/usr/bin/env bash
# Construction Hermes - one-command installer and onboarding for a VPS.
#
#   curl -fsSL https://raw.githubusercontent.com/tidewaterrepair-code/construction-hermes/HEAD/install.sh | sudo bash
#
# What it does (safe to re-run; a re-run upgrades and keeps your settings and data):
#   1. Checks the server (OS, RAM, disk, port) and asks a few onboarding questions.
#   2. Installs Docker if it is missing (never upgrades or reconfigures an existing Docker).
#   3. Downloads Construction Hermes to /opt/construction-hermes and generates secrets.
#   4. Builds and starts its own isolated Docker Compose project "construction-hermes"
#      (own network, volumes and limits; only the dashboard listens, on 127.0.0.1).
#   5. Creates your owner login, connects Hermes Agent to the construction tools, sets up the
#      AI model and Telegram (optional), private phone access (Tailscale, optional),
#      nightly encrypted backups, the usage guard, and starts in SHADOW mode (nothing is sent).
#   6. Verifies everything and prints a summary.
#
# It never stops, edits or removes other containers, never changes SSH or firewall settings,
# and never opens a public port.
#
# Unattended use: set CH_NONINTERACTIVE=1 and any of the CH_* variables documented below.
#   CH_OWNER_USERNAME, CH_OWNER_NAME, CH_OWNER_PASSWORD, CH_COMPANY_NAME,
#   CH_TELEGRAM_BOT_TOKEN, CH_TELEGRAM_USER_ID, CH_CHAT_APPROVALS=yes|no,
#   CH_ACCESS=tailscale|ssh, CH_TS_AUTHKEY, CH_TOKEN_CAP,
#   CH_MODEL_PROVIDER (anthropic|openrouter|nous-api|gemini), CH_MODEL, CH_MODEL_API_KEY, CH_AI_TEST=yes|no
#     (ChatGPT sign-in, provider openai-codex, needs a person to enter a code: run interactively),
#   CH_DIR (default /opt/construction-hermes), CH_REPO_URL, CH_REF, CH_WEB_PORT (default 8640),
#   CH_DOCKER_BUILD_FLAGS (extra flags for `docker build`).

set -Eeuo pipefail

main() {
  # ------------------------------------------------------------------ setup
  REPO_URL="${CH_REPO_URL:-https://github.com/tidewaterrepair-code/construction-hermes.git}"
  REF="${CH_REF:-}"
  DIR="${CH_DIR:-/opt/construction-hermes}"
  PROJECT="construction-hermes"
  ETC="/etc/construction-hermes"
  BACKUP_DIR="/var/backups/construction-hermes"
  WEB_PORT="${CH_WEB_PORT:-8640}"
  LOG="/var/log/construction-hermes-install.log"
  SUMMARY="/root/construction-hermes-install.txt"
  NONINTERACTIVE="${CH_NONINTERACTIVE:-0}"
  MISSING=()
  WARNINGS=()

  if [ "$(id -u)" -ne 0 ]; then
    echo "Please run as root, e.g.:  curl -fsSL <url>/install.sh | sudo bash" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$LOG")"; touch "$LOG"; chmod 600 "$LOG"
  # Answers come from the keyboard even when this script is piped from curl.
  if [ "$NONINTERACTIVE" != "1" ] && ( : </dev/tty ) 2>/dev/null; then
    exec </dev/tty
    INTERACTIVE=1
  else
    exec </dev/null
    INTERACTIVE=0
  fi
  # Report once, from the main shell (not from every subshell the error passes through).
  trap '[ "$BASH_SUBSHELL" = 0 ] && fail "step failed at line $LINENO (see $LOG)"' ERR

  banner
  preflight
  ask_questions
  install_prereqs
  fetch_code
  write_secrets
  build_and_start
  onboard_service
  connect_hermes
  setup_ai_model
  setup_access
  setup_systemd
  first_backup
  verify
  summary
}

# ====================================================================== helpers
c_bold=$'\e[1m'; c_dim=$'\e[2m'; c_red=$'\e[31m'; c_grn=$'\e[32m'; c_ylw=$'\e[33m'; c_off=$'\e[0m'
banner() {
  echo
  echo "${c_bold}Construction Hermes installer${c_off}"
  echo "${c_dim}Installs into ${DIR} as Docker project '${PROJECT}'. Log: ${LOG}${c_off}"
  echo
}
step() { echo; echo "${c_bold}==> $*${c_off}"; echo "==> $*" >>"$LOG"; }
ok() { echo "  ${c_grn}✓${c_off} $*"; echo "  ok: $*" >>"$LOG"; }
warn() { echo "  ${c_ylw}!${c_off} $*"; echo "  warn: $*" >>"$LOG"; WARNINGS+=("$*"); }
fail() { echo; echo "${c_red}✗ $*${c_off}" >&2; echo "Re-run the same command after fixing the problem; it is safe to repeat." >&2; exit 1; }
run() { echo "  \$ $*" >>"$LOG"; "$@" >>"$LOG" 2>&1; }
compose() { (cd "$DIR/deploy" && docker compose -p "$PROJECT" "$@"); }
cexec() { compose exec -T "$@"; }

ask() {  # ask VAR "Question" "default"
  local __var="$1" __q="$2" __def="${3:-}" __ans
  if [ -n "${!__var:-}" ]; then return; fi
  if [ "$INTERACTIVE" = "1" ]; then
    read -r -p "  $__q${__def:+ [$__def]}: " __ans || true
    printf -v "$__var" '%s' "${__ans:-$__def}"
  else
    printf -v "$__var" '%s' "$__def"
  fi
}
ask_secret() {  # ask_secret VAR "Question"
  local __var="$1" __q="$2" __ans
  if [ -n "${!__var:-}" ]; then return; fi
  if [ "$INTERACTIVE" = "1" ]; then
    read -r -s -p "  $__q: " __ans || true; echo
    printf -v "$__var" '%s' "$__ans"
  fi
}
yesno() {  # yesno "Question" default(y|n) -> returns 0 for yes
  local q="$1" def="${2:-n}" a
  if [ "$INTERACTIVE" != "1" ]; then [ "$def" = "y" ]; return; fi
  read -r -p "  $q [$( [ "$def" = y ] && echo Y/n || echo y/N )]: " a || true
  a="${a:-$def}"; [[ "$a" =~ ^[Yy] ]]
}
envset() {  # envset FILE KEY VALUE  (replace or append, keep mode 600)
  local f="$1" k="$2" v="$3" tmp
  tmp="$(mktemp)"; chmod 600 "$tmp"
  if [ -f "$f" ]; then grep -v -E "^${k}=" "$f" >"$tmp" || true; fi
  printf '%s=%s\n' "$k" "$v" >>"$tmp"
  mv "$tmp" "$f"; chmod 600 "$f"
}
envget() { [ -f "$1" ] && sed -n "s/^$2=//p" "$1" | tail -1 || true; }

# ====================================================================== 1. preflight
preflight() {
  step "Checking this server"
  . /etc/os-release 2>/dev/null || true
  case "${ID:-}" in
    ubuntu|debian) ok "OS: ${PRETTY_NAME:-$ID}" ;;
    *) warn "OS ${PRETTY_NAME:-unknown} is untested (Ubuntu 22.04/24.04 or Debian 12 recommended)" ;;
  esac
  ARCH="$(uname -m)"
  case "$ARCH" in
    x86_64|amd64) ok "CPU: $ARCH, $(nproc) core(s)" ;;
    aarch64|arm64) warn "ARM server: images may need to build/pull for arm64 (untested)" ;;
    *) fail "unsupported CPU architecture $ARCH" ;;
  esac
  MEM_MB=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) / 1024 ))
  if [ "$MEM_MB" -lt 1800 ]; then fail "only ${MEM_MB} MB RAM; at least 2 GB is required"; fi
  if [ "$MEM_MB" -lt 3500 ]; then warn "${MEM_MB} MB RAM: works, but 4 GB is more comfortable"; else ok "RAM: ${MEM_MB} MB"; fi
  local dockroot="/var/lib/docker"; [ -d "$dockroot" ] || dockroot="/var/lib"
  FREE_GB=$(( $(df -Pk "$dockroot" | awk 'NR==2{print $4}') / 1024 / 1024 ))
  if [ "$FREE_GB" -lt 12 ]; then fail "only ${FREE_GB} GB free disk; need at least 12 GB (images are ~5 GB)"; fi
  ok "Disk free: ${FREE_GB} GB"
  if [ -f "$DIR/deploy/.env" ]; then
    ok "Existing install found in $DIR: this run will upgrade it and keep your data/settings"
    local p; p="$(envget "$DIR/deploy/.env" CHOPS_WEB_PORT)"
    if [ -n "$p" ]; then WEB_PORT="$p"; fi
  fi
  if [ -f "$ETC/onboarded" ]; then EXISTING=1; else EXISTING=0; fi
  if command -v ss >/dev/null && ss -ltn "( sport = :$WEB_PORT )" | grep -q LISTEN; then
    if [ "$EXISTING" = "1" ] && docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${PROJECT}-web-"; then
      ok "Port $WEB_PORT is used by this project's dashboard"
    else
      fail "port $WEB_PORT is already used by another program. Re-run with CH_WEB_PORT=<free port>"
    fi
  else
    ok "Port $WEB_PORT is free (dashboard listens on 127.0.0.1 only)"
  fi
  if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
    OTHER=$(docker ps --format '{{.Names}}' | { grep -v "^${PROJECT}-" || true; } | wc -l | tr -d ' ')
    ok "Docker present; ${OTHER} other running container(s) will be left untouched"
    docker ps --format '{{.Names}}\t{{.Status}}' | { grep -v "^${PROJECT}-" || true; } >"/root/.construction-hermes-other-containers.before"
  fi
}

# ====================================================================== 2. questions
ask_questions() {
  step "Onboarding questions (press Enter to accept a default or skip)"
  if [ "$EXISTING" = "1" ]; then
    ok "Already onboarded; only missing items will be asked"
  fi
  if [ "$EXISTING" = "0" ]; then
    echo "  ${c_dim}Your dashboard login:${c_off}"
    ask OWNER_USERNAME "Username" "${CH_OWNER_USERNAME:-jimmy}"
    ask OWNER_NAME "Your name" "${CH_OWNER_NAME:-Jimmy Blackwell}"
    OWNER_PASSWORD="${CH_OWNER_PASSWORD:-}"
    while [ "${#OWNER_PASSWORD}" -lt 12 ]; do
      [ "$INTERACTIVE" = "1" ] || fail "CH_OWNER_PASSWORD (12+ characters) is required in unattended mode"
      ask_secret OWNER_PASSWORD "Password (12+ characters)"
      local again=""; ask_secret again "Repeat password"
      if [ "${#OWNER_PASSWORD}" -lt 12 ] || [ "$OWNER_PASSWORD" != "$again" ]; then
        echo "  ${c_ylw}Passwords must match and be at least 12 characters.${c_off}"; OWNER_PASSWORD=""
      fi
    done
    echo "  ${c_dim}Company name for proposals and invoices (leave blank if not decided; documents will say 'not configured'):${c_off}"
    ask COMPANY_NAME "Company name" "${CH_COMPANY_NAME:-}"
  fi

  if [ -z "$(envget "$DIR/deploy/hermes.env" TELEGRAM_BOT_TOKEN)" ]; then
    echo
    echo "  ${c_dim}Telegram lets you text Hermes from your phone. You need a bot token from @BotFather"
    echo "  (send /newbot) and your numeric user ID from @userinfobot. Leave blank to skip for now.${c_off}"
    ask TELEGRAM_BOT_TOKEN "Telegram bot token" "${CH_TELEGRAM_BOT_TOKEN:-}"
    if [ -n "$TELEGRAM_BOT_TOKEN" ]; then
      ask TELEGRAM_USER_ID "Your numeric Telegram user ID" "${CH_TELEGRAM_USER_ID:-}"
      [[ "$TELEGRAM_USER_ID" =~ ^[0-9]+$ ]] || fail "Telegram user ID must be digits only (from @userinfobot), not a @username"
      CHAT_APPROVALS="${CH_CHAT_APPROVALS:-}"
      if [ -z "$CHAT_APPROVALS" ]; then
        if yesno "Allow approving actions from Telegram? (only your user ID can talk to the bot)" n; then CHAT_APPROVALS=yes; else CHAT_APPROVALS=no; fi
      fi
    fi
  fi

  if [ -z "$(envget "$DIR/deploy/.env" CH_ACCESS)" ]; then
    echo
    echo "  ${c_dim}How will you open the dashboard from your phone?"
    echo "    1) Tailscale private network (recommended; free; nothing is exposed to the internet)"
    echo "    2) SSH tunnel only for now (you can add Tailscale later by re-running)${c_off}"
    local choice="${CH_ACCESS:-}"
    if [ -z "$choice" ]; then ask choice "Choose 1 or 2" "1"; fi
    case "$choice" in 1|tailscale) ACCESS=tailscale ;; *) ACCESS=ssh ;; esac
  else
    ACCESS="$(envget "$DIR/deploy/.env" CH_ACCESS)"
  fi

  if [ ! -f "$ETC/usage-guard.env" ]; then
    echo
    echo "  ${c_dim}Monthly AI usage cap: when Hermes passes this many tokens in a month, it is stopped and"
    echo "  outbound actions are frozen until you raise the cap. Also set a spending limit with your AI provider.${c_off}"
    ask TOKEN_CAP "Monthly token cap" "${CH_TOKEN_CAP:-2000000}"
    [[ "$TOKEN_CAP" =~ ^[0-9]+$ ]] || fail "token cap must be a whole number"
  fi
}

# ====================================================================== 3. prerequisites
install_prereqs() {
  step "Installing prerequisites"
  export DEBIAN_FRONTEND=noninteractive
  local need=()
  for b in git curl openssl python3; do command -v "$b" >/dev/null || need+=("$b"); done
  command -v ss >/dev/null || need+=(iproute2)
  if [ "${#need[@]}" -gt 0 ]; then
    run apt-get update
    run apt-get install -y --no-install-recommends "${need[@]}" ca-certificates
  fi
  ok "git, curl, openssl, python3"
  if ! command -v docker >/dev/null; then
    echo "  Installing Docker Engine from Docker's official apt repository..."
    run apt-get update
    run apt-get install -y --no-install-recommends ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
      >/etc/apt/sources.list.d/docker.list
    run apt-get update
    run apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    run systemctl enable --now docker || true
    ok "Docker installed"
  else
    ok "Docker already installed ($(docker --version | sed 's/Docker version //;s/,.*//')); left as is"
  fi
  docker info >/dev/null 2>&1 || fail "Docker is installed but not running (try: systemctl start docker)"
  if ! docker compose version >/dev/null 2>&1; then
    if apt-cache policy docker-compose-plugin 2>/dev/null | grep -q Candidate; then
      run apt-get install -y docker-compose-plugin
    fi
    docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 plugin is missing (install docker-compose-plugin)"
  fi
  ok "Docker Compose $(docker compose version --short)"
}

# ====================================================================== 4. code
fetch_code() {
  step "Downloading Construction Hermes"
  if [ -d "$DIR/.git" ]; then
    run git -C "$DIR" fetch --prune origin
    local target="${REF:-$(git -C "$DIR" rev-parse --abbrev-ref origin/HEAD 2>/dev/null | sed 's#^origin/##')}"
    target="${target:-$(git -C "$DIR" rev-parse --abbrev-ref HEAD)}"
    run git -C "$DIR" checkout -q "$target"
    run git -C "$DIR" merge --ff-only "origin/$target" || warn "local changes in $DIR; kept them and skipped the code update"
  else
    mkdir -p "$(dirname "$DIR")"
    if [ -n "$REF" ]; then run git clone --branch "$REF" "$REPO_URL" "$DIR"; else run git clone "$REPO_URL" "$DIR"; fi
  fi
  chmod 750 "$DIR"
  ok "Code at $DIR ($(git -C "$DIR" rev-parse --short HEAD))"
}

# ====================================================================== 5. secrets
write_secrets() {
  step "Preparing configuration and secrets"
  mkdir -p "$ETC" "$BACKUP_DIR"; chmod 700 "$ETC"
  local env="$DIR/deploy/.env" henv="$DIR/deploy/hermes.env"
  touch "$env" "$henv"; chmod 600 "$env" "$henv"
  local k
  for k in POSTGRES_SUPERUSER_PASSWORD CHOPS_OWNER_DB_PASSWORD CHOPS_APP_DB_PASSWORD; do
    [ -n "$(envget "$env" "$k")" ] || envset "$env" "$k" "$(openssl rand -hex 24)"
  done
  [ -n "$(envget "$env" CHOPS_SECRET_KEY)" ] || envset "$env" CHOPS_SECRET_KEY "$(openssl rand -hex 32)"
  envset "$env" CHOPS_ENV prod
  envset "$env" CHOPS_WEB_PORT "$WEB_PORT"
  envset "$env" CHOPS_BACKUP_HOST_DIR "$BACKUP_DIR"
  envset "$env" CHOPS_BACKUP_KEY_HOST_FILE "$ETC/backup.key"
  [ -n "$(envget "$env" CHOPS_BACKUP_KEEP)" ] || envset "$env" CHOPS_BACKUP_KEEP 14
  envset "$env" CH_ACCESS "$ACCESS"
  if [ -z "$(envget "$env" CHOPS_BASE_URL)" ]; then
    envset "$env" CHOPS_BASE_URL "http://127.0.0.1:${WEB_PORT}"
    envset "$env" CHOPS_COOKIE_SECURE false
  fi
  envset "$henv" CHOPS_MCP_URL "http://mcp:8641/mcp"
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
    envset "$henv" TELEGRAM_BOT_TOKEN "$TELEGRAM_BOT_TOKEN"
    envset "$henv" TELEGRAM_ALLOWED_USERS "$TELEGRAM_USER_ID"
  fi
  if [ ! -s "$ETC/backup.key" ]; then
    (umask 077; head -c 32 /dev/urandom | base64 >"$ETC/backup.key")
    NEW_BACKUP_KEY=1
  fi
  # Containers run as uid 10001; they need to read the key and write backups.
  chown 10001:10001 "$ETC/backup.key" "$BACKUP_DIR"; chmod 400 "$ETC/backup.key"; chmod 700 "$BACKUP_DIR"
  if [ -n "${TOKEN_CAP:-}" ]; then
    printf 'CHOPS_MONTHLY_TOKEN_CAP=%s\n' "$TOKEN_CAP" >"$ETC/usage-guard.env"; chmod 600 "$ETC/usage-guard.env"
  fi
  ok "Secrets ready in $DIR/deploy/.env and hermes.env (mode 600, existing values kept); backup key in $ETC/backup.key"
}

# ====================================================================== 6. build & start
build_and_start() {
  step "Building and starting the services (first run takes several minutes)"
  # shellcheck disable=SC2086
  (cd "$DIR" && docker build ${CH_DOCKER_BUILD_FLAGS:-} -f deploy/Dockerfile -t construction-hermes/chops:0.1.0 . >>"$LOG" 2>&1) \
    || fail "image build failed (see $LOG)"
  ok "Service image built"
  run compose pull db hermes || warn "could not pull db/hermes images now; will retry on start"
  run compose up -d --no-build db migrate web mcp worker || fail "services failed to start (see $LOG)"
  local i
  for i in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:${WEB_PORT}/healthz" >/dev/null 2>&1; then break; fi
    sleep 2
  done
  curl -fsS "http://127.0.0.1:${WEB_PORT}/healthz" >/dev/null 2>&1 || fail "dashboard did not become healthy (see: docker compose -p $PROJECT logs web)"
  ok "Database migrated; dashboard, tool server and worker running"
}

# ====================================================================== 7. onboarding
onboard_service() {
  step "Setting up your account and business basics"
  if [ -n "${OWNER_PASSWORD:-}" ]; then
    # Passed through the environment, never on a command line.
    CHOPS_OWNER_PASSWORD="$OWNER_PASSWORD" cexec -e CHOPS_OWNER_PASSWORD web chops bootstrap-owner --if-missing \
      --username "$OWNER_USERNAME" --display-name "$OWNER_NAME" >>"$LOG" 2>&1 || fail "could not create the owner account"
    ok "Owner login ready: ${OWNER_USERNAME}"
  else
    ok "Owner account already exists"
  fi
  if [ -n "${COMPANY_NAME:-}" ]; then
    run cexec web chops set company_name "$COMPANY_NAME" && ok "Company name set"
  fi
  if [ "${CHAT_APPROVALS:-}" = "yes" ]; then run cexec web chops set channel_approvals true && ok "Chat approvals enabled"; fi
  if [ "${CHAT_APPROVALS:-}" = "no" ]; then run cexec web chops set channel_approvals false; fi
  if [ "$EXISTING" = "0" ]; then
    run cexec web chops mode SHADOW
    ok "Mode: SHADOW (records and drafts only; nothing is sent)"
  fi
  date -Is >"$ETC/onboarded"
}

connect_hermes() {
  step "Connecting Hermes Agent to the construction tools"
  local henv="$DIR/deploy/hermes.env" out attempt
  run compose up -d --no-build hermes-init || true
  for attempt in 1 2; do
    if [ -z "$(envget "$henv" CHOPS_MCP_TOKEN)" ] || [ "$attempt" = 2 ]; then
      local tok
      tok="$(cexec web chops issue-agent-token --revoke-existing 2>>"$LOG" | { grep -E '^chops_' || true; } | tail -1)"
      [ -n "$tok" ] || fail "could not issue the Hermes service token"
      envset "$henv" CHOPS_MCP_TOKEN "$tok"
      ok "Service token issued for Hermes (stored only in hermes.env)"
    fi
    run compose up -d --no-build --force-recreate hermes || fail "Hermes failed to start (see $LOG)"
    sleep 5
    out="$(cexec hermes hermes mcp test construction 2>&1 || true)"; echo "$out" >>"$LOG"
    if echo "$out" | grep -q "Tools discovered"; then break; fi
  done
  if echo "$out" | grep -q "Tools discovered"; then
    ok "Hermes reaches the construction tools ($(echo "$out" | sed -n 's/.*Tools discovered: \([0-9]*\).*/\1/p') tools)"
  else
    fail "Hermes could not reach the construction tools (see $LOG)"
  fi
  out="$(cexec hermes hermes tools list --platform telegram 2>&1 || true)"; echo "$out" >>"$LOG"
  if echo "$out" | grep -qE "✓ enabled +(terminal|file|browser|code_execution|web) "; then
    fail "safety check failed: Hermes has shell/file/browser/web tools enabled"
  fi
  ok "Safety check: Hermes has no shell, file, browser, web or code tools"
}

chatgpt_signin() {
  echo
  echo "  ${c_bold}ChatGPT sign-in${c_off} (Hermes' own login; it does not affect the ChatGPT app or Codex CLI)."
  echo "  Hermes will print a link and a short code. Open the link on your phone or computer,"
  echo "  sign in to ChatGPT, and enter the code. This window waits until you finish."
  if ! (cd "$DIR/deploy" && docker compose -p "$PROJECT" exec hermes hermes auth add openai-codex --type oauth); then
    echo
    echo "  If the error above mentions TLS/SSL, some networks reject the newer TLS handshake."
    echo "  Hermes documents a workaround (classic TLS key-exchange groups); it can be applied for Hermes only."
    if yesno "Retry the sign-in with the compatible TLS setting?" y; then
      cexec hermes sh -c 'cat > /opt/data/openssl-classic.cnf <<CNF
openssl_conf = openssl_init
[openssl_init]
ssl_conf = ssl_sect
[ssl_sect]
system_default = system_default_sect
[system_default_sect]
Groups = x25519:secp256r1:secp384r1:x448
CNF' >>"$LOG" 2>&1 || true
      if (cd "$DIR/deploy" && docker compose -p "$PROJECT" exec -e OPENSSL_CONF=/opt/data/openssl-classic.cnf hermes hermes auth add openai-codex --type oauth); then
        # The same network will need it for model calls too.
        envset "$DIR/deploy/hermes.env" OPENSSL_CONF /opt/data/openssl-classic.cnf
        run compose up -d --no-build --force-recreate hermes
        ok "Signed in using the compatible TLS setting (kept for Hermes)"
      else
        warn "ChatGPT sign-in did not finish"
        return
      fi
    else
      warn "ChatGPT sign-in did not finish"
      return
    fi
  fi
  echo
  echo "  Signed in. Now pick the model: in the next menu choose ${c_bold}ChatGPT or Codex Subscription${c_off},"
  echo "  then the model you want (you will not be asked to sign in again)."
  (cd "$DIR/deploy" && docker compose -p "$PROJECT" exec hermes hermes model) || warn "model selection did not finish"
  local prov
  prov="$(cexec hermes hermes config get model.provider 2>/dev/null | tail -1 | tr -d '"[:space:]' || true)"
  if [ "$prov" != "openai-codex" ]; then
    warn "the selected provider is '${prov:-none}', not the ChatGPT subscription (openai-codex); re-run to change it"
  fi
  run compose restart hermes
}

model_configured() {
  local m
  m="$(cexec hermes hermes config get model.default 2>/dev/null | tail -1 | tr -d '"[:space:]' || true)"
  [ -n "$m" ] && [ "$m" != "None" ] && [ "$m" != "null" ]
}

setup_ai_model() {
  step "AI model for Hermes"
  if model_configured; then
    ok "Model already configured; keeping it"
  elif [ -n "${CH_MODEL_PROVIDER:-}" ] && [ -n "${CH_MODEL:-}" ] && [ -n "${CH_MODEL_API_KEY:-}" ]; then
    local var
    case "$CH_MODEL_PROVIDER" in
      anthropic) var=ANTHROPIC_API_KEY ;; openrouter) var=OPENROUTER_API_KEY ;;
      nous-api) var=NOUS_API_KEY ;; gemini) var=GEMINI_API_KEY ;;
      *) fail "CH_MODEL_PROVIDER must be anthropic, openrouter, nous-api or gemini" ;;
    esac
    envset "$DIR/deploy/hermes.env" "$var" "$CH_MODEL_API_KEY"
    run cexec hermes hermes config set model.provider "$CH_MODEL_PROVIDER"
    run cexec hermes hermes config set model.default "$CH_MODEL"
    run compose up -d --no-build --force-recreate hermes
    ok "Model set: $CH_MODEL_PROVIDER / $CH_MODEL"
  elif [ "${CH_MODEL_PROVIDER:-}" = "openai-codex" ] && [ "$INTERACTIVE" != "1" ]; then
    warn "ChatGPT sign-in needs you to enter a code; run the installer interactively (or the command in the summary)"
  elif [ "$INTERACTIVE" = "1" ]; then
    echo "  How should Hermes reach an AI model?"
    echo "    1) Sign in with my ChatGPT account (uses your ChatGPT plan; no API key)"
    echo "    2) API key or another provider (Anthropic, OpenRouter, Nous Portal, ...)"
    echo "    3) Skip for now"
    local how=""; ask how "Choose 1, 2 or 3" "1"
    case "$how" in
      1) chatgpt_signin ;;
      2)
        echo "  Hermes will show its own picker: choose a provider, sign in or paste a key, then pick a model."
        (cd "$DIR/deploy" && docker compose -p "$PROJECT" exec hermes hermes model) || warn "model setup did not finish"
        run compose restart hermes
        ;;
      *) : ;;
    esac
  fi
  if model_configured; then
    AI_READY=1
    local test="${CH_AI_TEST:-}"
    if [ -z "$test" ]; then if yesno "Test it now with one short request? (uses a few cents of tokens at most)" y; then test=yes; else test=no; fi; fi
    if [ "$test" = "yes" ]; then
      local reply rc=0
      echo "  (waiting for the AI provider; up to 4 minutes)"
      reply="$(timeout 240 bash -c "cd '$DIR/deploy' && docker compose -p '$PROJECT' exec -T hermes hermes chat -q 'Call the whats_next tool and reply with its text only, nothing else.'" 2>&1)" || rc=$?
      echo "$reply" >>"$LOG"
      if [ "$rc" != 0 ] || echo "$reply" | grep -qiE "Provider said|error|invalid|unauthori[sz]ed|No authenticated|temporarily unavailable"; then
        warn "AI test failed: check the API key and model, and that this server can reach the provider (details in $LOG)"; AI_READY=0
      else
        ok "Hermes answered:"; echo "$reply" | grep -vE '^\s*$|Resume this session|hermes --resume|hermes -c|^Session:|^Title:|^Duration:|^Messages:' | tail -10 | sed 's/^/      /' || true
      fi
    fi
  else
    AI_READY=0
    MISSING+=("AI model: re-run the installer and choose 'Sign in with my ChatGPT account', or run  cd $DIR/deploy && docker compose -p $PROJECT exec hermes hermes auth add openai-codex --type oauth  then  ... exec hermes hermes model  then  docker compose -p $PROJECT restart hermes")
    warn "No AI model configured yet; Hermes can't chat until you add one (see summary)"
  fi
}

# ====================================================================== access
setup_access() {
  step "Phone access"
  local env="$DIR/deploy/.env"
  if [ "$ACCESS" = "tailscale" ]; then
    if ! command -v tailscale >/dev/null; then
      echo "  Installing Tailscale (official installer)..."
      curl -fsSL https://tailscale.com/install.sh -o /tmp/tailscale-install.sh
      run sh /tmp/tailscale-install.sh || { warn "Tailscale install failed; using SSH tunnel access"; ACCESS=ssh; }
    fi
  fi
  if [ "$ACCESS" = "tailscale" ]; then
    if ! tailscale status >/dev/null 2>&1; then
      if [ -n "${CH_TS_AUTHKEY:-}" ]; then
        run tailscale up --authkey "$CH_TS_AUTHKEY" || true
      elif [ "$INTERACTIVE" = "1" ]; then
        echo "  Tailscale will print a login link. Open it on your phone or computer and sign in"
        echo "  (install the Tailscale app on your phone with the same account)."
        tailscale up || true
      fi
    fi
    if tailscale status >/dev/null 2>&1; then
      local name
      name="$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
      # Keep any existing Tailscale serve setup (other services) intact: use port 8443 if 443 is taken.
      local https_port=443 suffix=""
      if tailscale serve status 2>/dev/null | grep -q "https://" && ! tailscale serve status 2>/dev/null | grep -q "127.0.0.1:${WEB_PORT}"; then
        https_port=8443; suffix=":8443"
      fi
      if tailscale serve --bg --https="$https_port" "http://127.0.0.1:${WEB_PORT}" >>"$LOG" 2>&1; then
        envset "$env" CHOPS_BASE_URL "https://${name}${suffix}"
        envset "$env" CHOPS_COOKIE_SECURE true
        run compose up -d --no-build web mcp worker
        DASH_URL="https://${name}${suffix}"
        ok "Dashboard on your private Tailscale network: $DASH_URL"
      else
        warn "Tailscale is connected but 'tailscale serve' failed (enable HTTPS certificates in the Tailscale admin console, then re-run)"
        ACCESS=ssh
      fi
    else
      warn "Tailscale not logged in; using SSH tunnel access for now (re-run later to finish)"
      ACCESS=ssh
    fi
  fi
  if [ "$ACCESS" = "ssh" ]; then
    envset "$env" CHOPS_BASE_URL "http://127.0.0.1:${WEB_PORT}"
    envset "$env" CHOPS_COOKIE_SECURE false
    run compose up -d --no-build web mcp worker
    DASH_URL="http://127.0.0.1:${WEB_PORT}  (via SSH tunnel: ssh -L ${WEB_PORT}:127.0.0.1:${WEB_PORT} <user>@<server>)"
    ok "Dashboard reachable through an SSH tunnel (nothing exposed publicly)"
  fi
  envset "$env" CH_ACCESS "$ACCESS"
}

# ====================================================================== systemd, backups
setup_systemd() {
  step "Start on boot, nightly backups, usage guard"
  if [ "$(ps -o comm= -p 1 2>/dev/null)" != "systemd" ]; then
    warn "systemd is not running on this machine; boot start, backup and usage-guard timers were not installed"
    return
  fi
  local u
  for u in "$DIR"/deploy/systemd/*.service "$DIR"/deploy/systemd/*.timer; do
    sed "s#/opt/construction-hermes#${DIR}#g" "$u" >"/etc/systemd/system/$(basename "$u")"
  done
  chmod +x "$DIR/deploy/scripts/usage-guard.sh"
  run systemctl daemon-reload
  run systemctl enable construction-hermes.service
  run systemctl enable --now construction-hermes-backup.timer construction-hermes-usage-guard.timer
  ok "Starts on boot; nightly encrypted backup at 02:17 ET with restore check; hourly usage guard"
}

first_backup() {
  step "First backup and restore check"
  if run compose --profile ops run --rm backup; then
    local latest
    latest="$(ls -1t "$BACKUP_DIR"/chops-*.tar.enc 2>/dev/null | head -1 || true)"
    if [ -n "$latest" ] && run compose --profile ops run --rm restore-test "/backups/$(basename "$latest")"; then
      ok "Encrypted backup created and verified by restoring it into a throwaway database"
    else
      warn "backup was created but the restore check failed (see $LOG)"
    fi
  else
    warn "first backup failed (see $LOG); nightly backups will retry"
  fi
  MISSING+=("Off-server backups: copy $BACKUP_DIR and the key $ETC/backup.key to somewhere other than this server (until then a server loss loses the data)")
}

# ====================================================================== verify & summary
verify() {
  step "Final checks"
  local h
  h="$(cexec web chops health 2>&1 || true)"; echo "$h" >>"$LOG"
  if echo "$h" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d["ok"] else 1)' 2>/dev/null; then
    ok "Health: database, migrations, worker, storage all OK"
  else
    warn "health check reported a problem: run  docker compose -p $PROJECT exec web chops health"
  fi
  if [ -f "/root/.construction-hermes-other-containers.before" ]; then
    local before after
    before="$(cut -f1 /root/.construction-hermes-other-containers.before | sort)"
    after="$(docker ps --format '{{.Names}}' | { grep -v "^${PROJECT}-" || true; } | sort)"
    if [ "$before" = "$after" ]; then ok "Your other containers are still running ($(printf '%s' "$before" | { grep -c . || true; }))"
    else warn "the set of other running containers changed during install; please check them: docker ps"; fi
  fi
}

summary() {
  local company tg model
  company="$(cexec web python3 -c 'from chops.db import session_scope; from chops.services import settings
with session_scope() as s: print(settings.get(s,"company_name") or "")' 2>/dev/null || true)"
  [ -n "$company" ] || MISSING+=("Company name, contact and terms: dashboard -> More -> Settings")
  MISSING+=("Pricing policy (overhead, profit method/%, contingency, labor burden, tax mode) and real rates: dashboard -> Settings, Estimates")
  tg="$(envget "$DIR/deploy/hermes.env" TELEGRAM_BOT_TOKEN)"
  [ -n "$tg" ] || MISSING+=("Telegram: re-run the installer and enter a bot token (@BotFather) and your user ID (@userinfobot)")
  {
    echo "Construction Hermes - install summary ($(date -Is))"
    echo "Code: $DIR ($(git -C "$DIR" rev-parse --short HEAD))   Docker project: $PROJECT"
    echo "Dashboard: $DASH_URL"
    echo "Login: the username you chose (password is not stored here)"
    echo "Mode: SHADOW - drafts and records only; nothing is sent to customers/vendors until you switch to LIVE in More -> System."
    echo "Telegram: $([ -n "$tg" ] && echo "connected to the bot; send it a message to start" || echo "not set up")"
    echo "AI model: $([ "${AI_READY:-0}" = 1 ] && echo "ready ($(cexec hermes hermes config get model.provider 2>/dev/null | tail -1 | tr -d '"[:space:]' || true) / $(cexec hermes hermes config get model.default 2>/dev/null | tail -1 | tr -d '"[:space:]' || true))" || echo "not set up")"
    echo "Backups: $BACKUP_DIR (encrypted; key $ETC/backup.key)"
    echo
    echo "Still needed from you:"
    local m; for m in "${MISSING[@]}"; do echo "  - $m"; done
    if [ "${#WARNINGS[@]}" -gt 0 ]; then echo; echo "Warnings:"; for m in "${WARNINGS[@]}"; do echo "  - $m"; done; fi
    echo
    echo "Useful commands:"
    echo "  Status:      cd $DIR/deploy && docker compose -p $PROJECT ps"
    echo "  Health:      docker compose -p $PROJECT exec web chops health"
    echo "  Logs:        docker compose -p $PROJECT logs -f --tail 100 hermes web worker"
    echo "  Kill switch: docker compose -p $PROJECT exec web chops kill-switch on --reason \"...\""
    echo "  AI plan usage (ChatGPT 5-hour/weekly limits): docker compose -p $PROJECT exec hermes hermes usage"
    echo "  Re-do ChatGPT sign-in: docker compose -p $PROJECT exec hermes hermes auth add openai-codex --type oauth"
    echo "  Upgrade / change answers: run the same install command again"
    echo "  Docs: $DIR/docs/RUNBOOK.md"
  } >"$SUMMARY"
  chmod 600 "$SUMMARY"
  echo
  echo "${c_bold}${c_grn}Construction Hermes is installed.${c_off}"
  echo
  cat "$SUMMARY"
  if [ "${NEW_BACKUP_KEY:-0}" = "1" ]; then
    echo
    echo "${c_ylw}Save your backup key somewhere safe and OFF this server (password manager). Without it, backups cannot be restored:${c_off}"
    echo "  sudo cat $ETC/backup.key"
  fi
  echo
  echo "(This summary is saved in $SUMMARY)"
}

main "$@"
