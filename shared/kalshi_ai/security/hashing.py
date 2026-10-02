"""Hashing helpers.

Activation codes are high-entropy random strings (>= 80 bits), so a keyed
HMAC-SHA256 with a server-side pepper is appropriate: it allows O(1) lookup by
hash while a database leak alone does not reveal or allow verifying codes.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from kalshi_ai.config import get_settings

# Crockford-style alphabet: no 0/O/1/I/L ambiguity for users typing codes.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def hash_code(code: str, pepper: str | None = None) -> str:
    pepper = pepper if pepper is not None else get_settings().code_hash_pepper.get_secret_value()
    return hmac.new(pepper.encode(), normalize_code(code).encode(), hashlib.sha256).hexdigest()


def generate_activation_code(groups: int = 4, group_len: int = 4) -> str:
    """Return e.g. ``KAI-ABCD-EFGH-JKMN-PQRS`` (16 random chars ~ 79 bits)."""
    parts = ["".join(secrets.choice(CODE_ALPHABET) for _ in range(group_len)) for _ in range(groups)]
    return "KAI-" + "-".join(parts)


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def sha256_hex(data: str | bytes) -> str:
    raw = data.encode() if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()
