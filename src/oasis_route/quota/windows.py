"""Quota reset windows (calendar or rolling) in a pool's configured time zone."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from oasis_route.domain.enums import QuotaPeriod

_FIXED = {
    QuotaPeriod.MINUTE: timedelta(minutes=1),
    QuotaPeriod.HOUR: timedelta(hours=1),
    QuotaPeriod.DAY: timedelta(days=1),
}


def window_start(period: QuotaPeriod, now: datetime, tz: str = "UTC") -> datetime | None:
    """Start of the calendar window containing ``now``. ``None`` for non-resetting periods."""
    if period in (QuotaPeriod.ONE_TIME, QuotaPeriod.NONE):
        return None
    local = now.astimezone(ZoneInfo(tz))
    if period is QuotaPeriod.MINUTE:
        start = local.replace(second=0, microsecond=0)
    elif period is QuotaPeriod.HOUR:
        start = local.replace(minute=0, second=0, microsecond=0)
    elif period is QuotaPeriod.DAY:
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    else:  # MONTH
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC)


def next_reset(
    period: QuotaPeriod, now: datetime, tz: str = "UTC", *, rolling_start: datetime | None = None
) -> datetime | None:
    if period in (QuotaPeriod.ONE_TIME, QuotaPeriod.NONE):
        return None
    if rolling_start is not None and period in _FIXED:
        return rolling_start + _FIXED[period]
    start = window_start(period, now, tz)
    assert start is not None
    if period in _FIXED:
        # Add in local time to respect DST for day windows.
        local = start.astimezone(ZoneInfo(tz))
        return (local + _FIXED[period]).astimezone(UTC)
    local = start.astimezone(ZoneInfo(tz))
    year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
    return local.replace(year=year, month=month).astimezone(UTC)


def current_window(
    period: QuotaPeriod, now: datetime, tz: str, rolling: bool, existing: datetime | None
) -> datetime | None:
    """Window start the pool should be in now. Rolling windows start at first use and last one period."""
    if period in (QuotaPeriod.ONE_TIME, QuotaPeriod.NONE):
        return existing
    if rolling and period in _FIXED:
        if existing is None or now >= existing + _FIXED[period]:
            return now
        return existing
    return window_start(period, now, tz)


def period_seconds(period: QuotaPeriod) -> float | None:
    if period in _FIXED:
        return _FIXED[period].total_seconds()
    if period is QuotaPeriod.MONTH:
        return 30 * 86400.0
    return None
