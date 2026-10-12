from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import timedelta

import pytest
from sqlalchemy import select

from kalshi_ai.config import get_settings
from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import RiskSettings, StripeCustomer, Subscription
from kalshi_ai.domain.entitlements import Feature
from kalshi_ai.domain.enums import PaymentStatus, Plan, SubscriptionStatus
from kalshi_ai.payments.stripe_service import WebhookVerificationError, process_event, verify_webhook
from kalshi_ai.services.subscriptions import get_access
from tests.conftest import make_user

SECRET = "whsec_test_secret"


def sign(payload: bytes, secret: str = SECRET, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def sub_event(
    event_id: str,
    user_id: str,
    status: str = "active",
    price: str = "price_auto_monthly",
    etype: str = "customer.subscription.created",
    period_days: int = 30,
    **extra,
) -> dict:
    end = int((utcnow() + timedelta(days=period_days)).timestamp())
    return {
        "id": event_id,
        "type": etype,
        "data": {
            "object": {
                "id": "sub_123",
                "object": "subscription",
                "customer": "cus_123",
                "status": status,
                "metadata": {"user_id": user_id},
                "start_date": int(time.time()),
                "cancel_at_period_end": False,
                "items": {"data": [{"price": {"id": price}, "current_period_end": end}]},
                **extra,
            }
        },
    }


def test_signature_verification() -> None:
    payload = json.dumps({"id": "evt_1", "type": "x"}).encode()
    assert verify_webhook(payload, sign(payload))["id"] == "evt_1"
    with pytest.raises(WebhookVerificationError):
        verify_webhook(payload, sign(payload, "whsec_wrong"))
    with pytest.raises(WebhookVerificationError):
        verify_webhook(payload + b" ", sign(payload))  # body tampered
    with pytest.raises(WebhookVerificationError):
        verify_webhook(payload, None)
    with pytest.raises(WebhookVerificationError):
        verify_webhook(payload, sign(payload, ts=int(time.time()) - 3600))  # replay outside tolerance


async def test_subscription_lifecycle(session) -> None:
    user = await make_user(session, 20)
    uid = str(user.id)
    assert await process_event(session, sub_event("evt_1", uid)) == "processed"
    access = await get_access(session, user)
    assert access.plan == Plan.AUTO and access.has(Feature.AUTO_TRADING)
    assert (await session.execute(select(StripeCustomer))).scalar_one().stripe_customer_id == "cus_123"

    # Idempotency: replaying the same event is a no-op.
    assert await process_event(session, sub_event("evt_1", uid)) == "duplicate"
    assert len((await session.execute(select(Subscription))).scalars().all()) == 1

    # Payment failure -> past_due (access kept until period end, payment flagged).
    invoice = {
        "id": "evt_2",
        "type": "invoice.payment_failed",
        "data": {
            "object": {
                "id": "in_1",
                "customer": "cus_123",
                "parent": {"subscription_details": {"subscription": "sub_123"}},
            }
        },
    }
    await process_event(session, invoice)
    sub = (await session.execute(select(Subscription))).scalar_one()
    assert sub.status == SubscriptionStatus.PAST_DUE and sub.payment_status == PaymentStatus.FAILED

    # Recovery via invoice.paid
    paid = {
        "id": "evt_3",
        "type": "invoice.paid",
        "data": {"object": {"id": "in_2", "customer": "cus_123", "subscription": "sub_123", "lines": {"data": []}}},
    }
    await process_event(session, paid)
    assert sub.status == SubscriptionStatus.ACTIVE and sub.payment_status == PaymentStatus.PAID

    # Cancellation at period end keeps access; deletion ends it.
    await process_event(
        session, sub_event("evt_4", uid, etype="customer.subscription.updated", cancel_at_period_end=True)
    )
    assert sub.cancel_at_period_end is True and (await get_access(session, user)).active
    rs = (await session.execute(select(RiskSettings).where(RiskSettings.user_id == user.id))).scalar_one()
    rs.auto_trading_enabled = rs.live_trading_enabled = True
    await session.flush()
    await process_event(
        session,
        sub_event(
            "evt_5", uid, status="canceled", etype="customer.subscription.deleted", ended_at=int(time.time()) - 1
        ),
    )
    assert not (await get_access(session, user)).active
    await session.refresh(rs)
    assert rs.auto_trading_enabled is False and rs.live_trading_enabled is False

    # Reactivation: a new active subscription restores access.
    reactivate = sub_event("evt_6", uid, etype="customer.subscription.updated")
    await process_event(session, reactivate)
    assert (await get_access(session, user)).active


async def test_full_refund_revokes_access(session) -> None:
    user = await make_user(session, 21)
    await process_event(session, sub_event("evt_10", str(user.id)))
    refund = {
        "id": "evt_11",
        "type": "charge.refunded",
        "data": {
            "object": {"id": "ch_1", "customer": "cus_123", "refunded": True, "amount": 2000, "amount_refunded": 2000}
        },
    }
    await process_event(session, refund)
    assert not (await get_access(session, user)).active
    # A late "active" event must not resurrect a refunded subscription.
    await process_event(session, sub_event("evt_12", str(user.id), etype="customer.subscription.updated"))
    assert not (await get_access(session, user)).active


async def test_unknown_price_and_unmatched_customer_grant_nothing(session) -> None:
    user = await make_user(session, 22)
    await process_event(session, sub_event("evt_20", str(user.id), price="price_unknown"))
    assert not (await get_access(session, user)).active
    ev = sub_event("evt_21", "00000000-0000-0000-0000-000000000000")
    ev["data"]["object"]["customer"] = "cus_nobody"
    await process_event(session, ev)
    assert (await session.execute(select(Subscription))).scalars().all() == []


async def test_trial_status(session) -> None:
    user = await make_user(session, 23)
    await process_event(session, sub_event("evt_30", str(user.id), status="trialing", period_days=7))
    access = await get_access(session, user)
    assert access.active and access.status == SubscriptionStatus.TRIALING
    assert get_settings().stripe_trial_days == 0
