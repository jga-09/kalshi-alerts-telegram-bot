from __future__ import annotations

import json

import httpx
import respx
from sqlalchemy import select

from kalshi_ai.db.models import AuditLog, KalshiConnection
from kalshi_ai.domain.enums import KalshiConnectionStatus, Plan
from kalshi_ai.security.crypto import SecretCipher
from kalshi_ai.security.tokens import create_access_token
from kalshi_ai.services.kalshi_connections import build_user_auth, issue_connect_token
from tests.conftest import auth_headers, give_subscription, make_user
from tests.unit.test_kalshi_integration import rsa_pem
from tests.unit.test_stripe_webhooks import sign, sub_event

BASE = "https://kalshi.test/trade-api/v2"


async def test_health_and_security_headers(client) -> None:
    r = await client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert len(r.headers["X-Request-ID"]) >= 8


async def test_ready(client) -> None:
    r = await client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["checks"]["database"]["status"] == "ok" and body["checks"]["redis"]["status"] == "ok"
    assert body["details"]["global_kill_switch"] is False


async def test_auth_required_and_invalid_token(client) -> None:
    assert (await client.get("/api/me/profile")).status_code == 401
    r = await client.get("/api/me/profile", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401


async def test_profile_scoped_to_user(client, session) -> None:
    u1 = await make_user(session, 31)
    u2 = await make_user(session, 32)
    await give_subscription(session, u2, Plan.PREMIUM)
    await session.commit()
    r = await client.get("/api/me/profile", headers=auth_headers(u1))
    assert r.json()["id"] == str(u1.id) and r.json()["subscription"]["active"] is False


async def test_admin_requires_role_and_configured_telegram_id(client, session) -> None:
    customer = await make_user(session, 40)
    role_only = await make_user(session, 41, admin=True)  # admin role but NOT in ADMIN_TELEGRAM_IDS
    real_admin = await make_user(session, 999, admin=True)
    await session.commit()
    assert (await client.get("/api/admin/stats", headers=auth_headers(customer))).status_code == 403
    # A forged "admin" role claim in the JWT is ignored; the DB + config decide.
    forged = {"Authorization": f"Bearer {create_access_token(customer.id, 'admin')}"}
    assert (await client.get("/api/admin/stats", headers=forged)).status_code == 403
    assert (await client.get("/api/admin/stats", headers=auth_headers(role_only))).status_code == 403
    r = await client.get("/api/admin/stats", headers=auth_headers(real_admin))
    assert r.status_code == 200 and r.json()["total_users"] == 3


async def test_admin_codes_and_kill_switch(client, session) -> None:
    admin = await make_user(session, 999, admin=True)
    await session.commit()
    h = auth_headers(admin)
    r = await client.post("/api/admin/codes", headers=h, json={"plan": "pro", "duration_days": 90, "count": 2})
    assert r.status_code == 200
    codes = r.json()["codes"]
    assert len(codes) == 2 and codes[0]["code"].startswith("KAI-")
    listing = (await client.get("/api/admin/codes", headers=h)).json()
    assert all("code" not in c for c in listing)  # plaintext never listed again
    r = await client.post(f"/api/admin/codes/{codes[0]['id']}/revoke", headers=h)
    assert r.json()["status"] == "revoked"
    r = await client.post("/api/admin/kill-switch", headers=h, json={"active": True, "reason": "test drill"})
    assert r.json()["global_kill_switch"] is True
    assert (await client.get("/ready")).json()["details"]["global_kill_switch"] is True


async def test_csrf_enforced_for_cookie_sessions(client, session) -> None:
    user = await make_user(session, 50)
    await session.commit()
    token = create_access_token(user.id, "customer")
    client.cookies.set("kai_session", token)
    assert (await client.get("/api/me/settings")).status_code == 200  # safe method OK
    r = await client.put("/api/me/risk", json={"risk_mode": "passive"})
    assert r.status_code == 403
    client.cookies.set("kai_csrf", "abc123")
    r = await client.put("/api/me/risk", json={"risk_mode": "passive"}, headers={"X-CSRF-Token": "abc123"})
    assert r.status_code == 200
    client.cookies.clear()


@respx.mock(assert_all_called=False)
async def test_kalshi_connect_flow(client, session, respx_mock) -> None:
    user = await make_user(session, 60)
    await give_subscription(session, user, Plan.AUTO)
    token = await issue_connect_token(session, user)
    await session.commit()
    pem, _ = rsa_pem()

    info = await client.get("/api/kalshi/connect", params={"token": token})
    assert info.status_code == 200 and info.json()["valid"]

    respx_mock.get(f"{BASE}/portfolio/balance").mock(return_value=httpx.Response(200, json={"balance": 1000}))
    keys = respx_mock.get(f"{BASE}/api_keys").mock(
        return_value=httpx.Response(
            200,
            json={
                "api_keys": [
                    {"api_key_id": "kid-abcdef12", "name": "bot", "scopes": ["read", "write::transfer", "write::trade"]}
                ]
            },
        )
    )
    body = {"token": token, "api_key_id": "kid-abcdef12", "private_key_pem": pem, "environment": "demo"}
    r = await client.post("/api/kalshi/connect", json=body)
    assert r.status_code == 400 and "transfer" in r.json()["detail"]  # transfer-capable keys refused

    keys.mock(
        return_value=httpx.Response(
            200, json={"api_keys": [{"api_key_id": "kid-abcdef12", "name": "bot", "scopes": ["read", "write::trade"]}]}
        )
    )
    r = await client.post("/api/kalshi/connect", json=body)  # token not burned by the failed attempt
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "verified" and data["can_trade"] and data["api_key_id_hint"] == "***ef12"
    assert "private" not in json.dumps(data).lower()

    # Token is single-use.
    assert (await client.post("/api/kalshi/connect", json=body)).status_code == 400

    conn = (await session.execute(select(KalshiConnection))).scalar_one()
    assert conn.status == KalshiConnectionStatus.VERIFIED
    assert pem.encode() not in conn.encrypted_private_key and b"kid-abcdef12" not in conn.encrypted_api_key_id
    assert build_user_auth(conn, SecretCipher.from_settings()).api_key_id == "kid-abcdef12"
    audit_rows = (await session.execute(select(AuditLog))).scalars().all()
    assert all("BEGIN" not in json.dumps(a.details) for a in audit_rows)
    await session.commit()  # end the test's read transaction (SQLite shares one in-memory connection)

    status_r = await client.get("/api/kalshi/status", headers=auth_headers(user))
    assert status_r.json()["connected"] is True
    r = await client.post("/api/kalshi/disconnect", headers=auth_headers(user))
    assert r.json()["disconnected"] is True
    assert (await client.get("/api/kalshi/status", headers=auth_headers(user))).json() == {"connected": False}


@respx.mock(assert_all_called=False)
async def test_kalshi_connect_rejects_bad_credentials(client, session, respx_mock) -> None:
    user = await make_user(session, 61)
    await give_subscription(session, user, Plan.PRO)
    token = await issue_connect_token(session, user)
    await session.commit()
    pem, _ = rsa_pem()
    respx_mock.get(f"{BASE}/portfolio/balance").mock(return_value=httpx.Response(401, json={}))
    r = await client.post(
        "/api/kalshi/connect", json={"token": token, "api_key_id": "kid-abcdef12", "private_key_pem": pem}
    )
    assert r.status_code == 400 and "rejected" in r.json()["detail"]
    r = await client.post(
        "/api/kalshi/connect", json={"token": token, "api_key_id": "kid-abcdef12", "private_key_pem": "x" * 80}
    )
    assert r.status_code == 422


async def test_kalshi_connect_requires_plan(client, session) -> None:
    user = await make_user(session, 62)
    await give_subscription(session, user, Plan.SIGNALS)
    token = await issue_connect_token(session, user)
    await session.commit()
    pem, _ = rsa_pem()
    r = await client.post(
        "/api/kalshi/connect", json={"token": token, "api_key_id": "kid-abcdef12", "private_key_pem": pem}
    )
    assert r.status_code == 402


async def test_stripe_webhook_endpoint(client, session) -> None:
    user = await make_user(session, 70)
    await session.commit()
    payload = json.dumps(sub_event("evt_api_1", str(user.id))).encode()
    bad = await client.post("/api/stripe/webhook", content=payload, headers={"Stripe-Signature": "t=1,v1=bad"})
    assert bad.status_code == 400
    ok = await client.post("/api/stripe/webhook", content=payload, headers={"Stripe-Signature": sign(payload)})
    assert ok.status_code == 200 and ok.json()["status"] == "processed"
    me = await client.get("/api/auth/me", headers=auth_headers(user))
    assert me.json()["subscription"]["plan"] == "auto"
    dup = await client.post("/api/stripe/webhook", content=payload, headers={"Stripe-Signature": sign(payload)})
    assert dup.json()["status"] == "duplicate"


async def test_frontend_cannot_self_grant_subscription(client, session) -> None:
    """There is no endpoint that grants access from a client claim; checkout only returns a Stripe URL."""
    user = await make_user(session, 71)
    await session.commit()
    paths = set(client._transport.app.openapi()["paths"])  # type: ignore[attr-defined]
    assert not any("activate" in p and "admin" not in p for p in paths)
    me = await client.get("/api/auth/me", headers=auth_headers(user))
    assert me.json()["subscription"]["active"] is False


async def test_rate_limit(client, app) -> None:
    from kalshi_ai_api.middleware import RateLimitMiddleware

    for m in app.user_middleware:
        if m.cls is RateLimitMiddleware:
            m.kwargs["per_minute"] = 3
    app.middleware_stack = app.build_middleware_stack()
    codes = [(await client.get("/api/disclosures")).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and 429 in codes[3:]
