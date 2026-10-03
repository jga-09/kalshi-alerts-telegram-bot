#!/usr/bin/env bash
# Diagnose why the Telegram bot isn't answering:  bash scripts/check-bot.sh
cd "$(dirname "$0")/.."
if docker info >/dev/null 2>&1; then D=(docker); else D=(sudo docker); fi
echo "== Container status =="; "${D[@]}" compose ps bot
echo; echo "== Bot messages (most recent last) =="
"${D[@]}" compose logs --no-log-prefix --tail=300 bot 2>&1 \
  | grep -E "Connected to Telegram|rejected TELEGRAM_BOT_TOKEN|Cannot reach|Bot stopped|Error|error|Exception" \
  | grep -v -E "Unclosed (client session|connector)" | tail -8
echo; echo "== Can this computer reach Telegram? =="
curl -s -o /dev/null -w "api.telegram.org answered HTTP %{http_code}\n" https://api.telegram.org || echo "NO - no internet from Linux"
