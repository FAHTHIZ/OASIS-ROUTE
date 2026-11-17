"""In-process token-bucket rate limiter (per instance; see ADR-0001)."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    updated: float


class TokenBucketLimiter:
    def __init__(self, max_keys: int = 100_000) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._max_keys = max_keys

    def check(self, key: str, rate_per_minute: int, now: float | None = None) -> tuple[bool, float]:
        """Consume one token. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic() if now is None else now
        capacity = float(rate_per_minute)
        refill = capacity / 60.0
        b = self._buckets.get(key)
        if b is None:
            if len(self._buckets) >= self._max_keys:
                self._buckets.clear()  # bounded memory; worst case a brief reset of limits
            b = _Bucket(tokens=capacity, updated=now)
            self._buckets[key] = b
        b.tokens = min(capacity, b.tokens + (now - b.updated) * refill)
        b.updated = now
        if b.tokens >= 1.0:
            b.tokens -= 1.0
            return True, 0.0
        return False, (1.0 - b.tokens) / refill
