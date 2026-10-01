"""In-process async token buckets, one per (broker account, call category).

Correct only with a single worker process (the deployment model; see docs/PLAN.md Step 13).
Scaling out would need a shared limiter (e.g. Redis)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


class TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: float | None = None, clock: Callable[[], float] = time.monotonic):
        if rate_per_sec <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate_per_sec
        self.capacity = capacity if capacity is not None else max(1.0, rate_per_sec)
        self._tokens = self.capacity
        self._clock = clock
        self._updated = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
        self._updated = now

    async def acquire(self) -> None:
        # Holding the lock while sleeping makes waiters queue in arrival order.
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) / self.rate)


class RateLimiterRegistry:
    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str], TokenBucket] = {}

    def get(self, key: str, category: str, rate_per_sec: float) -> TokenBucket:
        bucket = self._buckets.get((key, category))
        if bucket is None or bucket.rate != rate_per_sec:
            bucket = TokenBucket(rate_per_sec)
            self._buckets[(key, category)] = bucket
        return bucket
