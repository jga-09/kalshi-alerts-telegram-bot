# Admin guide

## Becoming an admin

Both conditions are required (knowing a command is never enough):

1. Your Telegram numeric ID is in `ADMIN_TELEGRAM_IDS`.
2. Your user has the backend role `admin`: `python scripts/manage.py grant-admin <telegram_id>`
   (revoke with `revoke-admin`). Every change is audited.

Log in at `https://<domain>/admin` with the Telegram Login Widget.

## Dashboard tabs

| Tab | What you see / do |
|---|---|
| Overview | Users, active/expired subscriptions, estimated MRR, auto/live/paper traders, open orders, kill-switch & monitor status, order errors (24h), data-source health, system events |
| Performance | Paper/live P&L, win rate, drawdown, model vs market Brier score, calibration bins, performance by market |
| Users | Suspend (requires reason; disables auto/live), reactivate, extend subscription, disable/allow auto trading |
| Codes | Generate 7/30/90/365/custom-day codes for any plan with max uses; **plaintext shown once**; revoke |
| Trades | Recent paper and live orders with status, reason and model version |
| Logs | Audit log with action-prefix filter (e.g. `order.`, `admin.`, `code.`) |
| Kill switch | Activate/deactivate the GLOBAL emergency stop (reason required) |

Customer secrets are never shown anywhere in the admin UI or API.

## Access codes

* Dashboard → Codes, or Telegram `/gencode <plan> <days> [count] [max_uses]`, or
  `python scripts/manage.py create-codes pro 30 --count 10`.
* Codes are stored only as HMAC hashes (with a server pepper) plus an 8-character hint. Lost codes cannot be
  recovered — generate new ones. Changing `CODE_HASH_PEPPER` invalidates all outstanding codes.
* Redeeming twice with the same account is blocked; `max_uses` is enforced atomically; expired/revoked/exhausted
  codes are indistinguishable from unknown codes to the user.

## Plans & prices

`PUT /api/admin/plans` with `{plan, interval, stripe_price_id, amount_cents, currency, active}` or
`python scripts/manage.py seed-plans --amounts pro_monthly=4900,...`.

## Global kill switch

Use when data looks wrong, a model misbehaves, Kalshi is unstable, or during incidents. Activation blocks every
new automated order immediately (validator re-reads the DB flag per order) and enqueues cancellation of all
Kalshi-AI-placed open orders. Deactivation does not re-enable anyone's live trading automatically.

## Monitoring suspensions

Critical alerts (model worse than market, majority of data sources down, live order error spike) automatically
set `live_trading_suspended`. Investigate in Overview → System events, then clear with
`POST /api/admin/live-trading-suspension {"active": false, "reason": "..."}`.

## Models

* A logistic baseline is retrained daily from resolved predictions and registered as **shadow**.
* Promote by updating `model_versions.status` to `active` only if the shadow model beats both the incumbent and
  the market on the holdout (`metrics` column; `should_promote()` encodes the rule).
* Retire a model by setting `status = retired`; the rule-based baseline is always available.

## Daily checklist

1. Overview: kill switch off (unless intended), no critical events, sources healthy.
2. Performance: model Brier ≤ market Brier; calibration bins near the diagonal.
3. Trades: no clusters of `failed`/`unknown` live orders.
4. Logs: unexpected `admin.command.denied` or `code.rate_limited` spikes.
