"""Backtesting framework.

Replays historical market states (prices, order books, trades), candles, news and
social data through the SAME FeatureEngine -> PredictionEngine -> StrategyEngine ->
RiskEngine -> fill-simulation path used live, and reports results per strategy.

Resting (Drift) orders fill only on later frames whose asks reach the limit, before expiry.

Look-ahead protection:
* Each step builds a MarketContext with ``as_of = frame time``; FeatureEngine
  filters every input to ``ts <= as_of`` (and only completed candles).
* Order books newer than the step are rejected.
* Market results are only read at settlement, after the last frame.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from kalshi_ai.analytics.metrics import TradeRecord, brier_score, calibration_bins, log_loss, summarize, summarize_by
from kalshi_ai.config import Settings, get_settings
from kalshi_ai.data.base import Candle, DataPoint
from kalshi_ai.domain.enums import EntryStyle, RiskMode
from kalshi_ai.features.engine import FeatureEngine, MarketContext
from kalshi_ai.kalshi.models import KalshiMarket, KalshiOrderBook, KalshiTrade
from kalshi_ai.modeling.prediction import PredictionEngine
from kalshi_ai.news.engine import Article
from kalshi_ai.risk.engine import AccountState, LiquidityView, RiskEngine, SignalView
from kalshi_ai.risk.profiles import effective_limits
from kalshi_ai.signals.engine import SignalDecision
from kalshi_ai.social.engine import Post
from kalshi_ai.strategies.config import PRESETS, StrategyConfig
from kalshi_ai.strategies.engine import StrategyEngine
from kalshi_ai.trading.fees import estimate_maker_fee
from kalshi_ai.trading.paper import simulate_ioc_fill

ZERO = Decimal(0)
ONE = Decimal(1)


class LookAheadError(Exception):
    pass


@dataclass(frozen=True)
class HistoricalFrame:
    as_of: datetime
    market: KalshiMarket
    orderbook: KalshiOrderBook


@dataclass
class HistoricalDataset:
    frames: dict[str, list[HistoricalFrame]]
    results: dict[str, bool]  # ticker -> settled YES?
    candles: dict[str, list[Candle]] = field(default_factory=dict)
    trades: dict[str, list[KalshiTrade]] = field(default_factory=dict)
    points: list[DataPoint] = field(default_factory=list)
    articles: list[Article] = field(default_factory=list)
    posts: list[Post] = field(default_factory=list)
    calendar: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SimTrade:
    ticker: str
    as_of: datetime
    side: str
    quantity: int
    price: Decimal
    fees: Decimal
    estimated_probability: Decimal
    edge: Decimal
    pnl: Decimal | None = None
    won: bool | None = None
    strategy: str = "model"


@dataclass
class _Resting:
    ticker: str
    signal: SignalDecision
    remaining: int
    expires_at: datetime
    counted_fill: bool = False  # already counted as "filled" for the fill-rate statistic


@dataclass
class BacktestReport:
    trades: list[SimTrade]
    summary: dict[str, Any]
    prediction_calibration: dict[str, Any]
    equity_curve: list[tuple[str, float]]
    blocked_by_risk: dict[str, int]
    starting_balance: Decimal
    ending_balance: Decimal
    by_strategy: dict[str, Any] = field(default_factory=dict)
    fill_rate_by_strategy: dict[str, float | None] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "starting_balance": str(self.starting_balance),
            "ending_balance": str(self.ending_balance),
            "simulated_pnl": str(self.ending_balance - self.starting_balance),
            "trade_count": len(self.trades),
            **self.summary,
            "expected_value_per_trade": self.summary.get("average_trade_pnl"),
            "prediction_calibration": self.prediction_calibration,
            "blocked_by_risk": self.blocked_by_risk,
            "by_strategy": self.by_strategy,
            "fill_rate_by_strategy": self.fill_rate_by_strategy,
            "equity_curve": self.equity_curve,
            "disclaimer": "Hypothetical simulated results. Past performance does not guarantee future results.",
        }


class _SimAccount:
    def __init__(self, balance: Decimal):
        self.balance = balance
        self.positions: dict[str, Decimal] = defaultdict(lambda: ZERO)  # ticker -> contracts
        self.exposure: dict[str, Decimal] = defaultdict(lambda: ZERO)  # ticker -> cost
        self.realized_by_day: dict[date, Decimal] = defaultdict(lambda: ZERO)
        self.trades_by_day: dict[date, int] = defaultdict(int)

    def state(self, ticker: str, group: str, day: date) -> AccountState:
        return AccountState(
            available_balance=self.balance,
            realized_pnl_today=self.realized_by_day[day],
            unrealized_pnl_today=ZERO,
            trades_today=self.trades_by_day[day],
            position_contracts_in_market=self.positions[ticker],
            exposure_in_market=self.exposure[ticker],
            exposure_in_group=sum((v for t, v in self.exposure.items() if t.split("-", 1)[0] == group), ZERO),
        )


class BacktestEngine:
    def __init__(
        self,
        dataset: HistoricalDataset,
        *,
        risk_mode: RiskMode = RiskMode.PASSIVE,
        starting_balance: Decimal = Decimal("1000"),
        prediction_engine: PredictionEngine | None = None,
        settings: Settings | None = None,
        strategies: StrategyConfig | None = None,
    ):
        self.data = dataset
        self.settings = settings or get_settings()
        self.risk_mode = risk_mode
        self.starting_balance = starting_balance
        self.features = FeatureEngine()
        self.predictor = prediction_engine or PredictionEngine()
        self.strategy_engine = StrategyEngine(self.settings)
        self.strategies = strategies or StrategyConfig(enabled=frozenset(PRESETS[risk_mode]))
        self.risk = RiskEngine(
            effective_limits(risk_mode, None, self.settings),
            risk_mode,
            min_price=self.settings.hard_min_price,
            max_price=self.settings.hard_max_price,
            max_data_age_seconds=self.settings.max_data_age_seconds,
        )

    async def run(self) -> BacktestReport:
        acct = _SimAccount(self.starting_balance)
        resting: list[_Resting] = []
        attempts: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # strategy -> [filled, orders]
        trades: list[SimTrade] = []
        blocked: dict[str, int] = defaultdict(int)
        pred_probs: list[float] = []
        pred_outcomes: list[int] = []
        equity: list[tuple[str, float]] = []

        # Interleave all markets chronologically so cross-market limits are realistic.
        steps = sorted(((f.as_of, t, f) for t, fs in self.data.frames.items() for f in fs), key=lambda x: x[0])
        for as_of, ticker, frame in steps:
            if frame.orderbook.fetched_at > as_of:
                raise LookAheadError(f"order book for {ticker} is from the future")
            ctx = MarketContext(
                as_of=as_of,
                market=frame.market,
                orderbook=frame.orderbook,
                trades=self.data.trades.get(ticker, []),
                candles=self.data.candles,
                points=self.data.points,
                articles=self.data.articles,
                posts=self.data.posts,
                calendar=self.data.calendar,
                max_data_age_seconds=self.settings.max_data_age_seconds,
            )
            fv = self.features.build(ctx)  # applies point_in_time()
            pred = await self.predictor.predict(fv)
            if pred is None:
                continue
            if ticker in self.data.results:
                pred_probs.append(pred.probability_yes)
                pred_outcomes.append(1 if self.data.results[ticker] else 0)
            self._fill_resting(resting, ticker, frame, as_of, acct, trades, attempts)
            result = self.strategy_engine.evaluate(pred)
            fired = {x.strategy: x for x in result.signals if x.strategy}
            for strategy in self.strategies.select(set(fired)):
                self._enter(fired[strategy], ticker, frame, as_of, acct, trades, blocked, resting, attempts)
            equity.append((as_of.isoformat(), float(acct.balance)))
        for r in resting:
            if r.remaining > 0:
                blocked["limit_expired_unfilled"] += 1

        # Settlement - the only place results are read.
        for t in trades:
            if t.ticker not in self.data.results:
                continue
            won = (t.side == "yes") == self.data.results[t.ticker]
            payout = ONE * t.quantity if won else ZERO
            t.pnl = payout - t.price * t.quantity - t.fees
            t.won = won
            acct.balance += payout
            acct.realized_by_day[t.as_of.date()] += t.pnl
        equity.append(("final", float(acct.balance)))

        records = [
            TradeRecord(
                pnl=t.pnl or ZERO,
                cost=t.price * t.quantity + t.fees,
                estimated_probability=t.estimated_probability,
                entry_price=t.price,
                won=t.won,
                tags={"market": t.ticker.split("-", 1)[0], "strategy": t.strategy},
            )
            for t in trades
        ]
        return BacktestReport(
            trades=trades,
            summary=summarize(records),
            prediction_calibration={
                "predictions": len(pred_probs),
                "brier": brier_score(pred_probs, pred_outcomes),
                "log_loss": log_loss(pred_probs, pred_outcomes),
                "bins": [b.__dict__ for b in calibration_bins(pred_probs, pred_outcomes)],
            },
            equity_curve=equity,
            blocked_by_risk=dict(blocked),
            starting_balance=self.starting_balance,
            ending_balance=acct.balance,
            by_strategy=summarize_by(records, "strategy"),
            fill_rate_by_strategy={k: (f / n if n else None) for k, (f, n) in sorted(attempts.items())},
        )

    def _enter(
        self,
        sig: SignalDecision,
        ticker: str,
        frame: HistoricalFrame,
        as_of: datetime,
        acct: _SimAccount,
        trades: list[SimTrade],
        blocked: dict[str, int],
        resting: list[_Resting],
        attempts: dict[str, list[int]],
    ) -> None:
        assert sig.strategy is not None
        book = frame.orderbook
        ask = book.ask_for(sig.side)
        if ask is None:
            return
        maker = sig.entry_style == EntryStyle.MAKE
        price = sig.entry_price if maker else ask
        if price > self.strategies.max_price:
            blocked["max_price_cap"] += 1
            return
        view = SignalView(
            ticker,
            sig.correlation_group,
            sig.side,
            sig.estimated_probability,
            price,
            price,
            sig.estimated_probability - price,
            sig.confidence,
            sig.expires_at,
            sig.data_age_seconds,
        )
        depth = book.top_ask_size(sig.side) if maker else book.ask_depth(sig.side, ask)
        liq = LiquidityView(book.spread, depth, book.top_ask_size(sig.side))
        risk = self.risk.evaluate(
            view,
            acct.state(ticker, sig.correlation_group, as_of.date()),
            liq,
            as_of,
            size_factor=self.strategies.size_factor(sig.strategy),
        )
        if not risk.approved:
            for c in risk.failed:
                blocked[c.name] += 1
            return
        attempts[sig.strategy.value][1] += 1
        fill = simulate_ioc_fill(book, sig.side, risk.quantity, price)
        if fill.filled and fill.avg_price is not None:
            self._book_trade(sig, ticker, as_of, fill.filled, fill.avg_price, fill.fees, acct, trades)
            attempts[sig.strategy.value][0] += 1
        remaining = risk.quantity - fill.filled
        if maker and remaining > 0:
            resting.append(_Resting(ticker, sig, remaining, sig.expires_at, counted_fill=bool(fill.filled)))
        elif not fill.filled:
            blocked["no_fill"] += 1

    def _fill_resting(
        self,
        resting: list[_Resting],
        ticker: str,
        frame: HistoricalFrame,
        as_of: datetime,
        acct: _SimAccount,
        trades: list[SimTrade],
        attempts: dict[str, list[int]],
    ) -> None:
        for r in resting:
            if r.ticker != ticker or r.remaining <= 0 or as_of > r.expires_at:
                continue
            limit = r.signal.entry_price
            fill = simulate_ioc_fill(frame.orderbook, r.signal.side, r.remaining, limit)
            if fill.filled:
                # A maker is paid its own limit price, with the maker fee estimate.
                fee = estimate_maker_fee(fill.filled, limit)
                self._book_trade(r.signal, ticker, as_of, fill.filled, limit, fee, acct, trades)
                r.remaining -= fill.filled
                if not r.counted_fill and r.signal.strategy is not None:
                    attempts[r.signal.strategy.value][0] += 1
                    r.counted_fill = True

    @staticmethod
    def _book_trade(
        sig: SignalDecision,
        ticker: str,
        as_of: datetime,
        qty: int,
        price: Decimal,
        fees: Decimal,
        acct: _SimAccount,
        trades: list[SimTrade],
    ) -> None:
        cost = price * qty + fees
        acct.balance -= cost
        acct.positions[ticker] += qty
        acct.exposure[ticker] += cost
        acct.trades_by_day[as_of.date()] += 1
        trades.append(
            SimTrade(
                ticker,
                as_of,
                sig.side.value,
                qty,
                price,
                fees,
                sig.estimated_probability,
                sig.estimated_probability - price,
                strategy=sig.strategy.value if sig.strategy else "model",
            )
        )
