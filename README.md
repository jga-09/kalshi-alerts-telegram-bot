# Kalshi AI

A subscription-based Telegram SaaS that analyzes Kalshi markets with market data, order-book flow, technicals,
news, social sentiment and macro data. It produces **structured, evidence-based probability estimates**, runs
**paper trading by default**, and — only after explicit opt-in and confirmation — can trade a customer's **own**
Kalshi account through the **official Kalshi API**, subject to a deterministic risk engine and emergency stops.

> AI probabilities are estimates, not guarantees. Trading involves risk; automated trading can lose money.
> Past performance does not guarantee future results.

## Architecture

```
DATA ──► FEATURES ──► MODEL ──► SIGNAL ──► RISK ENGINE ──► EXECUTION VALIDATOR ──► KALSHI
 │         │            │         │             │                    │
 │  point-in-time   ensemble   edge vs ask   deterministic     re-reads kill switches,
 │  filter + hash   (+ bounded  + idempotency  limits, sizing   hard caps; persists
 │                  LLM notes)   key                            PENDING before sending
 └── source health feeds confidence; missing data => lower confidence, never invented values
```

| Component | Path | Tech |
|---|---|---|
| Domain core (models, services, engines) | `shared/kalshi_ai` | Python 3.12, SQLAlchemy 2, Pydantic 2 |
| REST API, Stripe webhooks, admin API | `backend/kalshi_ai_api` | FastAPI |
| DB migrations | `backend/migrations` | Alembic (PostgreSQL 16) |
| Telegram bot | `bot/kalshi_ai_bot` | aiogram 3 |
| Background jobs | `worker/kalshi_ai_worker` | Celery + Redis |
| Admin dashboard & customer web pages | `frontend` | Next.js 16, TypeScript, Tailwind 4 |
| Tests | `tests` | pytest (unit, API, bot, security, acceptance) |
| Deployment | `docker`, `docker-compose.yml` | Docker, Caddy (HTTPS) |

## Easiest: one-command setup (Ubuntu/Debian)

```bash
git clone https://github.com/jga-09/kalshi-alerts-telegram-bot.git && cd kalshi-alerts-telegram-bot
git checkout claude/kalshi-ai-saas-ivlmc2
bash scripts/setup-linux.sh
```

It installs Docker if needed, generates secrets, asks for your bot token and Telegram ID (verified with Telegram),
starts everything and makes you admin.

## Quick start (Docker, manual)

```bash
cp .env.example .env
pip install cryptography && python3 scripts/gen_secrets.py >> .env   # later values override the blanks above
# edit .env: TELEGRAM_BOT_TOKEN, ADMIN_TELEGRAM_IDS (+ Stripe test keys to test billing)
docker compose up --build
docker compose exec api python scripts/manage.py grant-admin <your_telegram_id>
```

API: http://localhost:8000/docs · Web: http://localhost:3000 · Bot: message your bot `/start`.

Without Docker, see [docs/SETUP.md](docs/SETUP.md) (`scripts/dev-setup.sh` + `scripts/run-local.sh`).

## Tests

```bash
scripts/run-tests.sh                                   # lint + all tests on SQLite + frontend typecheck
TEST_DATABASE_URL=postgresql+asyncpg://kalshi:kalshi@localhost:5432/kalshi_ai_test scripts/run-tests.sh
```

150 tests cover security primitives, Kalshi signing/parsing, subscriptions, access codes, Stripe webhooks, the
API (RBAC, CSRF, rate limits), the bot (real dispatcher), every analysis engine, the risk engine, the execution
pipeline (each failure case), monitoring, backtesting (look-ahead guards) and a 25-step end-to-end acceptance test.

## Documentation

| Doc | Contents |
|---|---|
| [SETUP](docs/SETUP.md) | Local setup, exact commands, where to get every credential |
| [DEPLOYMENT](docs/DEPLOYMENT.md) | Production deployment, HTTPS, backups, scaling, runbook |
| [TRADING_ENGINE](docs/TRADING_ENGINE.md) | Features, models, edge, risk profiles, execution pipeline, kill switches |
| [KALSHI_INTEGRATION](docs/KALSHI_INTEGRATION.md) | Auth scheme, endpoints, connection flow, verification evidence |
| [TELEGRAM](docs/TELEGRAM.md) | Commands, flows, webhook vs polling |
| [STRIPE](docs/STRIPE.md) | Products/prices, webhooks, lifecycle mapping, refunds |
| [SECURITY](SECURITY.md) | Threat model, controls, secret handling, reporting |
| [BACKTESTING](docs/BACKTESTING.md) | Replay framework and its limitations |
| [ADMIN_GUIDE](docs/ADMIN_GUIDE.md) | Dashboard, codes, users, kill switch, monitoring |
| [CUSTOMER_GUIDE](docs/CUSTOMER_GUIDE.md) | Plain-language guide for subscribers |
| [API](docs/API.md) | REST API reference (OpenAPI at `/docs` in non-production) |
| [COMPLIANCE](docs/COMPLIANCE.md) | Legal/regulatory items to review with counsel **before launch** |

## Status & known limitations (read before production)

* **Kalshi API verification.** `docs.kalshi.com` and Kalshi's API hosts were blocked from the build environment.
  The integration was implemented against Kalshi's **official OpenAPI-generated SDK (kalshi_python 3.31.0)** and
  Kalshi's official starter code, and is covered by mocked HTTP tests — but it has **not yet been exercised
  against the live demo exchange**. Run the demo checklist in [KALSHI_INTEGRATION](docs/KALSHI_INTEGRATION.md)
  (especially V2 `bid`/`ask` order direction) before enabling `LIVE_TRADING`.
* **Series tickers** in `FEATURED_SERIES` are defaults to verify on kalshi.com.
* **Licensed data** (spot gold, COMEX GC futures, ICE DXY) is an explicit adapter that reports *disabled* until
  you plug in a licensed vendor; FRED's trade-weighted dollar is used as a DXY proxy.
* **Settlement index.** Crypto markets settle on Kalshi's specified reference index; Coinbase candles are a proxy.
* **Fees** in paper/backtests are estimates of Kalshi's published formula; live trading uses actual fills.
* **Docker images** were validated with `docker compose config` but not built here (no Docker daemon in the
  build sandbox); CI builds both images.
* **Stripe** webhook handling is fully tested; Checkout/Portal session creation needs real test-mode keys.
* **Legal**: disclosure texts are templates. See [COMPLIANCE](docs/COMPLIANCE.md).
