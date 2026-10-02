#!/usr/bin/env bash
# One-command setup for Ubuntu/Debian.
#   bash scripts/setup-linux.sh
# Installs Docker if needed, creates .env with fresh secrets, asks for your Telegram bot token
# and Telegram ID (typed locally, never sent anywhere except Telegram's own API to verify the token),
# starts everything, and makes you admin.
set -euo pipefail
cd "$(dirname "$0")/.."

say()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*"; exit 1; }

command -v apt-get >/dev/null || die "This script supports Ubuntu/Debian (apt). See docs/SETUP.md for other systems."

# --- 1. Base tools ---------------------------------------------------------------
say "Checking required tools"
need=()
for pkg in git curl python3; do command -v "$pkg" >/dev/null || need+=("$pkg"); done
python3 -c "import venv, ensurepip" 2>/dev/null || need+=("python3-venv")
if [ ${#need[@]} -gt 0 ]; then
  echo "Installing: ${need[*]} (your password may be requested)"
  sudo apt-get update -qq && sudo apt-get install -y -qq "${need[@]}"
fi

# --- 2. Docker -------------------------------------------------------------------
if [ "${SKIP_DOCKER:-0}" != "1" ]; then
  if ! command -v docker >/dev/null; then
    say "Installing Docker (official convenience script)"
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker "$USER" || true
  fi
  if docker info >/dev/null 2>&1; then DOCKER=(docker); else DOCKER=(sudo docker); fi
  "${DOCKER[@]}" compose version >/dev/null 2>&1 || die "Docker Compose plugin missing: sudo apt-get install docker-compose-plugin"
fi

# --- 3. .env with fresh secrets --------------------------------------------------
if [ ! -f .env ]; then
  say "Creating .env with freshly generated secrets"
  cp .env.example .env
  python3 -m venv /tmp/kalshi-ai-keygen >/dev/null
  /tmp/kalshi-ai-keygen/bin/pip install -q cryptography
  /tmp/kalshi-ai-keygen/bin/python scripts/gen_secrets.py >> .env
  rm -rf /tmp/kalshi-ai-keygen
  chmod 600 .env
else
  say ".env already exists - keeping your existing secrets"
fi

set_env() {  # set_env KEY VALUE  (replaces every KEY= line; appends if absent)
  python3 - "$1" "$2" <<'PY'
import sys, re, pathlib
key, val = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env"); lines = p.read_text().splitlines()
pat = re.compile(rf"^{re.escape(key)}=")
found = False
for i, l in enumerate(lines):
    if pat.match(l):
        lines[i] = f"{key}={val}"; found = True
if not found:
    lines.append(f"{key}={val}")
p.write_text("\n".join(lines) + "\n")
PY
}

# --- 4. Telegram details ---------------------------------------------------------
say "Telegram setup"
echo "1) In Telegram, message @BotFather, send /newbot, and copy the token."
while true; do
  read -r -s -p "Paste your bot token (hidden as you type): " TOKEN; echo
  [[ "$TOKEN" =~ ^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$ ]] || { warn "That doesn't look like a bot token. Try again."; continue; }
  RESP=$(curl -fsS "https://api.telegram.org/bot${TOKEN}/getMe" 2>/dev/null || true)
  USERNAME=$(printf '%s' "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['result']['username'] if d.get('ok') else '')" 2>/dev/null || true)
  if [ -n "$USERNAME" ]; then echo "Token OK - bot is @$USERNAME"; break; fi
  warn "Telegram rejected that token. Check it with @BotFather and try again."
done
echo
echo "2) Message @userinfobot in Telegram to get your numeric ID."
while true; do
  read -r -p "Your Telegram ID (numbers only): " ADMIN_ID
  [[ "$ADMIN_ID" =~ ^[0-9]{4,15}$ ]] && break
  warn "Numbers only, e.g. 123456789."
done

set_env TELEGRAM_BOT_TOKEN "$TOKEN"
set_env TELEGRAM_BOT_USERNAME "$USERNAME"
set_env ADMIN_TELEGRAM_IDS "$ADMIN_ID"
unset TOKEN
say "Saved to .env (file permissions: owner-only)"

[ "${SKIP_DOCKER:-0}" = "1" ] && { say "SKIP_DOCKER=1 - configuration done, not starting containers"; exit 0; }

# --- 5. Start everything ---------------------------------------------------------
say "Building and starting Kalshi AI (first run takes several minutes)"
"${DOCKER[@]}" compose up -d --build

say "Waiting for the API to become healthy"
for _ in $(seq 1 60); do
  if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then break; fi
  sleep 3
done
curl -fsS http://localhost:8000/health >/dev/null 2>&1 || die "API did not start. Check: ${DOCKER[*]} compose logs api"

say "Granting admin role to Telegram user $ADMIN_ID"
"${DOCKER[@]}" compose exec -T api python scripts/manage.py grant-admin "$ADMIN_ID"

cat <<EOF

$(printf '\033[1;32m')All set!$(printf '\033[0m')
  • Open Telegram, find @$USERNAME and send /start
  • Web dashboard: http://localhost:3000   (API docs: http://localhost:8000/docs)
  • Paper trading is the default; live trading is OFF (LIVE_TRADING=false in .env).

Useful commands (run in this folder):
  ${DOCKER[*]} compose logs -f bot     # watch the bot
  ${DOCKER[*]} compose down            # stop everything
  ${DOCKER[*]} compose up -d           # start again
EOF
