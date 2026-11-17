"""Authoritative quota ledger.

Invariants (docs/ROUTING_AND_QUOTAS.md):
- Reservations are made against shared pool rows, never per-credential rows.
- Every dimension a request touches is reserved in ONE transaction using conditional atomic updates
  (``UPDATE ... SET reserved = reserved + n WHERE limit - used - reserved >= n``). If any dimension fails,
  the whole reservation rolls back.
- Reservations are idempotent by ``idempotency_key``.
- Settlement happens exactly once (conditional status transition) and records provider-reported vs estimated
  usage separately, plus drift.
- Unknown limits are never treated as unlimited: they are reservable only when the project opts in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from oasis_route.adapters.base import QuotaObservation, Usage
from oasis_route.db.models import QuotaPool, Reservation, ReservationItem, utcnow
from oasis_route.db.session import Database
from oasis_route.domain.enums import QuotaPeriod, QuotaSource, QuotaUnit, ReservationStatus
from oasis_route.quota.windows import current_window, next_reset


@dataclass(frozen=True)
class Demand:
    input_tokens: int
    output_tokens: int
    requests: int = 1
    credits_micros: int | None = None  # None == cost unknown

    def amount(self, unit: str) -> int | None:
        u = QuotaUnit(unit)
        if u is QuotaUnit.REQUESTS or u is QuotaUnit.CONCURRENT:
            return self.requests
        if u is QuotaUnit.INPUT_TOKENS:
            return self.input_tokens
        if u is QuotaUnit.OUTPUT_TOKENS:
            return self.output_tokens
        if u is QuotaUnit.TOTAL_TOKENS:
            return self.input_tokens + self.output_tokens
        return self.credits_micros


class QuotaUnavailable(Exception):
    def __init__(self, pool_id: str, unit: str, reason: str, retry_at: datetime | None = None) -> None:
        super().__init__(f"quota unavailable: {unit} ({reason})")
        self.pool_id = pool_id
        self.unit = unit
        self.reason = reason
        self.retry_at = retry_at


def pool_filter(project_id: str, provider_id: str, account_scope: str, upstream_model: str) -> Any:
    return and_(
        QuotaPool.project_id == project_id,
        QuotaPool.provider_id == provider_id,
        QuotaPool.account_scope == account_scope,
        or_(QuotaPool.model_scope == "*", QuotaPool.model_scope == upstream_model),
    )


async def pools_for(
    session: AsyncSession, project_id: str, provider_id: str, account_scope: str, upstream_model: str
) -> list[QuotaPool]:
    q = select(QuotaPool).where(pool_filter(project_id, provider_id, account_scope, upstream_model))
    return list((await session.execute(q.order_by(QuotaPool.id))).scalars().all())


async def roll_windows(session: AsyncSession, pools: list[QuotaPool], now: datetime) -> None:
    """Advance pools whose reset window has passed. Idempotent and safe under concurrency."""
    for p in pools:
        target = current_window(QuotaPeriod(p.period), now, p.reset_tz, p.rolling, p.window_start)
        if target is None or target == p.window_start:
            continue
        await session.execute(
            update(QuotaPool)
            .where(
                QuotaPool.id == p.id, or_(QuotaPool.window_start.is_(None), QuotaPool.window_start < target)
            )
            .values(used=0, estimated_used=0, window_start=target)
            .execution_options(synchronize_session=False)
        )
        p.window_start = target


def available(p: QuotaPool, now: datetime) -> int | None:
    """Locally available amount (None == unknown). Exhausted/expired pools report 0."""
    if p.expires_at is not None and p.expires_at <= now:
        return 0
    if p.exhausted_until is not None and p.exhausted_until > now:
        return 0
    if p.limit_value is None:
        return None
    return max(0, p.limit_value - p.used - p.reserved)


async def reserve(
    db: Database,
    *,
    idempotency_key: str,
    request_id: str,
    project_id: str,
    connection_id: str,
    model_id: str,
    pools: list[QuotaPool],
    demand: Demand,
    allow_unknown: bool,
    ttl_s: int,
    now: datetime | None = None,
) -> Reservation:
    now = now or utcnow()
    try:
        async with db.transaction() as s:
            existing = (
                await s.execute(select(Reservation).where(Reservation.idempotency_key == idempotency_key))
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            await roll_windows(s, pools, now)
            items: list[ReservationItem] = []
            for p in sorted(pools, key=lambda x: x.id):  # stable order avoids lock-order deadlocks
                amount = demand.amount(p.unit)
                if amount is None:
                    if p.limit_value is None and allow_unknown:
                        continue
                    raise QuotaUnavailable(p.id, p.unit, "cost_unknown")
                if amount <= 0:
                    continue
                conds = [
                    QuotaPool.id == p.id,
                    or_(QuotaPool.exhausted_until.is_(None), QuotaPool.exhausted_until <= now),
                    or_(QuotaPool.expires_at.is_(None), QuotaPool.expires_at > now),
                ]
                if p.limit_value is None:
                    if not allow_unknown:
                        raise QuotaUnavailable(p.id, p.unit, "unknown_limit")
                    conds.append(QuotaPool.limit_value.is_(None))
                else:
                    conds.append(QuotaPool.limit_value - QuotaPool.used - QuotaPool.reserved >= amount)
                result = await s.execute(
                    update(QuotaPool)
                    .where(*conds)
                    .values(reserved=QuotaPool.reserved + amount)
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:  # type: ignore[attr-defined]
                    fresh = await s.get(QuotaPool, p.id, populate_existing=True)
                    reason, retry_at = "insufficient", None
                    if fresh is not None:
                        if fresh.expires_at is not None and fresh.expires_at <= now:
                            reason = "expired"
                        elif fresh.exhausted_until is not None and fresh.exhausted_until > now:
                            reason, retry_at = "exhausted", fresh.exhausted_until
                        else:
                            retry_at = next_reset(
                                QuotaPeriod(fresh.period),
                                now,
                                fresh.reset_tz,
                                rolling_start=fresh.window_start if fresh.rolling else None,
                            )
                    raise QuotaUnavailable(p.id, p.unit, reason, retry_at)
                items.append(ReservationItem(pool_id=p.id, unit=p.unit, amount=amount))
            r = Reservation(
                idempotency_key=idempotency_key,
                request_id=request_id,
                project_id=project_id,
                connection_id=connection_id,
                model_id=model_id,
                status=ReservationStatus.RESERVED.value,
                estimate={
                    "input_tokens": demand.input_tokens,
                    "output_tokens": demand.output_tokens,
                    "approximate": True,
                },
                expires_at=now + timedelta(seconds=ttl_s),
                items=items,
            )
            s.add(r)
            await s.flush()
            return r
    except IntegrityError:
        # A concurrent call with the same idempotency key won the race; its reservation is authoritative.
        async with db.session() as s:
            winner = (
                await s.execute(select(Reservation).where(Reservation.idempotency_key == idempotency_key))
            ).scalar_one()
            return winner


def _actual(unit: str, amount: int, usage: Usage | None, output_estimate: int | None) -> tuple[int, bool]:
    """Return (actual amount, is_estimate) for a reserved item."""
    u = QuotaUnit(unit)
    if u is QuotaUnit.REQUESTS:
        return 1, False
    if u is QuotaUnit.CONCURRENT:
        return 0, False
    if usage is not None and usage.provider_reported:
        if u is QuotaUnit.INPUT_TOKENS:
            return usage.input_tokens, False
        if u is QuotaUnit.OUTPUT_TOKENS:
            return usage.output_tokens, False
        if u is QuotaUnit.TOTAL_TOKENS:
            return usage.total_tokens, False
        return amount, True  # credits: no provider cost report; keep estimate, labelled
    # No provider usage: keep the conservative estimate (full reserved amount), or a measured output estimate.
    if output_estimate is not None and u is QuotaUnit.OUTPUT_TOKENS:
        return output_estimate, True
    return amount, True


async def settle(
    db: Database,
    reservation_id: str,
    usage: Usage | None,
    *,
    output_estimate: int | None = None,
    now: datetime | None = None,
) -> bool:
    """Commit actual usage exactly once. Returns False if already settled/expired."""
    now = now or utcnow()
    estimated = usage is None or not usage.provider_reported
    async with db.transaction() as s:
        res = await s.execute(
            update(Reservation)
            .where(Reservation.id == reservation_id, Reservation.status == ReservationStatus.RESERVED.value)
            .values(
                status=ReservationStatus.COMMITTED.value,
                settled_at=now,
                usage_estimated=estimated,
                actual={
                    "input_tokens": usage.input_tokens if usage else None,
                    "output_tokens": usage.output_tokens if usage else None,
                    "provider_reported": bool(usage and usage.provider_reported),
                },
            )
            .execution_options(synchronize_session=False)
        )
        if res.rowcount != 1:  # type: ignore[attr-defined]
            return False
        items = (
            (await s.execute(select(ReservationItem).where(ReservationItem.reservation_id == reservation_id)))
            .scalars()
            .all()
        )
        for it in items:
            actual, is_est = _actual(it.unit, it.amount, usage, output_estimate)
            it.actual = actual
            values: dict[str, Any] = {"reserved": QuotaPool.reserved - it.amount}
            if it.unit != QuotaUnit.CONCURRENT.value:
                values["used"] = QuotaPool.used + actual
                values["drift"] = QuotaPool.drift + (actual - it.amount)
                if is_est:
                    values["estimated_used"] = QuotaPool.estimated_used + actual
            await s.execute(
                update(QuotaPool)
                .where(QuotaPool.id == it.pool_id)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
    return True


async def release(
    db: Database,
    reservation_id: str,
    *,
    status: ReservationStatus = ReservationStatus.RELEASED,
    count_request: bool = False,
    now: datetime | None = None,
) -> bool:
    """Return reserved capacity. ``count_request`` charges one request (the upstream did receive it)."""
    now = now or utcnow()
    async with db.transaction() as s:
        res = await s.execute(
            update(Reservation)
            .where(Reservation.id == reservation_id, Reservation.status == ReservationStatus.RESERVED.value)
            .values(status=status.value, settled_at=now)
            .execution_options(synchronize_session=False)
        )
        if res.rowcount != 1:  # type: ignore[attr-defined]
            return False
        items = (
            (await s.execute(select(ReservationItem).where(ReservationItem.reservation_id == reservation_id)))
            .scalars()
            .all()
        )
        for it in items:
            values: dict[str, Any] = {"reserved": QuotaPool.reserved - it.amount}
            if count_request and it.unit == QuotaUnit.REQUESTS.value:
                values["used"] = QuotaPool.used + 1
                it.actual = 1
            else:
                it.actual = 0
            await s.execute(
                update(QuotaPool)
                .where(QuotaPool.id == it.pool_id)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
    return True


async def expire_stale(db: Database, now: datetime | None = None) -> int:
    """Release reservations whose holder never settled them (crash, lost connection)."""
    now = now or utcnow()
    async with db.session() as s:
        ids = (
            (
                await s.execute(
                    select(Reservation.id).where(
                        Reservation.status == ReservationStatus.RESERVED.value, Reservation.expires_at < now
                    )
                )
            )
            .scalars()
            .all()
        )
    count = 0
    for rid in ids:
        if await release(db, rid, status=ReservationStatus.EXPIRED, now=now):
            count += 1
    return count


async def apply_observations(
    db: Database,
    *,
    project_id: str,
    provider_id: str,
    account_scope: str,
    upstream_model: str,
    observations: list[QuotaObservation],
    now: datetime | None = None,
) -> None:
    """Fold provider-reported quota facts into the ledger.

    Provider counts can only make local accounting more conservative (``used`` never decreases from headers).
    Pools are auto-created for observed dimensions with a known limit, scoped to the model.
    """
    if not observations:
        return
    now = now or utcnow()
    async with db.transaction() as s:
        for ob in observations:
            q = (
                select(QuotaPool)
                .where(
                    QuotaPool.project_id == project_id,
                    QuotaPool.provider_id == provider_id,
                    QuotaPool.account_scope == account_scope,
                    QuotaPool.unit == ob.unit.value,
                    QuotaPool.period == ob.period.value,
                    or_(QuotaPool.model_scope == upstream_model, QuotaPool.model_scope == "*"),
                )
                .order_by(QuotaPool.model_scope.desc())
            )  # exact model scope before "*"
            pool = (await s.execute(q)).scalars().first()
            if pool is None:
                if ob.limit is None:
                    continue
                pool = QuotaPool(
                    project_id=project_id,
                    provider_id=provider_id,
                    account_scope=account_scope,
                    model_scope=upstream_model,
                    unit=ob.unit.value,
                    period=ob.period.value,
                    rolling=True,
                    limit_value=ob.limit,
                    used=0,
                    reserved=0,
                    window_start=now,
                    source=QuotaSource.RESPONSE_HEADERS.value,
                    confidence=0.8,
                )
                s.add(pool)
                await s.flush()
            if pool.limit_value is None and ob.limit is not None:
                pool.limit_value = ob.limit
                pool.source = QuotaSource.RESPONSE_HEADERS.value
            pool.observed_remaining = ob.remaining
            pool.observed_at = now
            pool.last_verified_at = now
            if ob.limit is not None and ob.remaining is not None and pool.limit_value == ob.limit:
                provider_used = max(0, ob.limit - ob.remaining)
                if provider_used > pool.used:
                    pool.drift += provider_used - pool.used
                    pool.used = provider_used
            if ob.remaining == 0:
                pool.exhausted_until = ob.reset_at or next_reset(QuotaPeriod(pool.period), now, pool.reset_tz)
                pool.needs_recheck = True


async def mark_exhausted(
    db: Database, pool_ids: list[str], until: datetime | None, now: datetime | None = None
) -> None:
    """Provider said QUOTA_EXHAUSTED: block pools until reset (or next window) and require a recheck."""
    now = now or utcnow()
    async with db.transaction() as s:
        for pid in pool_ids:
            p = await s.get(QuotaPool, pid)
            if p is None:
                continue
            p.exhausted_until = (
                until or next_reset(QuotaPeriod(p.period), now, p.reset_tz) or (now + timedelta(hours=1))
            )
            p.needs_recheck = True
            p.confidence = min(p.confidence, 0.5)


async def clear_recheck(db: Database, pool_ids: list[str], now: datetime | None = None) -> None:
    """A successful request after reset proves capacity is available again."""
    now = now or utcnow()
    async with db.transaction() as s:
        await s.execute(
            update(QuotaPool)
            .where(
                QuotaPool.id.in_(pool_ids),
                QuotaPool.needs_recheck.is_(True),
                or_(QuotaPool.exhausted_until.is_(None), QuotaPool.exhausted_until <= now),
            )
            .values(needs_recheck=False, exhausted_until=None, last_verified_at=now)
            .execution_options(synchronize_session=False)
        )
