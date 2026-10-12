# Security

## Reporting a vulnerability

Email the operator's security contact (set one before launch) with details and reproduction steps. Do not open
public issues for vulnerabilities. We aim to acknowledge within 2 business days.

## Threat model (summary)

| Asset | Threats | Primary controls |
|---|---|---|
| Customer Kalshi API keys | DB leak, log leak, Telegram exposure, cross-customer use | Fernet encryption at rest (MultiFernet rotation), never logged/returned, web-form-only entry, Telegram guard deletes key-like messages, per-user client construction, scope policy rejecting fund-transfer keys, crypto-shred on disconnect |
| Customer funds | Erroneous/rogue orders | Paper default; live opt-in + fingerprint-bound confirmation; deterministic risk engine; hard global caps; final validation re-reading kill switches from DB; IOC limit orders; no blind retries; user/global kill switches; automatic monitor suspension |
| Subscriptions/revenue | Forged payments, code brute force | Stripe HMAC signature verification + 5-min replay tolerance; event-id idempotency; access never granted from client claims; codes HMAC-peppered, ≥79-bit entropy, per-user + global rate limits that **fail closed**, atomic use-counting |
| Admin powers | Privilege escalation | Admin = DB role **and** Telegram ID in `ADMIN_TELEGRAM_IDS`; JWT role claim ignored for authorization; every admin action audited; unauthorized bot commands look like unknown commands |
| Web sessions | XSS, CSRF, token theft | httpOnly + Secure + SameSite=Strict cookies, double-submit CSRF for cookie-auth writes, strict CSP, same-origin API proxy, short-lived JWTs |
| Availability | API abuse | Per-IP API rate limits (Redis), per-user bot throttling, request size limits on webhooks |

## Controls checklist

* **Encryption at rest** — `shared/kalshi_ai/security/crypto.py`. Rotate: prepend a new key to `ENCRYPTION_KEYS`,
  run `python scripts/manage.py rotate-encryption`, then remove the old key. Use disk/volume encryption for
  PostgreSQL and backups as well.
* **HTTPS** — Caddy terminates TLS with automatic certificates; HSTS in production; API trusts proxy headers only
  behind the proxy.
* **Secure cookies / CSRF** — `backend/kalshi_ai_api/deps.py`, `routers/auth.py`.
* **Rate limiting** — `services/rate_limit.py`, `middleware.py`.
* **Telegram authorization** — Telegram Login Widget HMAC verification (`security/telegram_auth.py`); webhook
  mode uses a secret token header; users are keyed by Telegram user ID.
* **RBAC** — `require_admin` on every `/api/admin/*` route; bot admin commands check `is_admin`.
* **Input validation** — Pydantic models on every endpoint; ticker/character allowlists; bounded sizes.
* **SQL injection** — SQLAlchemy ORM/Core with bound parameters only; no string-built SQL.
* **Secure headers** — API (`SecurityHeadersMiddleware`) and frontend (`next.config.ts`).
* **Secret management** — env vars only (`SecretStr`), production startup refuses dev defaults; use your
  platform's secret manager (Docker secrets, AWS SSM/Secrets Manager, GCP Secret Manager, Vault) to inject them.
* **Logging** — structured JSON; key-based and value-pattern redaction (PEM blocks, Stripe keys, Telegram tokens,
  bearer tokens); paths logged without query strings; audit-log details are redacted before storage.
* **Audit logs** — `audit_logs` table for logins, connects/disconnects, code events, subscription changes, trading
  switches, every order decision and every admin action.
* **Dependency scanning** — `pip-audit`, `npm audit`, Dependabot, gitleaks in CI.
* **No secrets in Git** — `.gitignore` covers `.env*`, `*.pem`, `*.key`; gitleaks scans history.

## LLM safety

The optional LLM receives only public market context, has **no tools**, returns schema-validated JSON, and can
move a probability by at most ±3 percentage points. It cannot create, modify or approve orders.

## Operational guidance

* Run with `APP_ENV=production` (enables guards, HSTS, hides OpenAPI docs).
* Restrict database and Redis to the private network; require Redis AUTH/TLS when not on a private network.
* Back up PostgreSQL with encryption; test restores; keep `ENCRYPTION_KEYS` in a separate secret store
  (a DB backup without the key is useless to an attacker — and to you, if you lose the key).
* Rotate `JWT_SECRET` to invalidate all web sessions; rotate the bot token via BotFather if exposed.
* Review `system_events` with severity `critical` daily.
