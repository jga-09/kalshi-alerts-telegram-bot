from __future__ import annotations

import base64
import json
from decimal import Decimal

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

from kalshi_ai.domain.enums import Side
from kalshi_ai.kalshi import (
    KalshiAuthService,
    KalshiClient,
    KalshiMarketService,
    KalshiOrderService,
    KalshiPortfolioService,
    OrderIntent,
    build_v2_order_payload,
)
from kalshi_ai.kalshi.errors import (
    KalshiAuthError,
    KalshiCredentialFormatError,
    KalshiTimeoutError,
    KalshiValidationError,
)

BASE = "https://kalshi.test/trade-api/v2"


def rsa_pem() -> tuple[str, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    ).decode()
    return pem, key


def ed_pem() -> tuple[str, ed25519.Ed25519PrivateKey]:
    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return pem, key


def test_rsa_signature_matches_official_scheme() -> None:
    pem, key = rsa_pem()
    auth = KalshiAuthService("key-id-123456", pem)
    headers = auth.headers("get", "/trade-api/v2/portfolio/balance?limit=5", timestamp_ms=1700000000000)
    assert headers["KALSHI-ACCESS-KEY"] == "key-id-123456"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    # Signed message excludes the query string and upper-cases the method.
    message = b"1700000000000GET/trade-api/v2/portfolio/balance"
    key.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    assert auth.key_type == "rsa"
    assert "BEGIN" not in repr(auth) and "key-id-123456" not in repr(auth)


def test_ed25519_signature() -> None:
    pem, key = ed_pem()
    auth = KalshiAuthService("abcd-efgh", pem)
    headers = auth.headers("POST", "/trade-api/v2/portfolio/events/orders", timestamp_ms=1)
    key.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]), b"1POST/trade-api/v2/portfolio/events/orders"
    )
    assert auth.key_type == "ed25519"


def test_rejects_bad_keys() -> None:
    with pytest.raises(KalshiCredentialFormatError):
        KalshiAuthService("id-12345678", "not a key")
    ec_key = ec.generate_private_key(ec.SECP256R1())
    ec_pem = ec_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    with pytest.raises(KalshiCredentialFormatError):
        KalshiAuthService("id-12345678", ec_pem)
    small = (
        rsa.generate_private_key(public_exponent=65537, key_size=1024)
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        .decode()
    )
    with pytest.raises(KalshiCredentialFormatError):
        KalshiAuthService("id-12345678", small)


def test_v2_order_payload_conversion() -> None:
    yes = build_v2_order_payload(OrderIntent("KXBTC-1", Side.YES, 10, Decimal("0.55"), "c1"))
    assert yes["side"] == "bid" and yes["price"] == "0.5500" and yes["count"] == "10"
    no = build_v2_order_payload(OrderIntent("KXBTC-1", Side.NO, 3, Decimal("0.40"), "c2"))
    # Buying NO at 0.40 == selling YES at 0.60 on the single YES-quoted book.
    assert no["side"] == "ask" and no["price"] == "0.6000"
    assert no["self_trade_prevention_type"] == "taker_at_cross"
    with pytest.raises(KalshiValidationError):
        build_v2_order_payload(OrderIntent("T", Side.YES, 0, Decimal("0.5"), "c"))
    with pytest.raises(KalshiValidationError):
        build_v2_order_payload(OrderIntent("T", Side.YES, 1, Decimal("1.00"), "c"))


@respx.mock
async def test_market_data_parsing_fixed_point() -> None:
    respx.get(f"{BASE}/markets/KXBTC15M-X/orderbook").mock(
        return_value=httpx.Response(
            200,
            json={
                "orderbook_fp": {
                    "yes_dollars": [["0.4800", "100.00"], ["0.5000", "40.00"]],
                    "no_dollars": [["0.4600", "30.00"], ["0.4700", "25.00"]],
                }
            },
        )
    )
    respx.get(f"{BASE}/markets/KXBTC15M-X").mock(
        return_value=httpx.Response(
            200,
            json={
                "market": {
                    "ticker": "KXBTC15M-X",
                    "event_ticker": "E",
                    "status": "active",
                    "yes_bid_dollars": "0.5000",
                    "yes_ask_dollars": "0.5300",
                    "no_bid_dollars": "0.4700",
                    "no_ask_dollars": "0.5000",
                    "last_price_dollars": "0.51",
                    "volume_fp": "1234.00",
                }
            },
        )
    )
    async with KalshiClient(BASE, min_interval_seconds=0) as client:
        svc = KalshiMarketService(client)
        book = await svc.get_orderbook("KXBTC15M-X")
        market = await svc.get_market("KXBTC15M-X")
    assert book.best_yes_bid == Decimal("0.50")
    assert book.best_yes_ask == Decimal("0.53")  # 1 - best NO bid 0.47
    assert book.best_no_ask == Decimal("0.50")
    assert book.spread == Decimal("0.03")
    assert book.ask_depth(Side.YES, Decimal("0.54")) == Decimal("55")  # NO bids at .47 and .46
    assert market.yes_ask == Decimal("0.53") and market.is_open and market.volume == Decimal("1234")


