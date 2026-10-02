#!/usr/bin/env bash
# Local (non-Docker) development setup. Requires Python 3.12+, PostgreSQL 16, Redis 7, Node 22.
set -euo pipefail
cd "$(dirname "$0")/.."
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"
if [ ! -f .env ]; then
  cp .env.example .env
  .venv/bin/python scripts/manage.py gen-secrets >> .env
  echo "Created .env with fresh secrets - now add TELEGRAM_BOT_TOKEN, Stripe keys, ADMIN_TELEGRAM_IDS."
fi
.venv/bin/alembic -c backend/alembic.ini upgrade head
(cd frontend && npm ci)
echo "Done. Start services with: scripts/run-local.sh"
