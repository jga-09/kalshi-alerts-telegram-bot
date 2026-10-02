from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import ActivationCode, AuditLog, RiskSettings, Subscription
from kalshi_ai.domain.entitlements import Feature
from kalshi_ai.domain.enums import ActivationCodeStatus, Plan, SubscriptionSource, SubscriptionStatus, UserStatus
from kalshi_ai.services.access_codes import RedeemOutcome, generate_codes, redeem_code, revoke_code
from kalshi_ai.services.rate_limit import RateLimiter
from kalshi_ai.services.subscriptions import expire_due_subscriptions, get_access, grant_time
from tests.conftest import give_subscription, make_user


async def test_new_user_has_no_access_and_safe_defaults(session) -> None:
    user = await make_user(session, 5)
    access = await get_access(session, user)
    assert not access.active and not access.has(Feature.SIGNALS)
    rs = (await session.execute(select(RiskSettings).where(RiskSettings.user_id == user.id))).scalar_one()
    assert rs.auto_trading_enabled is False and rs.live_trading_enabled is False


async def test_access_picks_highest_plan_and_respects_expiry(session) -> None:
    user = await make_user(session, 6)
    await give_subscription(session, user, Plan.SIGNALS, days=60)
    await give_subscription(session, user, Plan.PREMIUM, days=10)
    access = await get_access(session, user)
    assert access.plan == Plan.PREMIUM and access.has(Feature.LIVE_TRADING)
    assert (await get_access(session, user, now=utcnow() + timedelta(days=20))).plan == Plan.SIGNALS
    assert not (await get_access(session, user, now=utcnow() + timedelta(days=61))).active


async def test_suspended_user_has_no_access(session) -> None:
    user = await make_user(session, 7)
    await give_subscription(session, user)
    user.status = UserStatus.SUSPENDED
    assert not (await get_access(session, user)).active


async def test_expiration_job_revokes_auto_trading(session) -> None:
    user = await make_user(session, 8)
    sub = await give_subscription(session, user, Plan.AUTO)
    rs = (await session.execute(select(RiskSettings).where(RiskSettings.user_id == user.id))).scalar_one()
    rs.auto_trading_enabled = rs.live_trading_enabled = True
    sub.expires_at = utcnow() - timedelta(seconds=1)
    await session.flush()
    assert await expire_due_subscriptions(session) == 1
    await session.refresh(rs)
    assert sub.status == SubscriptionStatus.EXPIRED
    assert rs.auto_trading_enabled is False and rs.live_trading_enabled is False


async def test_grant_time_extends_same_grant(session) -> None:
    user = await make_user(session, 9)
    first = await grant_time(session, user, Plan.PRO, 30, source=SubscriptionSource.ACCESS_CODE)
    second = await grant_time(session, user, Plan.PRO, 7, source=SubscriptionSource.ACCESS_CODE)
    assert first.id == second.id
    assert (second.expires_at - second.started_at).days in (36, 37)


async def test_code_generation_stores_only_hash(session) -> None:
    admin = await make_user(session, 999, admin=True)
    generated = await generate_codes(session, plan=Plan.PRO, duration_days=30, count=3, admin=admin)
    rows = (await session.execute(select(ActivationCode))).scalars().all()
    assert len(rows) == 3
    for g in generated:
        assert g.record.code_hash != g.code and g.code not in g.record.code_hash
        assert len(g.record.code_hint) == 8
    logs = (await session.execute(select(AuditLog).where(AuditLog.action == "admin.codes.generated"))).scalars().all()
    assert logs and all(g.code not in str(logs[0].details) for g in generated)


async def test_redeem_happy_path_and_reuse_prevention(session, redis) -> None:
    admin = await make_user(session, 999, admin=True)
    user = await make_user(session, 10)
    other = await make_user(session, 11)
    third = await make_user(session, 12)
    [g] = await generate_codes(session, plan=Plan.AUTO, duration_days=7, max_uses=2, admin=admin)
    limiter = RateLimiter(redis)

    result = await redeem_code(session, user, g.code.lower(), limiter)
    assert result.outcome == RedeemOutcome.ACTIVATED
    assert (await get_access(session, user)).plan == Plan.AUTO
    assert (await redeem_code(session, user, g.code, limiter)).outcome == RedeemOutcome.ALREADY_USED
    assert (await redeem_code(session, other, g.code, limiter)).outcome == RedeemOutcome.ACTIVATED
    await session.refresh(g.record)
    assert g.record.current_uses == 2 and g.record.status == ActivationCodeStatus.EXHAUSTED
    assert (await redeem_code(session, third, g.code, limiter)).outcome == RedeemOutcome.INVALID
    assert not (await get_access(session, third)).active


async def test_redeem_rejects_expired_revoked_and_unknown(session, redis) -> None:
    admin = await make_user(session, 999, admin=True)
    user = await make_user(session, 13)
    limiter = RateLimiter(redis)
    [expired] = await generate_codes(
        session, plan=Plan.PRO, duration_days=30, admin=admin, code_expires_at=utcnow() + timedelta(seconds=30)
    )
    expired.record.expires_at = utcnow() - timedelta(seconds=1)
    [revoked] = await generate_codes(session, plan=Plan.PRO, duration_days=30, admin=admin)
    await revoke_code(session, revoked.record.id, admin=admin)
    await session.flush()
    for code in (expired.code, revoked.code, "KAI-AAAA-BBBB-CCCC-DDDD", "x"):
        assert (await redeem_code(session, user, code, limiter)).outcome == RedeemOutcome.INVALID
    assert not (await get_access(session, user)).active
    subs = (await session.execute(select(Subscription).where(Subscription.user_id == user.id))).scalars().all()
    assert subs == []


async def test_brute_force_rate_limited(session, redis) -> None:
    admin = await make_user(session, 999, admin=True)
    user = await make_user(session, 14)
    [good] = await generate_codes(session, plan=Plan.PRO, duration_days=30, admin=admin)
    limiter = RateLimiter(redis)
    for i in range(5):
        assert (
            await redeem_code(session, user, f"KAI-WRNG-CODE-AAAA-{i:04d}", limiter)
        ).outcome == RedeemOutcome.INVALID
    # Even the correct code is refused once the attempt budget is spent.
    result = await redeem_code(session, user, good.code, limiter)
    assert result.outcome == RedeemOutcome.RATE_LIMITED and result.retry_after_seconds > 0


async def test_redemption_fails_closed_when_redis_down(session) -> None:
    from redis.asyncio import Redis

    admin = await make_user(session, 999, admin=True)
    user = await make_user(session, 15)
    [good] = await generate_codes(session, plan=Plan.PRO, duration_days=30, admin=admin)
    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    result = await redeem_code(session, user, good.code, RateLimiter(dead))
    await dead.aclose()
    assert result.outcome == RedeemOutcome.UNAVAILABLE
    assert not (await get_access(session, user)).active
