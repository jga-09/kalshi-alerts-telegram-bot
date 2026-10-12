# Setup

## Prerequisites

| Tool | Version |
|---|---|
| Python | 3.12+ |
| PostgreSQL | 16 |
| Redis | 7 |
| Node.js | 22 |
| Docker (optional) | 24+ with Compose v2 |

## Option A — Docker (recommended)

```bash
git clone <repo> kalshi-ai && cd kalshi-ai
cp .env.example .env
pip install cryptography && python3 scripts/gen_secrets.py >> .env
$EDITOR .env          # TELEGRAM_BOT_TOKEN, ADMIN_TELEGRAM_IDS (+ Stripe test keys if testing billing)
docker compose up --build
docker compose exec api python scripts/manage.py grant-admin <your_telegram_id>
```

Services: `postgres`, `redis`, `migrate` (runs `alembic upgrade head` once), `api` (:8000), `bot`, `worker`,
`beat`, `frontend` (:3000).

## Option B — Local processes

```bash
# 1. Databases (example for Debian/Ubuntu)
sudo apt install postgresql-16 redis-server
sudo -u postgres psql -c "CREATE ROLE kalshi LOGIN PASSWORD 'kalshi';"
sudo -u postgres createdb -O kalshi kalshi_ai
sudo -u postgres createdb -O kalshi kalshi_ai_test

# 2. Python + frontend + migrations + .env with fresh secrets
scripts/dev-setup.sh

# 3. Admin access (both are required)
#    a) put your Telegram numeric ID in ADMIN_TELEGRAM_IDS in .env
#    b) grant the backend role:
.venv/bin/python scripts/manage.py grant-admin <your_telegram_id>

# 4. Run everything
scripts/run-local.sh
```

Find your Telegram ID by messaging `@userinfobot`.

## Credentials — where to get them

| Variable | Where | Required? |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram `@BotFather` → `/newbot` | Yes (bot) |
| `TELEGRAM_BOT_USERNAME` | The bot's username from BotFather | Yes (web login) |
| `JWT_SECRET`, `ENCRYPTION_KEYS`, `CODE_HASH_PEPPER`, `INTERNAL_API_TOKEN` | `python3 scripts/gen_secrets.py` | Yes |
| `STRIPE_SECRET_KEY` | dashboard.stripe.com → Developers → API keys (use a **restricted** key) | For billing |
| `STRIPE_WEBHOOK_SECRET` | Developers → Webhooks → endpoint → Signing secret | For billing |
| `STRIPE_PRICE_*` | Products → create product per plan → monthly & yearly prices | For billing |
| `FRED_API_KEY` | fred.stlouisfed.org/docs/api/api_key.html | Optional |
| `NEWS_API_KEY` | newsapi.org (check commercial licensing) | Optional |
| `REDDIT_CLIENT_ID/SECRET` | reddit.com/prefs/apps (review Reddit Data API terms) | Optional |
| `X_BEARER_TOKEN` | developer.x.com (search needs a paid tier) | Optional |
| `ANTHROPIC_API_KEY` | console.anthropic.com (only if `LLM_ENABLED=true`) | Optional |
| Kalshi | **None at platform level.** Each customer connects their own key via `/connect`. | — |

For Telegram web login, set the bot's domain in BotFather: `/setdomain` → your frontend domain.

## Local Stripe webhooks

```bash
stripe login
stripe listen --forward-to localhost:8000/api/stripe/webhook   # prints whsec_... -> STRIPE_WEBHOOK_SECRET
stripe trigger customer.subscription.created
```

## Kalshi demo account

Use `KALSHI_ENV=demo` until you have completed the checklist in `docs/KALSHI_INTEGRATION.md`.
Create a demo account at Kalshi's demo environment and an API key there for testing `/connect`.

## Verifying the install

```bash
curl localhost:8000/health        # {"status":"ok"}
curl localhost:8000/ready         # database + redis ok
scripts/run-tests.sh
```
