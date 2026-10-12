"""Lightweight, deterministic finance-tuned lexicon sentiment.

A transparent baseline that needs no external API. It can be swapped for a
licensed NLP model behind the same function signature.
"""

from __future__ import annotations

import hashlib
import re

POSITIVE = {
    "surge": 2,
    "surges": 2,
    "soar": 2,
    "soars": 2,
    "rally": 2,
    "rallies": 2,
    "jump": 1.5,
    "jumps": 1.5,
    "gain": 1,
    "gains": 1,
    "rise": 1,
    "rises": 1,
    "record": 1,
    "bullish": 2,
    "beat": 1,
    "beats": 1,
    "approve": 1.5,
    "approved": 1.5,
    "approval": 1.5,
    "inflows": 1.5,
    "adoption": 1,
    "upgrade": 1,
    "strong": 1,
    "optimism": 1,
    "breakout": 1.5,
    "highs": 1,
    "dovish": 1,
    "cut": 0.5,
    "cuts": 0.5,
    "recovery": 1,
    "rebound": 1.5,
    "buy": 0.5,
    "accumulate": 1,
}
NEGATIVE = {
    "plunge": -2,
    "plunges": -2,
    "crash": -2.5,
    "crashes": -2.5,
    "tumble": -2,
    "tumbles": -2,
    "drop": -1.5,
    "drops": -1.5,
    "fall": -1,
    "falls": -1,
    "slump": -1.5,
    "bearish": -2,
    "miss": -1,
    "misses": -1,
    "reject": -1.5,
    "rejected": -1.5,
    "ban": -2,
    "bans": -2,
    "hack": -2.5,
    "hacked": -2.5,
    "outflows": -1.5,
    "lawsuit": -1.5,
    "fraud": -2.5,
    "weak": -1,
    "fear": -1.5,
    "selloff": -2,
    "sell-off": -2,
    "lows": -1,
    "hawkish": -1,
    "hike": -0.5,
    "hikes": -0.5,
    "liquidation": -1.5,
    "liquidations": -1.5,
    "recession": -1.5,
    "downgrade": -1,
    "default": -2,
    "sell": -0.5,
    "dump": -1.5,
}
NEGATORS = {"not", "no", "never", "without", "fails", "failed"}
TOKEN_RE = re.compile(r"[a-z][a-z\-']+")


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def sentiment_score(text: str) -> float:
    """Returns sentiment in [-1, 1]."""
    tokens = tokenize(text)
    if not tokens:
        return 0.0
    score = 0.0
    for i, tok in enumerate(tokens):
        w = POSITIVE.get(tok) or NEGATIVE.get(tok)
        if w is None:
            continue
        if any(t in NEGATORS for t in tokens[max(0, i - 3) : i]):
            w = -w * 0.5
        score += w
    return max(-1.0, min(1.0, score / 4.0))


def fingerprint(text: str) -> str:
    """Near-duplicate fingerprint: normalized, de-noised token set."""
    toks = sorted(set(t for t in tokenize(text) if len(t) > 3))[:40]
    return hashlib.sha256(" ".join(toks).encode()).hexdigest()


def shingles(text: str, k: int = 3) -> set[str]:
    toks = tokenize(text)
    return {" ".join(toks[i : i + k]) for i in range(max(1, len(toks) - k + 1))}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
