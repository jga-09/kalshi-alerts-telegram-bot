"""Fee ESTIMATE for simulations. Kalshi's published taker fee has the form
ceil_to_cent(rate * contracts * P * (1 - P)); the rate and rounding vary by series and
change over time - verify against Kalshi's current fee schedule. Live trading uses the
actual fees Kalshi reports on fills.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

DEFAULT_TAKER_RATE = Decimal("0.07")
CENT = Decimal("0.01")


def estimate_taker_fee(contracts: int | Decimal, price: Decimal, rate: Decimal = DEFAULT_TAKER_RATE) -> Decimal:
    raw = rate * Decimal(contracts) * price * (Decimal(1) - price)
    return raw.quantize(CENT, rounding=ROUND_CEILING)
