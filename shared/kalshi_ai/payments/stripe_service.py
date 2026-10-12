"""Stripe integration (stripe-python v16, API version pinned by the SDK).

Rules:
* Access is granted ONLY from signature-verified webhook events, never from a
  frontend redirect or "I paid" request.
* Every event id is recorded in ``stripe_events`` so retries are idempotent.
* Since Stripe API 2025-03-31 ("basil"), ``current_period_end`` lives on
  subscription items and invoices reference subscriptions via
  ``invoice.parent.subscription_details.subscription``. Both new and legacy
  shapes are handled.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import stripe
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.config import Settings, get_settings
from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import StripeCustomer, StripeEvent, Subscription, SubscriptionPlan, User
from kalshi_ai.domain.enums import (
    BillingInterval,
    PaymentStatus,
    Plan,
    SubscriptionSource,
    SubscriptionStatus,
)
from kalshi_ai.logging import get_logger
from kalshi_ai.services.audit import audit
from kalshi_ai.services.subscriptions import enforce_entitlements

log = get_logger(__name__)

STATUS_MAP: dict[str, SubscriptionStatus] = {
    "trialing": SubscriptionStatus.TRIALING,
    "active": SubscriptionStatus.ACTIVE,
    "past_due": SubscriptionStatus.PAST_DUE,
    "unpaid": SubscriptionStatus.PAST_DUE,
    "incomplete": SubscriptionStatus.INCOMPLETE,
    "incomplete_expired": SubscriptionStatus.EXPIRED,
    "canceled": SubscriptionStatus.CANCELED,
    "paused": SubscriptionStatus.CANCELED,
}


class StripeNotConfiguredError(Exception):
    pass


class WebhookVerificationError(Exception):
    pass


def _client(settings: Settings) -> stripe.StripeClient:
    key = settings.stripe_secret_key.get_secret_value()
    if not key:
        raise StripeNotConfiguredError("STRIPE_SECRET_KEY is not configured.")
    return stripe.StripeClient(key)


def verify_webhook(payload: bytes, signature_header: str | None, settings: Settings | None = None) -> dict[str, Any]:
    """Verify the Stripe-Signature header (HMAC-SHA256, 5 min tolerance) and return the event dict."""
    settings = settings or get_settings()
    secret = settings.stripe_webhook_secret.get_secret_value()
    if not secret:
        raise StripeNotConfiguredError("STRIPE_WEBHOOK_SECRET is not configured.")
    if not signature_header:
        raise WebhookVerificationError("Missing Stripe-Signature header")
    try:
        stripe.WebhookSignature.verify_header(payload.decode("utf-8"), signature_header, secret, 300)
    except (stripe.SignatureVerificationError, ValueError, UnicodeDecodeError) as exc:
        raise WebhookVerificationError("Invalid Stripe signature") from exc
    try:
        event = json.loads(payload)
    except ValueError as exc:
        raise WebhookVerificationError("Invalid JSON payload") from exc
    if not isinstance(event, dict) or "id" not in event or "type" not in event:
        raise WebhookVerificationError("Malformed event")
    return event


async def resolve_price(
    session: AsyncSession, price_id: str, settings: Settings
) -> tuple[Plan, BillingInterval] | None:
    row = (
        await session.execute(select(SubscriptionPlan).where(SubscriptionPlan.stripe_price_id == price_id))
    ).scalar_one_or_none()
    if row is not None:
        return row.plan, row.interval
    for plan in Plan:
        for interval in (BillingInterval.MONTHLY, BillingInterval.YEARLY):
            if price_id and settings.stripe_price_id(plan.value, interval.value) == price_id:
                return plan, interval
    return None


async def get_or_create_stripe_customer(session: AsyncSession, user: User, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    row = (await session.execute(select(StripeCustomer).where(StripeCustomer.user_id == user.id))).scalar_one_or_none()
    if row is not None:
        return row.stripe_customer_id
    client = _client(settings)
    customer = await client.v1.customers.create_async(
        {"metadata": {"user_id": str(user.id)}, **({"email": user.email} if user.email else {})},
        {"idempotency_key": f"customer-{user.id}"},
    )
    session.add(StripeCustomer(user_id=user.id, stripe_customer_id=customer.id))
    await session.flush()
    return customer.id


async def create_checkout_session(
    session: AsyncSession, user: User, plan: Plan, interval: BillingInterval, settings: Settings | None = None
) -> str:
    settings = settings or get_settings()
    price_id = settings.stripe_price_id(plan.value, interval.value)
    if not price_id:
        db_plan = (
            await session.execute(
                select(SubscriptionPlan).where(
                    SubscriptionPlan.plan == plan.value,
                    SubscriptionPlan.interval == interval.value,
                    SubscriptionPlan.active.is_(True),
                )
            )
        ).scalar_one_or_none()
        price_id = db_plan.stripe_price_id if db_plan and db_plan.stripe_price_id else ""
    if not price_id:
        raise StripeNotConfiguredError(f"No Stripe price configured for {plan.value}/{interval.value}.")
    customer_id = await get_or_create_stripe_customer(session, user, settings)
    meta = {"user_id": str(user.id), "plan": plan.value, "interval": interval.value}
    sub_data: dict[str, Any] = {"metadata": meta}
    if settings.stripe_trial_days > 0:
        sub_data["trial_period_days"] = settings.stripe_trial_days
    client = _client(settings)
    checkout = await client.v1.checkout.sessions.create_async(
        {
            "mode": "subscription",
            "customer": customer_id,
            "client_reference_id": str(user.id),
            "line_items": [{"price": price_id, "quantity": 1}],
            "subscription_data": sub_data,
            "metadata": meta,
            "success_url": f"{settings.frontend_base_url}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            "cancel_url": f"{settings.frontend_base_url}/billing/cancel",
            "allow_promotion_codes": True,
        }
    )
    await audit(
        session,
        "billing.checkout.created",
        actor_user_id=user.id,
        details={"plan": plan.value, "interval": interval.value},
    )
    return str(checkout.url)


async def create_portal_session(session: AsyncSession, user: User, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    customer_id = await get_or_create_stripe_customer(session, user, settings)
    portal = await _client(settings).v1.billing_portal.sessions.create_async(
        {"customer": customer_id, "return_url": f"{settings.frontend_base_url}/billing"}
    )
    return str(portal.url)


# --------------------------------------------------------------------------- webhooks


def _ts(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value), UTC) if value else None


def _period_end(sub_obj: dict[str, Any]) -> datetime | None:
    items = ((sub_obj.get("items") or {}).get("data")) or []
    ends = [i.get("current_period_end") for i in items if i.get("current_period_end")]
    if ends:
        return _ts(max(ends))
    return _ts(sub_obj.get("current_period_end"))  # legacy API versions


def _first_price_id(sub_obj: dict[str, Any]) -> str | None:
    items = ((sub_obj.get("items") or {}).get("data")) or []
    for item in items:
        price = item.get("price") or {}
        if isinstance(price, dict) and price.get("id"):
            return str(price["id"])
        if isinstance(price, str):
            return price
    return None


def _invoice_subscription_id(invoice: dict[str, Any]) -> str | None:
    parent = invoice.get("parent") or {}
    details = parent.get("subscription_details") or {}
    sub = details.get("subscription") or invoice.get("subscription")
    if isinstance(sub, dict):
        return sub.get("id")
    return sub


async def _user_for_customer(session: AsyncSession, customer_id: str | None, metadata: dict[str, Any]) -> User | None:
    user_id = (metadata or {}).get("user_id")
    if user_id:
        try:
            user = await session.get(User, uuid.UUID(str(user_id)))
        except ValueError:
            user = None
        if user is not None:
            return user
    if customer_id:
        row = (
            await session.execute(select(StripeCustomer).where(StripeCustomer.stripe_customer_id == customer_id))
        ).scalar_one_or_none()
        if row is not None:
            return await session.get(User, row.user_id)
    return None


async def _upsert_subscription(session: AsyncSession, obj: dict[str, Any], settings: Settings) -> Subscription | None:
    stripe_sub_id = obj.get("id")
    customer_id = obj.get("customer") if isinstance(obj.get("customer"), str) else (obj.get("customer") or {}).get("id")
    user = await _user_for_customer(session, customer_id, obj.get("metadata") or {})
    if user is None or not stripe_sub_id:
        log.warning("stripe_subscription_unmatched", stripe_subscription_id=stripe_sub_id)
        return None
    price_id = _first_price_id(obj)
    resolved = await resolve_price(session, price_id, settings) if price_id else None
    if resolved is None:
        log.error("stripe_unknown_price", price_id=price_id)
        return None
    plan, interval = resolved
    status = STATUS_MAP.get(str(obj.get("status")), SubscriptionStatus.INCOMPLETE)
    expires = _period_end(obj) or utcnow()
    sub = (
        await session.execute(select(Subscription).where(Subscription.stripe_subscription_id == stripe_sub_id))
    ).scalar_one_or_none()
    if sub is None:
        sub = Subscription(
            user_id=user.id,
            plan=plan,
            interval=interval,
            status=status,
            source=SubscriptionSource.STRIPE,
            started_at=_ts(obj.get("start_date")) or utcnow(),
            expires_at=expires,
            stripe_customer_id=customer_id,
            stripe_subscription_id=stripe_sub_id,
        )
        session.add(sub)
    if sub.status == SubscriptionStatus.REFUNDED:
        return sub  # a refunded subscription never regains access from a late event
    sub.plan, sub.interval, sub.status, sub.expires_at = plan, interval, status, expires
    sub.cancel_at_period_end = bool(obj.get("cancel_at_period_end"))
    sub.canceled_at = _ts(obj.get("canceled_at"))
    if status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING) and sub.payment_status == PaymentStatus.NONE:
        sub.payment_status = PaymentStatus.PAID if status == SubscriptionStatus.ACTIVE else PaymentStatus.PENDING
    if status == SubscriptionStatus.CANCELED and (obj.get("ended_at") or obj.get("status") == "canceled"):
        sub.expires_at = min(sub.expires_at, _ts(obj.get("ended_at")) or utcnow())
    if customer_id:
        existing = (
            await session.execute(select(StripeCustomer).where(StripeCustomer.stripe_customer_id == customer_id))
        ).scalar_one_or_none()
        if existing is None:
            session.add(StripeCustomer(user_id=user.id, stripe_customer_id=customer_id))
    await session.flush()
    return sub


async def _find_sub(session: AsyncSession, stripe_sub_id: str | None) -> Subscription | None:
    if not stripe_sub_id:
        return None
    return (
        await session.execute(select(Subscription).where(Subscription.stripe_subscription_id == stripe_sub_id))
    ).scalar_one_or_none()


async def process_event(session: AsyncSession, event: dict[str, Any], settings: Settings | None = None) -> str:
    """Apply a VERIFIED Stripe event. Returns 'processed', 'duplicate' or 'ignored'."""
    settings = settings or get_settings()
    event_id, event_type = str(event["id"]), str(event["type"])
    try:
        async with session.begin_nested():
            session.add(StripeEvent(id=event_id, type=event_type))
            await session.flush()
    except IntegrityError:
        return "duplicate"

    obj: dict[str, Any] = (event.get("data") or {}).get("object") or {}
    handled = True
    if event_type == "checkout.session.completed":
        customer_id = obj.get("customer")
        user = await _user_for_customer(session, customer_id, {"user_id": obj.get("client_reference_id")})
        if user is not None and customer_id:
            existing = (
                await session.execute(select(StripeCustomer).where(StripeCustomer.stripe_customer_id == customer_id))
            ).scalar_one_or_none()
            if existing is None:
                session.add(StripeCustomer(user_id=user.id, stripe_customer_id=customer_id))
            await audit(
                session, "billing.checkout.completed", actor_type="system", target_type="user", target_id=user.id
            )
    elif event_type in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "customer.subscription.resumed",
        "customer.subscription.paused",
    ):
        sub = await _upsert_subscription(session, obj, settings)
        if sub is not None:
            await session.flush()
            await enforce_entitlements(session, sub.user_id)
            await audit(
                session,
                f"billing.{event_type}",
                actor_type="system",
                target_type="subscription",
                target_id=sub.id,
                details={"status": sub.status.value},
            )
    elif event_type in ("invoice.paid", "invoice.payment_succeeded"):
        sub = await _find_sub(session, _invoice_subscription_id(obj))
        if sub is not None and sub.status != SubscriptionStatus.REFUNDED:
            sub.payment_status = PaymentStatus.PAID
            line_ends = [(ln.get("period") or {}).get("end") for ln in ((obj.get("lines") or {}).get("data") or [])]
            new_end = _ts(max([e for e in line_ends if e], default=0))
            if new_end and new_end > sub.expires_at:
                sub.expires_at = new_end
            if sub.status in (SubscriptionStatus.PAST_DUE, SubscriptionStatus.INCOMPLETE):
                sub.status = SubscriptionStatus.ACTIVE
    elif event_type == "invoice.payment_failed":
        sub = await _find_sub(session, _invoice_subscription_id(obj))
        if sub is not None:
            sub.payment_status = PaymentStatus.FAILED
            if sub.status == SubscriptionStatus.ACTIVE:
                sub.status = SubscriptionStatus.PAST_DUE
            await audit(
                session, "billing.payment_failed", actor_type="system", target_type="subscription", target_id=sub.id
            )
    elif event_type == "charge.refunded":
        await _handle_refund(session, obj)
    else:
        handled = False
    return "processed" if handled else "ignored"


async def _handle_refund(session: AsyncSession, charge: dict[str, Any]) -> None:
    """Full refund -> access revoked immediately. Partial refund -> recorded only."""
    customer_id = charge.get("customer")
    if not customer_id:
        return
    sub = (
        await session.execute(
            select(Subscription)
            .where(
                Subscription.stripe_customer_id == customer_id, Subscription.source == SubscriptionSource.STRIPE.value
            )
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if sub is None:
        return
    full = bool(charge.get("refunded")) and int(charge.get("amount_refunded") or 0) >= int(charge.get("amount") or 0)
    sub.payment_status = PaymentStatus.REFUNDED
    if full:
        sub.status = SubscriptionStatus.REFUNDED
        sub.expires_at = utcnow()
    await session.flush()
    await enforce_entitlements(session, sub.user_id)
    await audit(
        session,
        "billing.refund",
        actor_type="system",
        target_type="subscription",
        target_id=sub.id,
        details={"full": full},
    )
