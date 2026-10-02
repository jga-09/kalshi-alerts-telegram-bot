# REST API

Base: `https://<domain>` (browser traffic goes through the Next.js same-origin proxy at `/api/*`).
Interactive OpenAPI docs: `/docs` and `/redoc` (disabled when `APP_ENV=production`).

**Auth**: `POST /api/auth/telegram` with a Telegram Login Widget payload → httpOnly `kai_session` cookie (JWT) and
a readable `kai_csrf` cookie. Cookie-authenticated `POST/PUT/PATCH/DELETE` requests must send
`X-CSRF-Token: <kai_csrf>`. `Authorization: Bearer <jwt>` is also accepted (no CSRF needed).
Errors: `{"detail": "..."}`; every response carries `X-Request-ID`.

## Public

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness |
| GET | `/ready` | DB + Redis readiness (503 if not ready); kill switch & source health |
| GET | `/api/billing/plans` | Plans, features, DB-configured prices |
| GET | `/api/disclosures` / `/api/disclosures/{terms\|privacy\|risk\|subscription}` | Legal pages |
| POST | `/api/stripe/webhook` | Stripe events (signature required) |
| GET | `/api/kalshi/connect?token=` | Validate a connect link; instructions |
| POST | `/api/kalshi/connect` | `{token, api_key_id, private_key_pem, environment}` → verify, encrypt, store |

## Customer (authenticated)

| Method | Path | Description |
|---|---|---|
| POST | `/api/auth/telegram` · `/api/auth/logout` | Login / logout |
| GET | `/api/auth/me` | Identity, role, subscription |
| GET | `/api/me/profile` | Profile summary |
| GET | `/api/me/settings` | Risk mode, overrides, effective limits, switches |
| PUT | `/api/me/risk` | `{risk_mode, overrides?}` (stricter only; resets live confirmation) |
| POST | `/api/me/auto-trading` | `{enabled}` |
| GET | `/api/me/live-trading/review` | Limits + fingerprint + blockers |
| POST | `/api/me/live-trading/confirm` | `{fingerprint, acknowledge_risk: true}` |
| POST | `/api/me/live-trading/disable` | Turn live off |
| POST | `/api/me/emergency-stop` | User kill switch + cancel open orders |
| GET | `/api/me/positions?mode=paper\|live` · `/api/me/orders` · `/api/me/paper-trades` | Portfolio (own data only) |
| POST | `/api/disclosures/accept` | Record risk-disclosure acceptance |
| POST | `/api/billing/checkout` | `{plan, interval}` → Stripe Checkout URL |
| POST | `/api/billing/portal` | Stripe Customer Portal URL |
| GET | `/api/billing/subscriptions` | Own subscription history |
| GET | `/api/kalshi/status` · POST `/api/kalshi/disconnect` | Connection status / delete credentials |

## Admin (role `admin` + Telegram ID in `ADMIN_TELEGRAM_IDS`)

| Method | Path | Description |
|---|---|---|
| GET | `/api/admin/stats` | KPIs, kill switch, monitor suspension, order errors |
| GET | `/api/admin/health/sources` | Data-source health |
| GET | `/api/admin/performance?days=` | Paper/live performance, by market/hour/risk mode |
| GET | `/api/admin/models?days=` | Model calibration vs market |
| GET | `/api/admin/trades/recent` | Recent orders |
| GET | `/api/admin/events` · `/api/admin/audit-logs` | System events / audit log |
| GET | `/api/admin/users` | Users |
| POST | `/api/admin/users/{id}/suspend` · `/reactivate` · `/extend` · `/auto-trading` | User actions |
| POST/GET | `/api/admin/codes` · POST `/api/admin/codes/{id}/revoke` | Access codes |
| POST | `/api/admin/kill-switch` | `{active, reason}` global stop |
| POST | `/api/admin/live-trading-suspension` | `{active, reason}` |
| PUT | `/api/admin/plans` | Configure plan prices |

Rate limit: `API_RATE_LIMIT_PER_MINUTE` per IP (429 with `Retry-After`).
