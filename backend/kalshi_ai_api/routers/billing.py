from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select

from kalshi_ai.db.models import SubscriptionPlan
from kalshi_ai.domain.entitlements import PLAN_DESCRIPTIONS, PLAN_FEATURES
from kalshi_ai.domain.enums import BillingInterval, Plan
from kalshi_ai.logging import get_logger
from kalshi_ai.payments.stripe_service import (
    StripeNotConfiguredError,
    WebhookVerificationError,
    create_checkout_session,
    create_portal_session,
    process_event,
    verify_webhook,
)
from kalshi_ai.services.subscriptions import list_subscriptions
from kalshi_ai_api.deps import CurrentUser, SessionDep, SettingsDep

log = get_logger(__name__)
router = APIRouter(tags=["billing"])


@router.get("/api/billing/plans")
async def plans(session: SessionDep) -> list[dict[str, Any]]:
    rows = (await session.execute(select(SubscriptionPlan).where(SubscriptionPlan.active.is_(True)))).scalars().all()
    prices: dict[tuple[str, str], SubscriptionPlan] = {(r.plan.value, r.interval.value): r for r in rows}
    out = []
    for plan in Plan:
        out.append(
            {
                "plan": plan.value,
                "description": PLAN_DESCRIPTIONS[plan],
                "features": sorted(f.value for f in PLAN_FEATURES[plan]),
                "prices": {
                    interval.value: (
                        {"amount_cents": p.amount_cents, "currency": p.currency}
                        if (p := prices.get((plan.value, interval.value)))
                        else None
                    )
                    for interval in (BillingInterval.MONTHLY, BillingInterval.YEARLY)
                },
            }
        )
    return out


class CheckoutRequest(BaseModel):
    plan: Plan
    interval: BillingInterval


@router.post("/api/billing/checkout")
async def checkout(
    body: CheckoutRequest, user: CurrentUser, session: SessionDep, settings: SettingsDep
) -> dict[str, str]:
    if body.interval == BillingInterval.CUSTOM:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose monthly or yearly")
    try:
        url = await create_checkout_session(session, user, body.plan, body.interval, settings)
    except StripeNotConfiguredError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
    return {"url": url}


@router.post("/api/billing/portal")
async def portal(user: CurrentUser, session: SessionDep, settings: SettingsDep) -> dict[str, str]:
    try:
        return {"url": await create_portal_session(session, user, settings)}
    except StripeNotConfiguredError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None


@router.get("/api/billing/subscriptions")
async def my_subscriptions(user: CurrentUser, session: SessionDep) -> list[dict[str, Any]]:
    return [
        {
            "id": str(s.id),
            "plan": s.plan.value,
            "interval": s.interval.value,
            "status": s.status.value,
            "source": s.source.value,
            "started_at": s.started_at.isoformat(),
            "expires_at": s.expires_at.isoformat(),
            "payment_status": s.payment_status.value,
            "cancel_at_period_end": s.cancel_at_period_end,
        }
        for s in await list_subscriptions(session, user.id)
    ]


@router.post("/api/stripe/webhook", include_in_schema=False)
async def stripe_webhook(request: Request, session: SessionDep, settings: SettingsDep) -> dict[str, str]:
    payload = await request.body()
    if len(payload) > 1_000_000:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Payload too large")
    try:
        event = verify_webhook(payload, request.headers.get("Stripe-Signature"), settings)
    except StripeNotConfiguredError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Stripe not configured") from None
    except WebhookVerificationError:
        log.warning("stripe_webhook_rejected")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid signature") from None
    result = await process_event(session, event, settings)
    log.info("stripe_webhook", event_type=event["type"], result=result)
    return {"status": result}
