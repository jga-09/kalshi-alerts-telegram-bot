"""Admin commands. Authorization = backend role ADMIN *and* Telegram ID in ADMIN_TELEGRAM_IDS.

Unauthorized callers get the same response as an unknown command (no information leak).
"""

from __future__ import annotations

from html import escape

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.db.models import RiskSettings, User
from kalshi_ai.domain.enums import Plan
from kalshi_ai.services.access_codes import generate_codes
from kalshi_ai.services.audit import audit
from kalshi_ai.services.jobs import enqueue_cancel_all_open_orders
from kalshi_ai.services.kill_switch import set_global_kill_switch
from kalshi_ai.services.users import is_admin
from kalshi_ai_bot.container import BotContainer
from kalshi_ai_bot.handlers.common import reply

router = Router(name="admin")
UNKNOWN = "Unknown command. Send /help."


async def _authorized(
    message: Message, session: AsyncSession, user: User, container: BotContainer, action: str
) -> bool:
    if is_admin(user, container.settings):
        return True
    await audit(session, "admin.command.denied", actor_user_id=user.id, details={"command": action})
    await reply(message, UNKNOWN)
    return False


@router.message(Command("admin"))
async def cmd_admin(message: Message, session: AsyncSession, user: User, container: BotContainer) -> None:
    if not await _authorized(message, session, user, container, "admin"):
        return
    users = (await session.execute(select(func.count(User.id)))).scalar_one()
    auto = (
        await session.execute(select(func.count(RiskSettings.id)).where(RiskSettings.auto_trading_enabled.is_(True)))
    ).scalar_one()
    live = (
        await session.execute(select(func.count(RiskSettings.id)).where(RiskSettings.live_trading_enabled.is_(True)))
    ).scalar_one()
    await reply(
        message,
        "\n".join(
            [
                "<b>Admin</b>",
                f"Users: {users} | auto traders: {auto} | live traders: {live}",
                "",
                "/gencode &lt;plan&gt; &lt;days&gt; [count] [max_uses]",
                "/killswitch on|off &lt;reason&gt;",
                "Full dashboard: web admin panel.",
            ]
        ),
    )


@router.message(Command("gencode"))
async def cmd_gencode(
    message: Message, command: CommandObject, session: AsyncSession, user: User, container: BotContainer
) -> None:
    if not await _authorized(message, session, user, container, "gencode"):
        return
    parts = (command.args or "").split()
    try:
        plan = Plan(parts[0].lower())
        days = int(parts[1])
        count = int(parts[2]) if len(parts) > 2 else 1
        max_uses = int(parts[3]) if len(parts) > 3 else 1
        codes = await generate_codes(
            session, plan=plan, duration_days=days, count=min(count, 20), max_uses=max_uses, admin=user, note="telegram"
        )
    except (IndexError, ValueError) as exc:
        await reply(
            message,
            f"Usage: /gencode &lt;signals|pro|auto|premium&gt; &lt;days&gt; [count] [max_uses]\n"
            f"{escape(str(exc)) if isinstance(exc, ValueError) else ''}",
        )
        return
    body = "\n".join(f"<code>{g.code}</code>" for g in codes)
    await reply(
        message,
        f"<b>{len(codes)} code(s) - {plan.value.upper()} {days} days</b>\n{body}\n\n"
        "Shown once; not stored in plaintext. Delete this message after copying.",
    )


@router.message(Command("killswitch"))
async def cmd_killswitch(
    message: Message, command: CommandObject, session: AsyncSession, user: User, container: BotContainer
) -> None:
    if not await _authorized(message, session, user, container, "killswitch"):
        return
    parts = (command.args or "").split(maxsplit=1)
    if not parts or parts[0] not in ("on", "off") or len(parts) < 2:
        await reply(message, "Usage: /killswitch on|off &lt;reason&gt;")
        return
    active = parts[0] == "on"
    await set_global_kill_switch(session, user, active, parts[1][:300])
    await session.commit()
    if active:
        enqueue_cancel_all_open_orders()
    await reply(message, f"Global kill switch {'ACTIVATED ⛔' if active else 'deactivated ✅'}.")
