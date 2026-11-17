"""Quota ledger correctness: atomicity, idempotency, reconciliation, windows, and concurrency."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from oasis_route.adapters.base import QuotaObservation, Usage
from oasis_route.context import AppContext
from oasis_route.domain.enums import QuotaPeriod, QuotaUnit
from oasis_route.quota import ledger
from oasis_route.quota.ledger import Demand, QuotaUnavailable
from oasis_route.quota.windows import next_reset, window_start
from tests.factories import pool, project_with_provider, reload


async def _reserve(
    ctx: AppContext,
    pid: str,
    pools: list,
    key: str,
    demand: Demand | None = None,
    allow_unknown: bool = False,
):  # type: ignore[no-untyped-def]
    return await ledger.reserve(
        ctx.db,
        idempotency_key=key,
        request_id=key,
        project_id=pid,
        connection_id="c",
        model_id="m",
        pools=pools,
        demand=demand or Demand(10, 20),
        allow_unknown=allow_unknown,
        ttl_s=60,
    )


async def test_concurrent_requests_cannot_overspend(ctx: AppContext) -> None:
    pid, prov = await project_with_provider(ctx.db)
    p = await pool(ctx.db, pid, prov, limit_value=10)

    async def attempt(i: int) -> bool:
        try:
            await _reserve(ctx, pid, [p], f"k{i}")
            return True
        except QuotaUnavailable:
            return False

    results = await asyncio.gather(*(attempt(i) for i in range(40)))
    assert sum(results) == 10
    fresh = await reload(ctx.db, p.id)
    assert fresh.reserved == 10
    assert fresh.used == 0


async def test_concurrent_token_pool_never_negative(ctx: AppContext) -> None:
    pid, prov = await project_with_provider(ctx.db)
    p = await pool(ctx.db, pid, prov, unit="total_tokens", limit_value=1000)
    demand = Demand(input_tokens=60, output_tokens=40)  # 100 per request

    async def attempt(i: int) -> bool:
        try:
            r = await _reserve(ctx, pid, [p], f"t{i}", demand)
            await ledger.settle(ctx.db, r.id, Usage(70, 50, provider_reported=True))
            return True
        except QuotaUnavailable:
            return False

    results = await asyncio.gather(*(attempt(i) for i in range(25)))
    fresh = await reload(ctx.db, p.id)
    assert fresh.reserved == 0
    # Reservation gate: each admitted request had 100 tokens of headroom at admission time.
    assert sum(results) <= 10
    assert fresh.used == 120 * sum(results)
    assert fresh.drift == 20 * sum(results)


async def test_multi_dimension_all_or_nothing(ctx: AppContext) -> None:
    pid, prov = await project_with_provider(ctx.db)
    req_pool = await pool(ctx.db, pid, prov, unit="requests", limit_value=100)
    tok_pool = await pool(ctx.db, pid, prov, unit="input_tokens", limit_value=5)
    with pytest.raises(QuotaUnavailable) as e:
        await _reserve(ctx, pid, [req_pool, tok_pool], "x", Demand(10, 1))
    assert e.value.unit == "input_tokens"
    assert (await reload(ctx.db, req_pool.id)).reserved == 0
    assert (await reload(ctx.db, tok_pool.id)).reserved == 0


async def test_idempotent_reservation(ctx: AppContext) -> None:
    pid, prov = await project_with_provider(ctx.db)
    p = await pool(ctx.db, pid, prov, limit_value=5)
    r1 = await _reserve(ctx, pid, [p], "same")
    r2 = await _reserve(ctx, pid, [p], "same")
    assert r1.id == r2.id
    assert (await reload(ctx.db, p.id)).reserved == 1


async def test_concurrent_same_key_reserves_once(ctx: AppContext) -> None:
    pid, prov = await project_with_provider(ctx.db)
    p = await pool(ctx.db, pid, prov, limit_value=5)
    rs = await asyncio.gather(*(_reserve(ctx, pid, [p], "dup") for _ in range(8)))
    assert len({r.id for r in rs}) == 1
    assert (await reload(ctx.db, p.id)).reserved == 1


async def test_settle_exactly_once_and_labels_estimates(ctx: AppContext) -> None:
    pid, prov = await project_with_provider(ctx.db)
    p = await pool(ctx.db, pid, prov, unit="output_tokens", limit_value=1000)
    r = await _reserve(ctx, pid, [p], "s", Demand(10, 200))
    assert await ledger.settle(ctx.db, r.id, None, output_estimate=50)
    assert not await ledger.settle(ctx.db, r.id, Usage(1, 1, True))
    fresh = await reload(ctx.db, p.id)
    assert fresh.reserved == 0
    assert fresh.used == 50
    assert fresh.estimated_used == 50  # estimate is labelled, never counted as provider-confirmed


async def test_release_returns_capacity(ctx: AppContext) -> None:
    pid, prov = await project_with_provider(ctx.db)
    p = await pool(ctx.db, pid, prov, limit_value=1)
    r = await _reserve(ctx, pid, [p], "a")
    with pytest.raises(QuotaUnavailable):
        await _reserve(ctx, pid, [p], "b")
    assert await ledger.release(ctx.db, r.id)
    assert not await ledger.release(ctx.db, r.id)
    await _reserve(ctx, pid, [p], "c")


async def test_concurrency_pool_released_on_settle(ctx: AppContext) -> None:
    pid, prov = await project_with_provider(ctx.db)
    p = await pool(ctx.db, pid, prov, unit="concurrent_requests", period="none", limit_value=2)
    r1 = await _reserve(ctx, pid, [p], "1")
    await _reserve(ctx, pid, [p], "2")
    with pytest.raises(QuotaUnavailable):
        await _reserve(ctx, pid, [p], "3")
    await ledger.settle(ctx.db, r1.id, Usage(1, 1, True))
    await _reserve(ctx, pid, [p], "4")
    assert (await reload(ctx.db, p.id)).used == 0


async def test_unknown_limit_is_not_unlimited(ctx: AppContext) -> None:
    pid, prov = await project_with_provider(ctx.db)
    p = await pool(ctx.db, pid, prov, limit_value=None)
    with pytest.raises(QuotaUnavailable) as e:
        await _reserve(ctx, pid, [p], "u")
    assert e.value.reason == "unknown_limit"
    r = await _reserve(ctx, pid, [p], "u2", allow_unknown=True)
    assert r.items[0].amount == 1


async def test_expire_stale_reservations(ctx: AppContext) -> None:
    pid, prov = await project_with_provider(ctx.db)
    p = await pool(ctx.db, pid, prov, limit_value=3)
    await _reserve(ctx, pid, [p], "old")
    assert await ledger.expire_stale(ctx.db, now=datetime.now(UTC) + timedelta(hours=1)) == 1
    assert (await reload(ctx.db, p.id)).reserved == 0


async def test_window_reset(ctx: AppContext) -> None:
    pid, prov = await project_with_provider(ctx.db)
    p = await pool(ctx.db, pid, prov, limit_value=1, period="day")
    day1 = datetime(2026, 3, 1, 12, tzinfo=UTC)
    r = await ledger.reserve(
        ctx.db,
        idempotency_key="d1",
        request_id="d1",
        project_id=pid,
        connection_id="c",
        model_id="m",
        pools=[p],
        demand=Demand(1, 1),
        allow_unknown=False,
        ttl_s=60,
        now=day1,
    )
    await ledger.settle(ctx.db, r.id, Usage(1, 1, True), now=day1)
    p = await reload(ctx.db, p.id)
    with pytest.raises(QuotaUnavailable) as e:
        await ledger.reserve(
            ctx.db,
            idempotency_key="d1b",
            request_id="x",
            project_id=pid,
            connection_id="c",
            model_id="m",
            pools=[p],
            demand=Demand(1, 1),
            allow_unknown=False,
            ttl_s=60,
            now=day1,
        )
    assert e.value.retry_at == datetime(2026, 3, 2, tzinfo=UTC)
    day2 = day1 + timedelta(days=1)
    await ledger.reserve(
        ctx.db,
        idempotency_key="d2",
        request_id="d2",
        project_id=pid,
        connection_id="c",
        model_id="m",
        pools=[p],
        demand=Demand(1, 1),
        allow_unknown=False,
        ttl_s=60,
        now=day2,
    )
    fresh = await reload(ctx.db, p.id)
    assert fresh.used == 0
    assert fresh.reserved == 1


async def test_one_time_credit_never_resets(ctx: AppContext) -> None:
    pid, prov = await project_with_provider(ctx.db)
    p = await pool(ctx.db, pid, prov, unit="requests", period="one_time", limit_value=1)
    r = await _reserve(ctx, pid, [p], "o1")
    await ledger.settle(ctx.db, r.id, Usage(1, 1, True))
    p = await reload(ctx.db, p.id)
    with pytest.raises(QuotaUnavailable):
        await ledger.reserve(
            ctx.db,
            idempotency_key="o2",
            request_id="o2",
            project_id=pid,
            connection_id="c",
            model_id="m",
            pools=[p],
            demand=Demand(1, 1),
            allow_unknown=False,
            ttl_s=60,
            now=datetime.now(UTC) + timedelta(days=400),
        )


async def test_expired_pool_rejected(ctx: AppContext) -> None:
    pid, prov = await project_with_provider(ctx.db)
    p = await pool(ctx.db, pid, prov, limit_value=10, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(QuotaUnavailable) as e:
        await _reserve(ctx, pid, [p], "e")
    assert e.value.reason == "expired"


async def test_shared_pool_counted_once(ctx: AppContext) -> None:
    """Two connections with the same account scope resolve to the same pool rows."""
    pid, prov = await project_with_provider(ctx.db)
    await pool(ctx.db, pid, prov, limit_value=3, account_scope="team-acct")
    async with ctx.db.session() as s:
        a = await ledger.pools_for(s, pid, prov, "team-acct", "m1")
        b = await ledger.pools_for(s, pid, prov, "team-acct", "m2")
    assert [x.id for x in a] == [x.id for x in b]
    for i in range(3):
        await _reserve(ctx, pid, a if i % 2 else b, f"sh{i}")
    with pytest.raises(QuotaUnavailable):
        await _reserve(ctx, pid, a, "sh-over")


async def test_observations_update_and_exhaust(ctx: AppContext) -> None:
    pid, prov = await project_with_provider(ctx.db)
    reset = datetime.now(UTC) + timedelta(seconds=30)
    await ledger.apply_observations(
        ctx.db,
        project_id=pid,
        provider_id=prov,
        account_scope="default",
        upstream_model="m1",
        observations=[QuotaObservation(QuotaUnit.REQUESTS, QuotaPeriod.MINUTE, 100, 0, reset)],
    )
    async with ctx.db.session() as s:
        (p,) = await ledger.pools_for(s, pid, prov, "default", "m1")
    assert p.limit_value == 100
    assert p.used == 100
    assert p.exhausted_until == reset
    assert p.needs_recheck
    with pytest.raises(QuotaUnavailable) as e:
        await _reserve(ctx, pid, [p], "obs")
    assert e.value.reason == "exhausted"
    await ledger.clear_recheck(ctx.db, [p.id], now=reset + timedelta(seconds=1))
    assert not (await reload(ctx.db, p.id)).needs_recheck


async def test_mark_exhausted_until_reset(ctx: AppContext) -> None:
    pid, prov = await project_with_provider(ctx.db)
    p = await pool(ctx.db, pid, prov, limit_value=100)
    await ledger.mark_exhausted(ctx.db, [p.id], None)
    fresh = await reload(ctx.db, p.id)
    assert fresh.exhausted_until is not None
    assert fresh.needs_recheck
    with pytest.raises(QuotaUnavailable):
        await _reserve(ctx, pid, [fresh], "mx")


def test_window_math() -> None:
    now = datetime(2026, 1, 31, 23, 30, tzinfo=UTC)
    assert window_start(QuotaPeriod.MONTH, now) == datetime(2026, 1, 1, tzinfo=UTC)
    assert next_reset(QuotaPeriod.MONTH, now) == datetime(2026, 2, 1, tzinfo=UTC)
    assert next_reset(QuotaPeriod.DAY, now, "Asia/Dhaka") == datetime(2026, 2, 1, 18, tzinfo=UTC)
    assert next_reset(QuotaPeriod.ONE_TIME, now) is None
    dec = datetime(2026, 12, 15, tzinfo=UTC)
    assert next_reset(QuotaPeriod.MONTH, dec) == datetime(2027, 1, 1, tzinfo=UTC)
