"""Crypto- and gold-specific features. Missing sources yield ``*_available = 0`` - never invented values."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from kalshi_ai.data.base import DataPoint


def _latest(points: list[DataPoint], symbol: str, metric: str, as_of: datetime) -> DataPoint | None:
    cands = [p for p in points if p.symbol == symbol and p.metric == metric and p.ts <= as_of]
    return max(cands, key=lambda p: p.ts) if cands else None


def _previous(points: list[DataPoint], symbol: str, metric: str, before: datetime) -> DataPoint | None:
    cands = [p for p in points if p.symbol == symbol and p.metric == metric and p.ts < before]
    return max(cands, key=lambda p: p.ts) if cands else None


def crypto_features(points: list[DataPoint], as_of: datetime) -> dict[str, Any]:
    feats: dict[str, Any] = {}
    funding = _latest(points, "BTC-PERP", "funding_8h", as_of)
    oi = _latest(points, "BTC-PERP", "open_interest", as_of)
    feats["funding_available"] = 1 if funding else 0
    feats["funding_8h"] = funding.value if funding else None
    feats["oi_available"] = 1 if oi else 0
    if oi:
        prev = _previous(points, "BTC-PERP", "open_interest", oi.ts - timedelta(minutes=55))
        feats["oi_change_1h"] = (oi.value / prev.value - 1) if prev and prev.value else None
    for name, metric in (
        ("btc_dominance", "dominance"),
        ("stablecoin_netflow", "netflow"),
        ("liquidations_1h", "liquidations"),
    ):
        p = _latest(points, "CRYPTO", metric, as_of)
        feats[f"{name}_available"] = 1 if p else 0
        feats[name] = p.value if p else None
    return feats


def trading_session(ts: datetime) -> dict[str, int]:
    """Approximate FX/metal sessions in UTC: Asia 23-08, London 07-16, New York 12-21 (overlaps intended)."""
    h = ts.astimezone(UTC).hour
    return {
        "session_asia": 1 if (h >= 23 or h < 8) else 0,
        "session_london": 1 if 7 <= h < 16 else 0,
        "session_new_york": 1 if 12 <= h < 21 else 0,
        "session_london_ny_overlap": 1 if 12 <= h < 16 else 0,
        "weekend": 1 if ts.weekday() >= 5 else 0,
    }


GC_ACTIVE_MONTHS = (2, 4, 6, 8, 10, 12)  # COMEX gold primary delivery months


def gc_front_contract(today: date, roll_days: int = 5) -> tuple[int, int, bool]:
    """Return (year, month, in_roll_window) for the most liquid COMEX GC contract.

    First notice day is (approximately) the last business day of the month before the
    delivery month; volume typically rolls ~``roll_days`` before that. Approximation only -
    verify against your licensed data vendor's roll calendar.
    """
    candidates = [(today.year, m) for m in GC_ACTIVE_MONTHS if date(today.year, m, 1) > today]
    candidates += [(today.year + 1, m) for m in GC_ACTIVE_MONTHS]
    first = date(candidates[0][0], candidates[0][1], 1)
    first_notice = first - timedelta(days=1)
    roll_date = first_notice - timedelta(days=roll_days)
    if today >= roll_date:
        return candidates[1][0], candidates[1][1], today <= first_notice
    return candidates[0][0], candidates[0][1], (roll_date - today).days <= 2


def gold_features(points: list[DataPoint], as_of: datetime, calendar: list[dict[str, Any]]) -> dict[str, Any]:
    feats: dict[str, Any] = dict(trading_session(as_of))
    spot = _latest(points, "XAUUSD", "price", as_of)
    feats["gold_spot_available"] = 1 if spot else 0
    feats["gold_spot"] = spot.value if spot else None
    for series, key in (("DGS10", "us10y"), ("DFII10", "real10y"), ("DTWEXBGS", "usd_index")):
        latest = None
        for metric in ("us10y_yield", "us10y_real_yield", "usd_broad_index"):
            latest = latest or _latest(points, series, metric, as_of)
        feats[f"{key}_available"] = 1 if latest else 0
        feats[key] = latest.value if latest else None
        if latest:
            prev = _previous(points, series, latest.metric, latest.ts)
            feats[f"{key}_change"] = (latest.value - prev.value) if prev else None
    y, m, rolling = gc_front_contract(as_of.date())
    feats["gc_front_contract"] = f"GC{y % 100:02d}{m:02d}"
    feats["gc_roll_window"] = 1 if rolling else 0
    feats.update(event_proximity(calendar, as_of, asset="gold"))
    return feats


def event_proximity(calendar: list[dict[str, Any]], as_of: datetime, asset: str) -> dict[str, Any]:
    upcoming, recent = None, None
    for ev in calendar:
        if asset not in ev.get("assets", [asset]):
            continue
        try:
            t = datetime.fromisoformat(str(ev["time"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        delta = (t - as_of).total_seconds() / 60
        if delta >= 0 and (upcoming is None or delta < upcoming[0]):
            upcoming = (delta, ev)
        if -240 <= delta < 0 and (recent is None or delta > recent[0]):
            recent = (delta, ev)
    return {
        "event_minutes_until": upcoming[0] if upcoming else None,
        "event_high_importance_within_60m": 1
        if upcoming and upcoming[0] <= 60 and upcoming[1].get("importance") == "high"
        else 0,
        "event_fed_related_within_24h": 1
        if upcoming
        and upcoming[0] <= 1440
        and any(k in upcoming[1].get("name", "").lower() for k in ("fomc", "fed", "powell"))
        else 0,
        "event_minutes_since": -recent[0] if recent else None,
    }
