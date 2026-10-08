import time
from typing import override

from redis.asyncio import Redis

from opsfleet_agent.core.errors import RateLimitedError
from opsfleet_agent.ports.identity import RateLimiter

_MINUTE = 60
_DAY = 86_400


class RedisRateLimiter(RateLimiter):
    def __init__(self, client: Redis, *, per_minute: int, per_day: int) -> None:
        self._client = client
        self._windows = ((_MINUTE, per_minute), (_DAY, per_day))

    @override
    async def hit(self, user_id: str) -> None:
        now = int(time.time())
        for seconds, limit in self._windows:
            key = f"ratelimit:{user_id}:{seconds}:{now // seconds}"
            async with self._client.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, seconds)
                count, _ = await pipe.execute()
            if count > limit:
                raise RateLimitedError(f"{count} requests in {seconds}s window (limit {limit})")


class UnlimitedRateLimiter(RateLimiter):
    @override
    async def hit(self, user_id: str) -> None:
        return None