@respx.mock
async def test_authenticated_calls_are_signed_and_portfolio_parsed() -> None:
    pem, _ = rsa_pem()
    route = respx.get(f"{BASE}/portfolio/balance").mock(
        return_value=httpx.Response(
            200, json={"balance": 12345, "balance_dollars": "123.45", "portfolio_value": 500, "updated_ts": 1}
        )
    )
    async with KalshiClient(BASE, KalshiAuthService("kid-12345678", pem), min_interval_seconds=0) as client:
        bal = await KalshiPortfolioService(client).get_balance()
    assert bal.balance == Decimal("123.45")
    sent = route.calls.last.request.headers
    assert sent["KALSHI-ACCESS-KEY"] == "kid-12345678" and "KALSHI-ACCESS-SIGNATURE" in sent


async def test_portfolio_requires_auth() -> None:
    async with KalshiClient(BASE) as client:
        with pytest.raises(ValueError):
            KalshiPortfolioService(client)
        with pytest.raises(KalshiAuthError):
            await client.get("/portfolio/balance", auth_required=True)


@respx.mock
async def test_order_post_is_never_retried_and_timeout_is_unknown() -> None:
    pem, _ = rsa_pem()
    route = respx.post(f"{BASE}/portfolio/events/orders").mock(side_effect=httpx.ReadTimeout("t"))
    async with KalshiClient(
        BASE, KalshiAuthService("kid-12345678", pem), max_retries=3, min_interval_seconds=0
    ) as client:
        with pytest.raises(KalshiTimeoutError):
            await KalshiOrderService(client).create_order(OrderIntent("T-1", Side.YES, 1, Decimal("0.5"), "cid"))
    assert route.call_count == 1


@respx.mock
async def test_order_success_and_error_mapping() -> None:
    pem, _ = rsa_pem()
    route = respx.post(f"{BASE}/portfolio/events/orders").mock(
        return_value=httpx.Response(
            201,
            json={
                "order_id": "o1",
                "client_order_id": "cid",
                "fill_count": "2.00",
                "remaining_count": "0.00",
                "average_fill_price": "0.5000",
                "ts_ms": 1,
            },
        )
    )
    async with KalshiClient(BASE, KalshiAuthService("kid-12345678", pem), min_interval_seconds=0) as client:
        result = await KalshiOrderService(client).create_order(OrderIntent("T-1", Side.YES, 2, Decimal("0.5"), "cid"))
        assert result.order_id == "o1" and result.fill_count == Decimal("2")
        body = json.loads(route.calls.last.request.content)
        assert body == {
            "ticker": "T-1",
            "client_order_id": "cid",
            "side": "bid",
            "count": "2",
            "price": "0.5000",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "cancel_order_on_pause": True,
        }
        respx.post(f"{BASE}/portfolio/events/orders").mock(
            return_value=httpx.Response(400, json={"error": {"code": "insufficient_balance", "message": "no money"}})
        )
        with pytest.raises(KalshiValidationError) as err:
            await KalshiOrderService(client).create_order(OrderIntent("T-1", Side.YES, 2, Decimal("0.5"), "c2"))
        assert err.value.code == "insufficient_balance"
        respx.get(f"{BASE}/portfolio/balance").mock(return_value=httpx.Response(401, json={}))
        with pytest.raises(KalshiAuthError):
            await KalshiPortfolioService(client).get_balance()


@respx.mock
async def test_get_is_retried_on_5xx() -> None:
    route = respx.get(f"{BASE}/exchange/status").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"exchange_active": True, "trading_active": True})]
    )
    async with KalshiClient(BASE, min_interval_seconds=0) as client:
        status = await KalshiMarketService(client).exchange_status()
    assert status.trading_active and route.call_count == 2
