# Kalshi integration

## How the current API was verified

`docs.kalshi.com` and the Kalshi API hosts were not reachable from the build environment, so the implementation
was checked against primary sources that were reachable:

* **Kalshi's official OpenAPI-generated Python SDK** `kalshi_python_async` **3.31.0** (PyPI, published from
  `github.com/Kalshi/exchange-infra`): `auth.py`, `configuration.py`, `api/*.py`, `models/*.py`.
* **Kalshi's official starter code** `github.com/Kalshi/kalshi-starter-code-python` (`clients.py`).

Findings that shaped the design:

| Topic | Current behaviour (SDK 3.31.0) | Where implemented |
|---|---|---|
| Third-party OAuth | **Not offered.** Access is via per-account API keys. | `/connect` flow below |
| Key types | RSA (RSA-PSS, MGF1-SHA256, salt = digest length, SHA-256) **and Ed25519** | `kalshi/auth.py` |
| Signed message | `timestamp_ms + METHOD + path`, path includes `/trade-api/v2`, **query string excluded** | `KalshiAuthService.headers` |
| Headers | `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE` (base64) | same |
| Hosts | prod `https://external-api.kalshi.com/trade-api/v2` (also `api.elections.kalshi.com`); demo `https://external-api.demo.kalshi.co/trade-api/v2` (also `demo-api.kalshi.co`) | `config.KALSHI_HOSTS` |
| API key scopes | `read`, `write`, `read::portfolio_balance`, `write::trade`, `write::transfer`, … | scope policy below |
| Prices | fixed-point dollar strings (`yes_bid_dollars: "0.5600"`); counts `*_fp: "10.00"` | `kalshi/models.py` (Decimal, never float) |
| Order book | `orderbook_fp.yes_dollars` / `no_dollars` = **bids only**; a NO bid at q ≡ YES ask at 1−q | `KalshiOrderBook` |
| Create order | **V2** `POST /portfolio/events/orders` `{ticker, client_order_id, side: bid\|ask, count, price, time_in_force, self_trade_prevention_type}`. Legacy `/portfolio/orders` deprecated (not earlier than 2026-05-06). | `build_v2_order_payload` |
| Cancel order | V2 `DELETE /portfolio/events/orders/{order_id}` | `KalshiOrderService.cancel_order` |
| Reads | `/portfolio/balance`, `/portfolio/positions`, `/portfolio/orders`, `/portfolio/fills`, `/api_keys`, `/markets`, `/markets/{t}`, `/markets/{t}/orderbook`, `/markets/trades`, `/exchange/status` | services |

## Abstraction layer

| Class | Responsibility |
|---|---|
| `KalshiAuthService` | Loads ONE customer's key in memory, signs requests; `repr` never shows key material |
| `KalshiClient` | HTTP transport, throttling, error mapping; **GETs retried, order POST/DELETE never retried** |
| `KalshiMarketService` | Public market data: markets, market, order book, trades, candlesticks, exchange status |
| `KalshiPortfolioService` | Customer balance, positions, orders, fills, API-key scopes |
| `KalshiOrderService` | V2 create/cancel/get, reconciliation by `client_order_id` |
| `KalshiBroker` | Per-customer adapter used by the live engine, built only from that customer's encrypted credential |

## Order direction (V2) — VERIFY ON DEMO

Our internal intent is always "BUY `side` at ≤ `limit_price`". Translation (`build_v2_order_payload`):

* BUY YES at p → `side: "bid"`, `price: p`
* BUY NO at q → `side: "ask"`, `price: 1 − q` (selling YES on the YES-quoted single book ≡ buying NO)

This follows the SDK's documented `book_side`/`outcome_side` semantics ("buy-no and sell-yes produce 'no'") but
**must be confirmed with a 1-contract order on the demo exchange** before enabling live trading.

## Customer connection flow

1. `/connect` in Telegram → single-use token (15 min, stored as SHA-256) → HTTPS link to `/connect` web page.
   **Credentials never pass through Telegram**; a guard deletes any message that looks like a key.
2. Customer creates an API key on kalshi.com with **`read` + `write::trade` only**.
3. `POST /api/kalshi/connect` → key parsed (RSA ≥ 2048 or Ed25519) → live verification: `GET /portfolio/balance`
   (proves auth) and `GET /api_keys` (reads the key's scopes).
4. Keys with `write` or `write::transfer` (fund-moving) are **rejected** unless `KALSHI_ALLOW_TRANSFER_SCOPE=true`.
5. Key ID and PEM are encrypted with Fernet (MultiFernet for rotation) into `kalshi_connections`; only the last
   4 characters of the key ID and the public-key fingerprint are stored in clear.
6. Live trading additionally requires a trade scope; read-only keys allow balance/positions only.
7. Disconnect deletes the encrypted material (crypto-shredding) and disables live trading.

Endpoints: `GET /api/kalshi/connect?token=` (validate link + instructions), `POST /api/kalshi/connect`,
`POST /api/kalshi/disconnect`, `GET /api/kalshi/status`. There is **no** `/api/kalshi/callback` because Kalshi
has no OAuth flow.

## Isolation guarantees

* Credentials are decrypted per operation into a client for exactly one user; no shared client holds customer keys.
* All portfolio/order queries in our DB are filtered by `user_id` (and `mode`).
* `client_order_id` is a UUIDv5 of `(user_id, signal_id, mode)`.
* Emergency cancellation only touches orders Kalshi AI placed (matched by our stored `kalshi_order_id`).

## Demo checklist before `LIVE_TRADING=true`

1. Connect a demo key via `/connect`; confirm `status: verified` and scopes shown.
2. `/balance` shows the demo balance.
3. Place a 1-contract BUY YES and BUY NO (low price, IOC) via a test signal; confirm on the demo UI that the
   resulting positions are YES and NO respectively.
4. Force a timeout (firewall the host mid-request) and confirm the order is reconciled, not duplicated.
5. `/stop` cancels a resting order placed by Kalshi AI and leaves manually placed orders alone.
6. Revoke the key on kalshi.com → next order fails with an auth error and nothing is retried.
