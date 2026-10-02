# Deployment

## Single host with Docker Compose + automatic HTTPS

Requirements: a Linux VM (2 vCPU / 4 GB RAM minimum), Docker 24+, a domain pointing to the VM, ports 80/443 open.

```bash
git clone <repo> /opt/kalshi-ai && cd /opt/kalshi-ai
cp .env.example .env && pip install cryptography && python3 scripts/gen_secrets.py >> .env
$EDITOR .env
#   APP_ENV=production
#   DOMAIN=app.example.com
#   PUBLIC_BASE_URL=https://app.example.com  FRONTEND_BASE_URL=https://app.example.com  CORS_ORIGINS=https://app.example.com
#   POSTGRES_PASSWORD=<strong>  TELEGRAM_BOT_TOKEN=...  ADMIN_TELEGRAM_IDS=...
#   STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET / STRIPE_PRICE_*
#   TELEGRAM_WEBHOOK_URL=https://app.example.com/telegram/webhook   (optional; polling otherwise)
#   KALSHI_ENV=demo   LIVE_TRADING=false   <- keep until the demo checklist passes
docker compose --profile https up -d --build
docker compose exec api python scripts/manage.py grant-admin <your_telegram_id>
docker compose exec api python scripts/manage.py seed-plans --amounts signals_monthly=1900,pro_monthly=4900,auto_monthly=9900,premium_monthly=19900
```

Then:

1. Stripe → Developers → Webhooks → add endpoint `https://app.example.com/api/stripe/webhook` with events:
   `checkout.session.completed`, `customer.subscription.created|updated|deleted|paused|resumed`,
   `invoice.paid`, `invoice.payment_succeeded`, `invoice.payment_failed`, `charge.refunded`.
2. BotFather → `/setdomain` → `app.example.com` (Telegram Login Widget for the admin/billing pages).
3. Verify: `curl https://app.example.com/health`, and readiness from inside the network:
   `docker compose exec api python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/ready').read())"`.

## Going live with real-money trading

1. Complete `docs/KALSHI_INTEGRATION.md` → demo checklist.
2. Review hard limits in `.env` (`HARD_*`), `MAX_DAILY_LOSS`, `MAX_POSITION_SIZE`.
3. Complete the legal review in `docs/COMPLIANCE.md`.
4. Set `KALSHI_ENV=prod` only for customers connecting production keys (the environment is chosen per connection;
   `KALSHI_ENV` sets the default and the public market-data host), then `LIVE_TRADING=true`, and restart.
5. Watch the admin dashboard and `system_events` closely for the first days; keep the global kill switch at hand.

## Scaling

* `api` is stateless — run several replicas behind Caddy/your load balancer.
* `worker` scales horizontally (`--concurrency`, more containers). Keep a dedicated worker on the `urgent` queue
  so emergency cancellations are never stuck behind analysis jobs:
  `celery -A kalshi_ai_worker.celery_app worker -Q urgent -c 2`.
* Run exactly **one** `beat` and one `bot` (polling) instance. In webhook mode the bot can be replicated.
* Managed PostgreSQL/Redis are recommended for production (automated backups, failover).

## Backups & restore

```bash
docker compose exec -T postgres pg_dump -U kalshi -Fc kalshi_ai > backup-$(date +%F).dump   # encrypt & ship offsite
docker compose exec -T postgres pg_restore -U kalshi -d kalshi_ai --clean < backup.dump
```

Store `ENCRYPTION_KEYS` separately from database backups.

## Upgrades & migrations

```bash
git pull && docker compose build && docker compose up -d   # `migrate` runs `alembic upgrade head` first
```

Create new migrations with `alembic -c backend/alembic.ini revision --autogenerate -m "..."` and review them.
CI fails if models and migrations drift (`alembic check`).

## Observability

* Logs: JSON on stdout (`docker compose logs -f api worker bot`), with `request_id`, `customer_id`, `trade_id`,
  `signal_id`. Ship to your log platform (Loki, Datadog, CloudWatch...).
* Health: `GET /health` (liveness), `GET /ready` (DB + Redis; reports kill switch & source health).
* Error tracking: hook your provider into the structlog pipeline (`shared/kalshi_ai/logging.py`).
* Alerts: `system_events` with severity `critical`; monitor auto-suspensions.

## Incident runbook

| Situation | Action |
|---|---|
| Suspected bad model/data | Admin → Kill switch tab → ACTIVATE GLOBAL STOP (or `/killswitch on <reason>`) |
| Kalshi outage | Orders fail safely; no retries. Optionally activate global stop to avoid noise. |
| Redis down | API rate limits fail open; code activation fails closed; candles unavailable ⇒ lower confidence. |
| DB down | `/ready` returns 503; nothing can trade (all checks read the DB). |
| Key compromise | Rotate `ENCRYPTION_KEYS` (see SECURITY.md); ask customers to revoke & recreate Kalshi keys. |
| Monitor suspended live trading | Investigate `system_events`, then clear via `POST /api/admin/live-trading-suspension {active:false}`. |
