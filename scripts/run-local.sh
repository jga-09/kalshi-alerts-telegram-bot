#!/usr/bin/env bash
# Runs API, bot, worker, beat and frontend locally (Ctrl+C stops all).
set -euo pipefail
cd "$(dirname "$0")/.."
trap 'kill 0' EXIT
.venv/bin/uvicorn kalshi_ai_api.main:app --reload --port 8000 &
.venv/bin/python -m kalshi_ai_bot.main &
.venv/bin/celery -A kalshi_ai_worker.celery_app worker -Q urgent,default -l info &
.venv/bin/celery -A kalshi_ai_worker.celery_app beat -l info --schedule /tmp/kalshi-ai-beat &
(cd frontend && npm run dev) &
wait
