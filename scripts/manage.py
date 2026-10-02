"""Operations CLI.

    python scripts/manage.py gen-secrets
    python scripts/manage.py grant-admin <telegram_id>
    python scripts/manage.py revoke-admin <telegram_id>
    python scripts/manage.py seed-plans                  # from STRIPE_PRICE_* env + --amounts
    python scripts/manage.py create-codes pro 30 --count 5 --max-uses 1
    python scripts/manage.py rotate-encryption           # re-encrypt secrets under the newest key
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys

from cryptography.fernet import Fernet
from sqlalchemy import select

from kalshi_ai.config import get_settings
from kalshi_ai.db.models import KalshiConnection, SubscriptionPlan, User
from kalshi_ai.db.session import init_engine, session_scope
from kalshi_ai.domain.enums import BillingInterval, Plan, UserRole
from kalshi_ai.security.crypto import SecretCipher
from kalshi_ai.services.access_codes import generate_codes
from kalshi_ai.services.audit import audit
from kalshi_ai.services.users import get_or_create_telegram_user, get_user_by_telegram_id


def gen_secrets() -> None:
    print("# Paste into .env - do not commit")
    print(f"JWT_SECRET={secrets.token_urlsafe(48)}")
    print(f"ENCRYPTION_KEYS={Fernet.generate_key().decode()}")
    print(f"CODE_HASH_PEPPER={secrets.token_urlsafe(32)}")
    print(f"INTERNAL_API_TOKEN={secrets.token_urlsafe(32)}")
    print(f"TELEGRAM_WEBHOOK_SECRET={secrets.token_urlsafe(32)}")


async def set_admin(telegram_id: int, grant: bool) -> None:
    settings = get_settings()
    if grant and telegram_id not in settings.admin_telegram_ids:
        print(f"WARNING: {telegram_id} is not in ADMIN_TELEGRAM_IDS; the role alone does not grant admin access.")
    async with session_scope() as session:
        user, _ = await get_or_create_telegram_user(session, telegram_id)
        user.role = UserRole.ADMIN if grant else UserRole.CUSTOMER
        await audit(session, "admin.role." + ("granted" if grant else "revoked"), actor_type="system",
                    target_type="user", target_id=user.id, details={"telegram_id": telegram_id, "via": "cli"})
    print(("Granted" if grant else "Revoked") + f" admin role for Telegram user {telegram_id}")


async def seed_plans(amounts: dict[str, int]) -> None:
    settings = get_settings()
    async with session_scope() as session:
        for plan in Plan:
            for interval in (BillingInterval.MONTHLY, BillingInterval.YEARLY):
                price_id = settings.stripe_price_id(plan.value, interval.value)
                if not price_id:
                    continue
                row = (await session.execute(select(SubscriptionPlan).where(
                    SubscriptionPlan.plan == plan.value, SubscriptionPlan.interval == interval.value))).scalar_one_or_none()
                if row is None:
                    row = SubscriptionPlan(plan=plan, interval=interval)
                    session.add(row)
                row.stripe_price_id = price_id
                row.amount_cents = amounts.get(f"{plan.value}_{interval.value}", row.amount_cents or 0)
                print(f"seeded {plan.value}/{interval.value} -> {price_id} ({row.amount_cents} cents)")


async def create_codes(plan: str, days: int, count: int, max_uses: int, admin_telegram_id: int | None) -> None:
    async with session_scope() as session:
        admin = await get_user_by_telegram_id(session, admin_telegram_id) if admin_telegram_id else None
        if admin is None:
            admin = (await session.execute(select(User).where(User.role == UserRole.ADMIN.value))).scalars().first()
        if admin is None:
            sys.exit("No admin user exists. Run grant-admin first.")
        for g in await generate_codes(session, plan=Plan(plan), duration_days=days, count=count, max_uses=max_uses,
                                      admin=admin, note="cli"):
            print(g.code)
    print("# Codes are shown once and stored only as hashes.", file=sys.stderr)


async def rotate_encryption() -> None:
    cipher = SecretCipher.from_settings()
    async with session_scope() as session:
        rows = (await session.execute(select(KalshiConnection))).scalars().all()
        for c in rows:
            c.encrypted_api_key_id = cipher.rotate(c.encrypted_api_key_id)
            c.encrypted_private_key = cipher.rotate(c.encrypted_private_key)
        await audit(session, "security.encryption_rotated", actor_type="system", details={"count": len(rows)})
    print(f"Re-encrypted {len(rows)} connection(s) under the primary key. Old keys can now be removed.")


def main() -> None:
    p = argparse.ArgumentParser(description="Kalshi AI operations")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gen-secrets")
    for name in ("grant-admin", "revoke-admin"):
        sp = sub.add_parser(name)
        sp.add_argument("telegram_id", type=int)
    sp = sub.add_parser("seed-plans")
    sp.add_argument("--amounts", default="", help="e.g. pro_monthly=2900,pro_yearly=29000")
    sp = sub.add_parser("create-codes")
    sp.add_argument("plan", choices=[x.value for x in Plan])
    sp.add_argument("days", type=int)
    sp.add_argument("--count", type=int, default=1)
    sp.add_argument("--max-uses", type=int, default=1)
    sp.add_argument("--admin-telegram-id", type=int)
    sub.add_parser("rotate-encryption")
    args = p.parse_args()

    if args.cmd == "gen-secrets":
        gen_secrets()
        return
    init_engine()
    if args.cmd in ("grant-admin", "revoke-admin"):
        asyncio.run(set_admin(args.telegram_id, args.cmd == "grant-admin"))
    elif args.cmd == "seed-plans":
        amounts = {k: int(v) for k, v in (kv.split("=") for kv in args.amounts.split(",") if kv)}
        asyncio.run(seed_plans(amounts))
    elif args.cmd == "create-codes":
        asyncio.run(create_codes(args.plan, args.days, args.count, args.max_uses, args.admin_telegram_id))
    elif args.cmd == "rotate-encryption":
        asyncio.run(rotate_encryption())


if __name__ == "__main__":
    main()
