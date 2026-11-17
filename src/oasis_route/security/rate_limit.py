"""Per-key rate limiters.

``TokenBucketLimiter`` is in-process (one replica). ``DatabaseRateLimiter`` keeps the counters in the database
so the limit holds across replicas; ``OASIS_ROUTE_RATE_LIMIT_BACKEND`` selects between them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from oasis_route.db.models import RateLimitWindow


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


class DatabaseRateLimiter:
    """Global per-key limit shared by every replica, using a sliding window over per-minute counters.

    The estimate is ``previous_minute * (1 - elapsed_fraction) + current_minute``, the usual sliding-window
    approximation: it never allows more than ``limit`` in any 60 s span by more than rounding, and needs one
    upsert plus one read per request. Rejected requests are not counted.
    """

    async def check(
        self, session: AsyncSession, key: str, rate_per_minute: int, now: float | None = None
    ) -> tuple[bool, float]:
        now = time.time() if now is None else now
        window = int(now // 60)
        frac = (now % 60) / 60.0
        rows = (
            await session.execute(
                select(RateLimitWindow.window, RateLimitWindow.count).where(
                    RateLimitWindow.key == key, RateLimitWindow.window.in_((window - 1, window))
                )
            )
        ).all()
        counts = {w: c for w, c in rows}
        prev, cur = counts.get(window - 1, 0), counts.get(window, 0)
        estimate = prev * (1.0 - frac) + cur
        if estimate + 1 > rate_per_minute:
            # Time until enough of the previous minute has slid out (or the next minute starts).
            if prev > 0 and cur + 1 <= rate_per_minute:
                need = (estimate + 1 - rate_per_minute) / prev
                return False, max(0.01, need * 60.0)
            return False, max(0.01, 60.0 - (now % 60))
        ins = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
        stmt = ins(RateLimitWindow).values(key=key, window=window, count=1)
        stmt = stmt.on_conflict_do_update(
            index_elements=["key", "window"], set_={"count": RateLimitWindow.count + 1}
        )
        await session.execute(stmt)
        if cur == 0:  # first request this minute for the key: drop its stale windows
            await session.execute(
                delete(RateLimitWindow).where(RateLimitWindow.key == key, RateLimitWindow.window < window - 1)
            )
        return True, 0.0
