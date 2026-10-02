"""Order-book and trade-flow analysis for a Kalshi binary market."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from kalshi_ai.kalshi.models import KalshiOrderBook, KalshiTrade

ZERO = Decimal(0)


@dataclass(frozen=True)
class OrderBookSignal:
    best_yes_bid: Decimal | None
    best_yes_ask: Decimal | None
    mid: Decimal | None
    spread: Decimal | None
    yes_depth: Decimal  # total YES bid size within `band` of best
    no_depth: Decimal
    imbalance: float  # (+1 = all YES-side demand, -1 = all NO-side demand)
    microprice: Decimal | None
    trade_flow: float  # signed taker flow, -1..1 (positive = takers buying YES)
    trade_count: int
    recent_volume: Decimal
    price_change: Decimal | None  # last trade vs first trade in window
    abnormal_activity: bool
    liquidity_ok: bool
    signal: float  # combined -1..1 tilt toward YES

    def as_features(self) -> dict[str, float | None]:
        def f(x: Decimal | None) -> float | None:
            return float(x) if x is not None else None

        return {
            "ob_mid": f(self.mid),
            "ob_spread": f(self.spread),
            "ob_yes_depth": float(self.yes_depth),
            "ob_no_depth": float(self.no_depth),
            "ob_imbalance": self.imbalance,
            "ob_microprice": f(self.microprice),
            "ob_trade_flow": self.trade_flow,
            "ob_trade_count": float(self.trade_count),
            "ob_recent_volume": float(self.recent_volume),
            "ob_price_change": f(self.price_change),
            "ob_abnormal": 1.0 if self.abnormal_activity else 0.0,
            "ob_signal": self.signal,
        }


def analyze(
    book: KalshiOrderBook,
    trades: list[KalshiTrade],
    now: datetime,
    *,
    band: Decimal = Decimal("0.05"),
    trade_window: timedelta = timedelta(minutes=10),
    max_spread: Decimal = Decimal("0.10"),
    min_depth: Decimal = Decimal("10"),
    baseline_volume: Decimal | None = None,
) -> OrderBookSignal:
    bid, ask = book.best_yes_bid, book.best_yes_ask
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    spread = book.spread
    yes_depth = sum((lvl.size for lvl in book.yes_bids if bid is not None and lvl.price >= bid - band), ZERO)
    no_best = book.best_no_bid
    no_depth = sum((lvl.size for lvl in book.no_bids if no_best is not None and lvl.price >= no_best - band), ZERO)
    total = yes_depth + no_depth
    imbalance = float((yes_depth - no_depth) / total) if total > 0 else 0.0

    microprice = None
    if bid is not None and ask is not None and book.yes_bids and book.no_bids:
        bid_sz, ask_sz = book.yes_bids[0].size, book.no_bids[0].size
        if bid_sz + ask_sz > 0:
            microprice = (bid * ask_sz + ask * bid_sz) / (bid_sz + ask_sz)

    window = [t for t in trades if now - trade_window <= t.created_time <= now]
    window.sort(key=lambda t: t.created_time)
    buy_yes = sum((t.count for t in window if (t.taker_outcome_side or "").lower() == "yes"), ZERO)
    buy_no = sum((t.count for t in window if (t.taker_outcome_side or "").lower() == "no"), ZERO)
    vol = buy_yes + buy_no
    flow = float((buy_yes - buy_no) / vol) if vol > 0 else 0.0
    price_change = (window[-1].yes_price - window[0].yes_price) if len(window) >= 2 else None
    abnormal = bool(baseline_volume and baseline_volume > 0 and vol > baseline_volume * 5)

    liquidity_ok = spread is not None and spread <= max_spread and total >= min_depth
    micro_tilt = float((microprice - mid) / spread) if microprice is not None and mid is not None and spread else 0.0
    signal = max(-1.0, min(1.0, 0.5 * imbalance + 0.3 * flow + 0.2 * micro_tilt))
    if not liquidity_ok:
        signal *= 0.25  # thin books produce unreliable signals
    return OrderBookSignal(
        bid,
        ask,
        mid,
        spread,
        yes_depth,
        no_depth,
        imbalance,
        microprice,
        flow,
        len(window),
        vol,
        price_change,
        abnormal,
        liquidity_ok,
        signal,
    )
