# app/infra/pools.py
import asyncio
import time


class TokenBucket:
    """按分钟速率的简单串行间隔限流（满足 Phase 1；高并发场景 Phase 2 换分布式桶）"""

    def __init__(self, rate_per_min: int) -> None:
        self._interval = 60.0 / max(1, rate_per_min)
        self._next_at = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self._interval
        if wait:
            await asyncio.sleep(wait)
