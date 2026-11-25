"""Background worker: lease exclusivity, scheduling, quota reset recovery, outage/auth/model simulations."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from oasis_route.context import AppContext
from oasis_route.db.models import (
    Connection,
    IdempotencyRecord,
    Model,
    Notification,
    QuotaPool,
    WorkerInstance,
    WorkerLease,
    utcnow,
)
from oasis_route.quota import ledger
from oasis_route.quota.ledger import Demand, QuotaUnavailable
from oasis_route.worker import runner
from oasis_route.worker.runner import Job
from tests.factories import pool, project_with_provider, reload
from tests.helpers import auth, chat, gateway_setup


def _counter() -> tuple[list[int], Any]:
    calls: list[int] = []

    async def fn(_ctx: AppContext) -> Any:
        calls.append(1)
        await asyncio.sleep(0.01)
        return {"n": len(calls)}

    return calls, fn


async def test_lease_is_exclusive_across_workers(ctx: AppContext) -> None:
    calls, fn = _counter()
    job = Job("test.exclusive", 3600, fn)
    results = await asyncio.gather(*(runner.run_once(ctx, [job], holder=f"w{i}") for i in range(5)))
    assert len(calls) == 1
    assert sum(1 for r in results if "test.exclusive" in r) == 1


async def test_job_not_rerun_until_due_and_expired_lease_is_taken_over(ctx: AppContext) -> None:
    calls, fn = _counter()
    job = Job("test.due", 3600, fn, lease_s=60)
    await runner.run_once(ctx, [job], holder="a")
    await runner.run_once(ctx, [job], holder="b")
    assert len(calls) == 1  # not due yet
    async with ctx.db.transaction() as s:
        row = await s.get(WorkerLease, "test.due")
        assert row is not None and row.last_result is not None and '"ok": true' in row.last_result
        row.last_run_at = row.next_run_at = utcnow() - timedelta(hours=2)
        # Simulate a crashed holder: lease held by "a" but long expired.
        row.holder, row.lease_until = "a", utcnow() - timedelta(seconds=1)
    await runner.run_once(ctx, [job], holder="b")
    assert len(calls) == 2


async def test_live_lease_blocks_other_holder(ctx: AppContext) -> None:
    calls, fn = _counter()
    job = Job("test.live", 1, fn, lease_s=600)
    assert await runner.acquire(ctx, job, "a")  # "a" is mid-run
    await runner.run_once(ctx, [job], holder="b")
    assert calls == []


async def test_lease_of_a_stopped_or_silent_worker_is_taken_over(ctx: AppContext) -> None:
    """A restart must not wait out the old process's lease (and raise a false 'failing' alert)."""
    calls, fn = _counter()
    job = Job("test.abandoned", 1, fn, lease_s=900)
    await runner.heartbeat(ctx, "old", "embedded")
    assert await runner.acquire(ctx, job, "old")  # "old" was mid-run when it went away
    await runner.run_once(ctx, [job], holder="new")
    assert calls == []  # "old" is still heart-beating: its lease stands
    await runner.heartbeat(ctx, "old", "embedded", stopped=True)
    await runner.run_once(ctx, [job], holder="new")
    assert calls == [1]
    # A crash leaves no stop mark; a heartbeat older than the stall window counts as gone too.
    await runner.heartbeat(ctx, "crashed", "embedded")
    async with ctx.db.transaction() as s:
        row = await s.get(WorkerLease, "test.abandoned")
        assert row is not None
        row.holder, row.lease_until, row.next_run_at = "crashed", utcnow() + timedelta(seconds=900), None
        row.last_run_at = utcnow() - timedelta(hours=1)
        inst = await s.get(WorkerInstance, "crashed")
        assert inst is not None
        inst.heartbeat_at = utcnow() - timedelta(seconds=ctx.settings.worker_stall_after_s + 5)
    await runner.run_once(ctx, [job], holder="new")
    assert calls == [1, 1]


async def test_failing_job_is_recorded_and_does_not_stop_others(ctx: AppContext) -> None:
    calls, fn = _counter()

    async def boom(_ctx: AppContext) -> Any:
        raise RuntimeError("x")

    out = await runner.run_once(ctx, [Job("test.boom", 60, boom), Job("test.ok", 60, fn)], holder="a")
    assert out["test.boom"] == {"error": "RuntimeError"} and calls == [1]
    async with ctx.db.session() as s:
        row = await s.get(WorkerLease, "test.boom")
        assert row is not None and row.last_result is not None and '"ok": false' in row.last_result


async def test_default_jobs_run_cleanly(ctx: AppContext) -> None:
    out = await runner.run_once(ctx, holder="a")
    assert set(out) == {j.name for j in runner.default_jobs(ctx)}
    assert all("error" not in (v or {}) for v in out.values() if isinstance(v, dict)), out


