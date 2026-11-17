from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import select

from oasis_route.context import AppContext
from oasis_route.db.models import Connection, HealthRecord, utcnow
from oasis_route.domain.enums import ErrorCategory, HealthState
from oasis_route.health import breaker
from tests.factories import project_with_provider


async def _conn(ctx: AppContext) -> str:
    pid, prov = await project_with_provider(ctx.db)
    async with ctx.db.transaction() as s:
        c = Connection(project_id=pid, provider_id=prov, status="active")
        s.add(c)
        await s.flush()
        return c.id


async def _avail(ctx: AppContext, cid: str, model: str = "m", now=None) -> breaker.Availability:
    async with ctx.db.session() as s:
        by = await breaker.load(s, [cid])
    return breaker.evaluate(breaker.records_for(by, cid, model), now or utcnow())


async def test_opens_after_threshold_and_backs_off(ctx: AppContext) -> None:
    cid = await _conn(ctx)
    for i in range(ctx.settings.breaker_failure_threshold - 1):
        rec = await breaker.record_failure(ctx.db, ctx.settings, cid, "m", ErrorCategory.PROVIDER_UNAVAILABLE)
        assert rec is not None and rec.state == HealthState.DEGRADED.value, i
    assert (await _avail(ctx, cid)).routable
    rec = await breaker.record_failure(ctx.db, ctx.settings, cid, "m", ErrorCategory.TIMEOUT)
    assert rec is not None and rec.state == HealthState.OPEN.value and rec.cooldown_level == 1
    a = await _avail(ctx, cid)
    assert not a.routable and a.reason == "circuit_open"
    # after cooldown a single probe is permitted
    later = utcnow() + timedelta(seconds=10)
    assert (await _avail(ctx, cid, now=later)).needs_probe
    assert await breaker.claim_probe(ctx.db, cid, "m", now=later)
    assert not await breaker.claim_probe(ctx.db, cid, "m", now=later)
    # failed probe reopens with a longer cooldown
    rec = await breaker.record_failure(ctx.db, ctx.settings, cid, "m", ErrorCategory.TIMEOUT, now=later)
    assert rec is not None and rec.cooldown_level == 2 and rec.state == HealthState.OPEN.value
    assert rec.open_until is not None
    assert (rec.open_until - later).total_seconds() <= ctx.settings.breaker_max_cooldown_s


async def test_success_closes_circuit_and_tracks_latency(ctx: AppContext) -> None:
    cid = await _conn(ctx)
    await breaker.record_failure(
        ctx.db, ctx.settings, cid, "m", ErrorCategory.RATE_LIMITED, retry_after_s=0.01
    )
    await asyncio.sleep(0.02)
    assert await breaker.claim_probe(ctx.db, cid, "m")
    await breaker.record_success(ctx.db, cid, "m", 100.0)
    await breaker.record_success(ctx.db, cid, "m", 200.0)
    async with ctx.db.session() as s:
        recs = {
            r.model_scope: r
            for r in (
                await s.execute(select(HealthRecord).where(HealthRecord.connection_id == cid))
            ).scalars()
        }
    model_rec, rec = recs["m"], recs[breaker.CONNECTION_SCOPE]
    assert model_rec.state == HealthState.HEALTHY.value and model_rec.consecutive_failures == 0
    assert (
        rec.state == HealthState.HEALTHY.value and rec.consecutive_failures == 0 and rec.cooldown_level == 0
    )
    assert rec.ewma_latency_ms is not None and 100 < rec.ewma_latency_ms < 200
    assert (await _avail(ctx, cid)).routable


async def test_retry_after_sets_cooldown_bounded(ctx: AppContext) -> None:
    cid = await _conn(ctx)
    now = utcnow()
    rec = await breaker.record_failure(
        ctx.db, ctx.settings, cid, "m", ErrorCategory.RATE_LIMITED, retry_after_s=30, now=now
    )
    assert rec is not None and rec.open_until is not None
    assert abs((rec.open_until - now).total_seconds() - 30) < 0.01
    rec = await breaker.record_failure(
        ctx.db, ctx.settings, cid, "m", ErrorCategory.RATE_LIMITED, retry_after_s=10**9, now=now
    )
    assert rec is not None and rec.open_until is not None
    assert (rec.open_until - now).total_seconds() <= ctx.settings.breaker_max_cooldown_s


async def test_model_scope_isolated_from_other_models(ctx: AppContext) -> None:
    cid = await _conn(ctx)
    await breaker.record_failure(ctx.db, ctx.settings, cid, "m1", ErrorCategory.MODEL_UNAVAILABLE)
    assert not (await _avail(ctx, cid, "m1")).routable
    assert (await _avail(ctx, cid, "m2")).routable


async def test_non_health_categories_ignored(ctx: AppContext) -> None:
    cid = await _conn(ctx)
    for cat in (
        ErrorCategory.REQUEST_UNSUPPORTED,
        ErrorCategory.POLICY_REJECTED,
        ErrorCategory.QUOTA_EXHAUSTED,
    ):
        assert await breaker.record_failure(ctx.db, ctx.settings, cid, "m", cat) is None
    assert (await _avail(ctx, cid)).reliability == breaker.reliability(None)


async def test_concurrent_probe_claims_single_winner(ctx: AppContext) -> None:
    cid = await _conn(ctx)
    await breaker.record_failure(
        ctx.db, ctx.settings, cid, "m", ErrorCategory.RATE_LIMITED, retry_after_s=0.01
    )
    await asyncio.sleep(0.02)
    results = await asyncio.gather(*(breaker.claim_probe(ctx.db, cid, "m") for _ in range(10)))
    assert sum(results) == 1


async def test_release_probe_allows_new_probe(ctx: AppContext) -> None:
    cid = await _conn(ctx)
    await breaker.record_failure(
        ctx.db, ctx.settings, cid, "m", ErrorCategory.RATE_LIMITED, retry_after_s=0.01
    )
    await asyncio.sleep(0.02)
    assert await breaker.claim_probe(ctx.db, cid, "m")
    await breaker.release_probe(ctx.db, cid, "m")
    assert await breaker.claim_probe(ctx.db, cid, "m")


async def test_rate_limit_cools_only_that_model(ctx: AppContext) -> None:
    cid = await _conn(ctx)
    await breaker.record_failure(ctx.db, ctx.settings, cid, "a", ErrorCategory.RATE_LIMITED, retry_after_s=30)
    assert not (await _avail(ctx, cid, "a")).routable
    assert (await _avail(ctx, cid, "b")).routable  # per-model limits leave other models usable


async def test_second_rate_limited_model_escalates_to_connection(ctx: AppContext) -> None:
    cid = await _conn(ctx)
    await breaker.record_failure(ctx.db, ctx.settings, cid, "a", ErrorCategory.RATE_LIMITED, retry_after_s=30)
    await breaker.record_failure(ctx.db, ctx.settings, cid, "b", ErrorCategory.RATE_LIMITED, retry_after_s=30)
    # Two models limited at once: the limit is account-wide, so a third model is cooled down as well.
    av = await _avail(ctx, cid, "c")
    assert not av.routable and av.reason == "circuit_open"
