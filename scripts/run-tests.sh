#!/usr/bin/env bash
# Lint + unit/API/security/acceptance tests on SQLite, then again on PostgreSQL if TEST_DATABASE_URL is set.
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest -q tests
if [ -n "${TEST_DATABASE_URL:-}" ]; then
  echo "== PostgreSQL run =="
  .venv/bin/pytest -q tests
fi
(cd frontend && npx tsc --noEmit)