async def test_quota_reset_recovery_admits_a_single_probe(ctx: AppContext) -> None:
    pid, prov = await project_with_provider(ctx.db)
    p = await pool(ctx.db, pid, prov, limit_value=100)
    now = utcnow()
    await ledger.mark_exhausted(ctx.db, [p.id], now + timedelta(seconds=30))
    p = await reload(ctx.db, p.id)
    assert p.needs_recheck

    def _reserve(key: str, at: Any) -> Any:
        return ledger.reserve(
            ctx.db,
            idempotency_key=key,
            request_id=key,
            project_id=pid,
            connection_id="c",
            model_id="m",
            pools=[p],
            demand=Demand(input_tokens=1, output_tokens=1),
            allow_unknown=False,
            ttl_s=60,
            now=at,
        )

    with pytest.raises(QuotaUnavailable) as ei:
        await _reserve("before", now)
    assert ei.value.reason == "exhausted"
    later = now + timedelta(seconds=31)
    probe = await _reserve("probe", later)  # reset elapsed: eligible for recheck, one probe at a time
    with pytest.raises(QuotaUnavailable) as ei:
        await _reserve("second", later)
    assert ei.value.reason == "awaiting_recheck"
    assert await ledger.settle(ctx.db, probe.id, None, now=later)
    await ledger.clear_recheck(ctx.db, [p.id], now=later)
    await _reserve("after-a", later)
    await _reserve("after-b", later)  # capacity confirmed: normal concurrency resumes


async def test_failed_probe_frees_the_slot(ctx: AppContext) -> None:
    pid, prov = await project_with_provider(ctx.db)
    p = await pool(ctx.db, pid, prov, limit_value=100)
    await ledger.mark_exhausted(ctx.db, [p.id], utcnow() - timedelta(seconds=1))
    p = await reload(ctx.db, p.id)
    kw: dict[str, Any] = dict(
        project_id=pid,
        connection_id="c",
        model_id="m",
        pools=[p],
        demand=Demand(input_tokens=1, output_tokens=1),
    )
    r = await ledger.reserve(
        ctx.db, idempotency_key="k1", request_id="k1", allow_unknown=False, ttl_s=60, **kw
    )
    await ledger.release(ctx.db, r.id)
    await ledger.reserve(ctx.db, idempotency_key="k2", request_id="k2", allow_unknown=False, ttl_s=60, **kw)


@pytest.mark.usefixtures("upstreams")
async def test_gateway_recovers_after_quota_reset(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client, two_providers=False)
    r = await client.post(
        f"/api/v1/projects/{g['pid']}/quota-pools",
        json={"provider_id": "mock", "unit": "requests", "period": "day", "limit_value": 100},
        headers=auth(g["key"]),
    )
    pool_id = r.json()["id"]
    upstreams["mock-a.test"].fail_next(
        "mock-large", 429, code="insufficient_quota", retry_after=3600, times=3
    )
    upstreams["mock-a.test"].fail_next(
        "mock-medium", 429, code="insufficient_quota", retry_after=3600, times=3
    )
    upstreams["mock-a.test"].fail_next(
        "mock-small", 429, code="insufficient_quota", retry_after=3600, times=3
    )
    r = await chat(client, g["key"])
    assert r.status_code in (429, 503)
    async with ctx.db.session() as s:
        exhausted = [
            p for p in (await s.execute(select(QuotaPool))).scalars() if p.exhausted_until is not None
        ]
    assert exhausted and all(p.needs_recheck for p in exhausted)
    # Time passes: the provider's reset arrives.
    async with ctx.db.transaction() as s:
        for p in (await s.execute(select(QuotaPool))).scalars():
            p.exhausted_until = utcnow() - timedelta(seconds=1)
    upstreams["mock-a.test"].failures.clear()
    r = await chat(client, g["key"])
    assert r.status_code == 200, r.text
    p = await reload(ctx.db, pool_id)
    assert not p.needs_recheck and p.exhausted_until is None


@pytest.mark.usefixtures("upstreams")
async def test_revalidation_detects_auth_expiry_and_notifies(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client, two_providers=False)
    async with ctx.db.transaction() as s:
        c = await s.get(Connection, g["conn_a"]["id"])
        assert c is not None
        c.last_validated_at = utcnow() - timedelta(days=2)
    upstreams["mock-a.test"].revoked = True
    out = await runner.run_once(
        ctx, [j for j in runner.default_jobs(ctx) if j.name == "connections.revalidate"]
    )
    assert out["connections.revalidate"] == {"checked": 1, "active": 0}
    async with ctx.db.session() as s:
        c = await s.get(Connection, g["conn_a"]["id"])
        assert c is not None and c.status == "suspended"
        n = (
            await s.execute(select(Notification).where(Notification.kind == "connection.suspended"))
        ).scalar_one()
        assert n.project_id == g["pid"]
    r = await chat(client, g["key"])
    assert r.status_code == 503  # no routable connection; never silently tries other accounts


