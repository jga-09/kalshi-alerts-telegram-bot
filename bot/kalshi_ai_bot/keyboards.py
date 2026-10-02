from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from kalshi_ai.domain.enums import Plan, RiskMode


class Menu(CallbackData, prefix="m"):
    action: str


class PlanPick(CallbackData, prefix="plan"):
    plan: str
    interval: str


class RiskPick(CallbackData, prefix="risk"):
    mode: str


class LiveConfirm(CallbackData, prefix="live"):
    action: str  # confirm | cancel
    fp: str  # first 16 hex chars of the reviewed risk fingerprint


class SignalPick(CallbackData, prefix="sig"):
    ticker: str


def main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for text, action in [
        ("📊 Signals", "signals"),
        ("🏛 Markets", "markets"),
        ("💰 Balance", "balance"),
        ("📈 Positions", "positions"),
        ("🤖 Auto Trade", "autotrade"),
        ("📝 Paper Trading", "paper"),
        ("⚖️ Risk Settings", "risk"),
        ("🔗 Connect Kalshi", "connect"),
        ("💳 Subscription", "subscription"),
        ("🛑 EMERGENCY STOP", "stop"),
    ]:
        kb.button(text=text, callback_data=Menu(action=action))
    kb.adjust(2, 2, 2, 2, 1, 1)
    return kb.as_markup()


def inactive_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 BUY SUBSCRIPTION", callback_data=Menu(action="subscribe"))
    kb.button(text="🔑 ENTER ACCESS CODE", callback_data=Menu(action="code"))
    kb.adjust(1)
    return kb.as_markup()


def plans_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for plan in Plan:
        kb.button(text=f"{plan.value.upper()} monthly", callback_data=PlanPick(plan=plan.value, interval="monthly"))
        kb.button(text=f"{plan.value.upper()} yearly", callback_data=PlanPick(plan=plan.value, interval="yearly"))
    kb.adjust(2)
    return kb.as_markup()


def risk_menu(current: RiskMode) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for mode in RiskMode:
        mark = "✅ " if mode == current else ""
        kb.button(text=f"{mark}{mode.value.upper()}", callback_data=RiskPick(mode=mode.value))
    kb.adjust(3)
    return kb.as_markup()


def live_confirm_menu(fingerprint: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ CONFIRM & ENABLE", callback_data=LiveConfirm(action="confirm", fp=fingerprint[:16]))
    kb.button(text="❌ CANCEL", callback_data=LiveConfirm(action="cancel", fp=fingerprint[:16]))
    kb.adjust(2)
    return kb.as_markup()


def autotrade_menu(auto_on: bool, live_on: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="⏸ Disable auto trading" if auto_on else "▶️ Enable auto trading (paper)",
        callback_data=Menu(action="auto_off" if auto_on else "auto_on"),
    )
    if auto_on:
        kb.button(
            text="⏹ Disable LIVE trading" if live_on else "⚡ Review LIVE trading",
            callback_data=Menu(action="live_off" if live_on else "live_review"),
        )
    kb.adjust(1)
    return kb.as_markup()


def disclosure_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="I have read and accept the risk disclosure", callback_data=Menu(action="accept_disclosures"))
    return kb.as_markup()


def signals_menu(tickers: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for ticker, label in tickers[:10]:
        kb.button(text=label[:60], callback_data=SignalPick(ticker=ticker[:50]))
    kb.adjust(1)
    return kb.as_markup()
