"""Redis-backed fixed-window rate limiter.

Security-sensitive limiters (activation codes) FAIL CLOSED when Redis is down:
we would rather temporarily block code redemption than allow brute force.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

from kalshi_ai.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after_seconds: int
    backend_available: bool = True


class RateLimiter:
    def __init__(self, redis: Redis, *, fail_closed: bool = True):
        self.redis = redis
        self.fail_closed = fail_closed

    async def hit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        window = int(time.time() // window_seconds)
        redis_key = f"rl:{key}:{window}"
        try:
            pipe = self.redis.pipeline(transaction=True)
            pipe.incr(redis_key)
            pipe.expire(redis_key, window_seconds)
            count, _ = await pipe.execute()
        except (RedisError, OSError) as exc:
            log.warning("rate_limiter_unavailable", key_prefix=key.split(":")[0], error=type(exc).__name__)
            return RateLimitResult(not self.fail_closed, 0, window_seconds, backend_available=False)
        count = int(count)
        retry_after = window_seconds - int(time.time() % window_seconds)
        return RateLimitResult(count <= limit, max(0, limit - count), retry_after if count > limit else 0)

    async def peek(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        window = int(time.time() // window_seconds)
        try:
            raw = await self.redis.get(f"rl:{key}:{window}")
        except (RedisError, OSError):
            return RateLimitResult(not self.fail_closed, 0, window_seconds, backend_available=False)
        count = int(raw or 0)
        return RateLimitResult(count < limit, max(0, limit - count), 0)