@pytest.mark.usefixtures("upstreams")
async def test_model_removal_is_synced_and_routed_around(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client, two_providers=False)
    upstreams["mock-a.test"].extra_models = [{"id": "mock-tiny", "context_length": 4_000}]
    url = f"/api/v1/projects/{g['pid']}/connections/{g['conn_a']['id']}/validate"
    r = await client.post(url, headers=auth(g["key"]))
    assert r.status_code == 200 and r.json()["models_added"] == 1, r.text
    async with ctx.db.session() as s:
        tiny = (
            await s.execute(
                select(Model).where(Model.provider_id == "mock", Model.upstream_id == "mock-tiny")
            )
        ).scalar_one()
        assert tiny.lifecycle == "active"  # discovered from the listing at connect time
    upstreams["mock-a.test"].hidden_models = {"mock-tiny"}
    r = await client.post(url, headers=auth(g["key"]))
    assert r.status_code == 200, r.text
    async with ctx.db.session() as s:
        tiny = (
            await s.execute(
                select(Model).where(Model.provider_id == "mock", Model.upstream_id == "mock-tiny")
            )
        ).scalar_one()
        assert tiny.lifecycle == "removed"
        curated = (
            await s.execute(
                select(Model).where(Model.provider_id == "mock", Model.upstream_id == "mock-large")
            )
        ).scalar_one()
        assert curated.lifecycle == "active"
        assert (
            await s.execute(select(Notification).where(Notification.kind == "models.removed"))
        ).scalar_one()
    # A curated model removed upstream is routed around via model_not_found failover.
    upstreams["mock-a.test"].hidden_models = {"mock-large"}
    r = await chat(client, g["key"])
    assert r.status_code == 200 and r.headers["x-oasis-route-model"] == "mock-medium"


@pytest.mark.usefixtures("upstreams")
async def test_simulated_outage_fails_over_then_recovers(
    client: httpx.AsyncClient, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client)
    for m in ("mock-large", "mock-medium", "mock-small"):
        upstreams["mock-a.test"].fail_next(m, 503, times=20)
    r = await chat(client, g["key"])
    assert r.status_code == 200 and r.headers["x-oasis-route-provider"] == "mock_alt"
    upstreams["mock-a.test"].failures.clear()
    r = await chat(client, g["key"], model="mock_alt/mock-medium")
    assert r.status_code == 200


async def test_retention_job_removes_expired_records(ctx: AppContext) -> None:
    pid, _ = await project_with_provider(ctx.db)
    async with ctx.db.transaction() as s:
        s.add(Notification(project_id=pid, kind="k", title="old", delete_after=utcnow() - timedelta(days=1)))
        s.add(Notification(project_id=pid, kind="k", title="new", delete_after=utcnow() + timedelta(days=1)))
        s.add(
            IdempotencyRecord(
                project_id=pid, key="x", request_hash="h", created_at=utcnow() - timedelta(days=2)
            )
        )
        s.add(IdempotencyRecord(project_id=pid, key="y", request_hash="h"))
    out = await runner.run_once(ctx, [j for j in runner.default_jobs(ctx) if j.name == "retention.records"])
    assert out["retention.records"] == {"notifications": 1, "idempotency": 1, "oauth_flows_expired": 0}
    async with ctx.db.session() as s:
        assert [n.title for n in (await s.execute(select(Notification))).scalars()] == ["new"]


async def test_record_retention_is_opt_in_and_prunes_only_old_rows(ctx: AppContext) -> None:
    from oasis_route.db.models import AuditEvent, RouteDecision, UsageRecord

    pid, _ = await project_with_provider(ctx.db)
    old = utcnow() - timedelta(days=40)
    async with ctx.db.transaction() as s:
        for i, ts in enumerate((old, utcnow())):
            s.add(UsageRecord(request_id=f"r{i}", project_id=pid, created_at=ts))
            s.add(
                RouteDecision(
                    request_id=f"r{i}", project_id=pid, requested_model="auto", mode="x", created_at=ts
                )
            )
            s.add(AuditEvent(action=f"a{i}", project_id=pid, created_at=ts))
    job = [j for j in runner.default_jobs(ctx) if j.name == "retention.records"]

    # Default: keep everything.
    out = await runner.run_once(ctx, job)
    assert "usage_records" not in out["retention.records"]

    ctx.settings.usage_retention_days = 30
    ctx.settings.route_decision_retention_days = 30
    ctx.settings.audit_retention_days = 30
    r = await runner._retention(ctx)
    assert r["usage_records"] == 1 and r["route_decisions"] == 1 and r["audit_events"] >= 1
    async with ctx.db.session() as s:
        assert [u.request_id for u in (await s.execute(select(UsageRecord))).scalars()] == ["r1"]
        assert [d.request_id for d in (await s.execute(select(RouteDecision))).scalars()] == ["r1"]
        assert "a1" in {a.action for a in (await s.execute(select(AuditEvent))).scalars()}
