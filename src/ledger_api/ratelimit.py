"""Per-API-key token bucket in Redis, shared by every API instance (ADR 7)."""

import logging
import math

import redis

logger = logging.getLogger("ledger_api.ratelimit")

# Runs atomically inside Redis, so concurrent requests cannot both take the last token.
# Redis's own clock is used, so instances with skewed clocks still agree on the refill.
_TAKE_TOKEN = """
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local time = redis.call('TIME')
local now = time[1] * 1000 + time[2] / 1000
local bucket = redis.call('HMGET', KEYS[1], 'tokens', 'at')
local tokens = tonumber(bucket[1]) or capacity
local at = tonumber(bucket[2]) or now
tokens = math.min(capacity, tokens + (now - at) * rate / 1000)
local wait_ms = 0
if tokens >= 1 then
    tokens = tokens - 1
else
    wait_ms = math.ceil((1 - tokens) * 1000 / rate)
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'at', tostring(now))
redis.call('PEXPIRE', KEYS[1], math.ceil(capacity * 1000 / rate) + 1000)
return wait_ms
"""


class RateLimited(Exception):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"retry after {retry_after_seconds} s")
        self.retry_after_seconds = retry_after_seconds


class RateLimiter:
    def __init__(self, client: redis.Redis, per_second: float, burst: int) -> None:
        self._script = client.register_script(_TAKE_TOKEN)
        self._per_second = per_second
        self._burst = burst

    def check(self, key_prefix: str) -> None:
        """Raises RateLimited when this API key has no token left."""
        try:
            wait_ms = int(
                self._script(keys=[f"ratelimit:{key_prefix}"], args=[self._per_second, self._burst])
            )
        except redis.RedisError:
            # Fail open: losing Redis must never stop payments, only the rate limit.
            logger.warning("rate limit unavailable, request allowed", exc_info=True)
            return
        if wait_ms > 0:
            raise RateLimited(math.ceil(wait_ms / 1000))
