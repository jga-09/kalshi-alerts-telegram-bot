"""Run a backtest over data recorded by the live system.

    python scripts/backtest.py --start 2026-09-01 --end 2026-09-30 --series KXBTC15M --risk passive \
        --candles-from-coinbase BTC-USD --out report.json

Results are hypothetical. Fees/slippage are estimated; queue position and partial liquidity
beyond the recorded book snapshots are not modelled.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from kalshi_ai.backtest.engine import BacktestEngine
from kalshi_ai.backtest.loader import load_dataset
from kalshi_ai.data.base import Candle
from kalshi_ai.db.session import init_engine, session_scope
from kalshi_ai.domain.enums import RiskMode
from kalshi_ai.modeling.prediction import PredictionEngine
from kalshi_ai.modeling.registry import ModelRegistry


async def coinbase_candles(product: str, start: datetime, end: datetime) -> list[Candle]:
    """Historical 1-minute candles from Coinbase's public API (300 per request)."""
    out: list[Candle] = []
    cursor = start
    async with httpx.AsyncClient(timeout=15) as http:
        while cursor < end:
            chunk_end = min(end, cursor + timedelta(minutes=300))
            r = await http.get(f"https://api.exchange.coinbase.com/products/{product}/candles",
                               params={"granularity": 60, "start": cursor.isoformat(), "end": chunk_end.isoformat()})
            r.raise_for_status()
            out += [Candle(datetime.fromtimestamp(x[0], UTC), x[3], x[2], x[1], x[4], x[5]) for x in r.json()]
            cursor = chunk_end
            await asyncio.sleep(0.35)  # stay well within public rate limits
    return sorted({c.ts: c for c in out}.values(), key=lambda c: c.ts)


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--series")
    p.add_argument("--risk", default="passive", choices=[m.value for m in RiskMode])
    p.add_argument("--balance", default="1000")
    p.add_argument("--candles-from-coinbase", default="")
    p.add_argument("--out", default="")
    a = p.parse_args()
    start = datetime.fromisoformat(a.start).replace(tzinfo=UTC)
    end = datetime.fromisoformat(a.end).replace(tzinfo=UTC)
    init_engine()
    async with session_scope() as session:
        dataset = await load_dataset(session, start, end, a.series)
        registry = await ModelRegistry.load(session)
    if a.candles_from_coinbase:
        dataset.candles[a.candles_from_coinbase] = await coinbase_candles(a.candles_from_coinbase,
                                                                          start - timedelta(hours=3), end)
    report = await BacktestEngine(dataset, risk_mode=RiskMode(a.risk), starting_balance=Decimal(a.balance),
                                  prediction_engine=PredictionEngine(registry)).run()
    data = report.as_dict()
    summary = {k: data[k] for k in ("trade_count", "simulated_pnl", "win_rate", "max_drawdown", "brier_score",
                                     "expected_value_per_trade")}
    print(json.dumps(summary, indent=2, default=str))
    if a.out:
        with open(a.out, "w") as f:
            json.dump(data, f, indent=2, default=str)


if __name__ == "__main__":
    asyncio.run(main())
