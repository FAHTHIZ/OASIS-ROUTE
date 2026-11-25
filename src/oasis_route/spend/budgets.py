"""Project spend budgets: USD caps per day, month or lifetime, with forecast.

Amounts are integer USD micros. Every request that could cost money holds its worst-case cost against
every budget of its project in the same transaction as its quota reservation (quota/ledger.py), so
concurrent requests can never overshoot a cap. Settlement replaces the hold with the actual cost; when
the provider reports no usage the worst case is kept (never under-count).

Free routes hold nothing. A route of unknown cost is never admitted while a budget exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oasis_route.db.models import SpendBudget, utcnow
from oasis_route.domain.enums import QuotaPeriod
from oasis_route.quota.windows import current_window, next_reset

PERIODS = {"day": QuotaPeriod.DAY, "month": QuotaPeriod.MONTH, "total": QuotaPeriod.ONE_TIME}
STORED = {v.value: k for k, v in PERIODS.items()}
MICROS = 1_000_000


async def for_project(session: AsyncSession, project_id: str) -> list[SpendBudget]:
    q = select(SpendBudget).where(SpendBudget.project_id == project_id).order_by(SpendBudget.period)
    return list((await session.execute(q)).scalars())


def _used(b: SpendBudget, now: datetime) -> int:
    """Used amount in the current window (a window that has rolled over counts as unused)."""
    target = current_window(QuotaPeriod(b.period), now, b.reset_tz, False, b.window_start)
    return 0 if (target is not None and target != b.window_start) else b.used_usd_micros


def remaining(b: SpendBudget, now: datetime) -> int:
    return max(0, b.limit_usd_micros - _used(b, now) - b.reserved_usd_micros)


@dataclass(frozen=True)
class Headroom:
    remaining_usd_micros: int
    budget_id: str
    resets_in_s: float | None


def tightest(budgets: list[SpendBudget], now: datetime) -> Headroom | None:
    if not budgets:
        return None
    b = min(budgets, key=lambda x: remaining(x, now))
    reset = next_reset(QuotaPeriod(b.period), now, b.reset_tz)
    return Headroom(remaining(b, now), b.id, (reset - now).total_seconds() if reset else None)


def view(b: SpendBudget, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    period = QuotaPeriod(b.period)
    used = _used(b, now)
    start = current_window(period, now, b.reset_tz, False, b.window_start)
    reset = next_reset(period, now, b.reset_tz)
    forecast: dict[str, Any] = {
        "burn_rate_usd_per_hour": None,
        "projected_window_usd": None,
        "exhausts_at": None,
    }
    if start is not None and reset is not None:
        elapsed_h = max((now - start).total_seconds() / 3600, 1 / 60)
        rate = used / elapsed_h  # micros per hour
        window_h = (reset - start).total_seconds() / 3600
        forecast["burn_rate_usd_per_hour"] = rate / MICROS
        forecast["projected_window_usd"] = rate * window_h / MICROS
        left = b.limit_usd_micros - used - b.reserved_usd_micros
        if rate > 0 and left > 0:
            hours = left / rate
            if hours < (reset - now).total_seconds() / 3600:
                forecast["exhausts_at"] = datetime.fromtimestamp(
                    now.timestamp() + hours * 3600, tz=now.tzinfo
                )
        elif left <= 0:
            forecast["exhausts_at"] = now
    return {
        "id": b.id,
        "period": STORED.get(b.period, b.period),
        "limit_usd": b.limit_usd_micros / MICROS,
        "used_usd": used / MICROS,
        "reserved_usd": b.reserved_usd_micros / MICROS,
        "remaining_usd": remaining(b, now) / MICROS,
        "drift_usd": b.drift_usd_micros / MICROS,
        "window_start": start,
        "resets_at": reset,
        "forecast": forecast,
    }


async def put(session: AsyncSession, project_id: str, period: str, limit_usd: float) -> SpendBudget:
    stored = PERIODS[period].value
    row = (
        await session.execute(
            select(SpendBudget).where(SpendBudget.project_id == project_id, SpendBudget.period == stored)
        )
    ).scalar_one_or_none()
    micros = round(limit_usd * MICROS)
    if row is None:
        now = utcnow()
        row = SpendBudget(
            project_id=project_id,
            period=stored,
            limit_usd_micros=micros,
            window_start=current_window(PERIODS[period], now, "UTC", False, None),
        )
        session.add(row)
    else:
        row.limit_usd_micros = micros
    await session.flush()
    return row
