"""Zero-operations acceptance (docs/ZERO_OPERATIONS_AUTONOMY.md).

Supervised embedded worker, lease renewal, bounded execution, failure backoff, pause controls, stall
detection and alerting, plain-language status, and the one-credential onboarding flow end to end.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from oasis_route.config import Settings
from oasis_route.context import AppContext
from oasis_route.db.models import Connection, Notification, WorkerInstance, WorkerLease, utcnow
from oasis_route.db.session import Database
from oasis_route.worker import runner
from oasis_route.worker.runner import Job
from tests.helpers import auth, bootstrap, chat, connect_mock, create_user


async def _lease(ctx: AppContext, name: str) -> WorkerLease:
    async with ctx.db.session() as s:
        row = await s.get(WorkerLease, name)
        assert row is not None
        return row


async def test_lease_is_renewed_while_a_long_job_runs(ctx: AppContext) -> None:
    started = asyncio.Event()

    async def slow(_c: AppContext) -> Any:
        started.set()
        await asyncio.sleep(0.8)
        return "done"

    job = Job("test.slow", 3600, slow, lease_s=0.3)
    task = asyncio.create_task(runner.run_once(ctx, [job], holder="a"))
    await started.wait()
    await asyncio.sleep(0.5)  # longer than lease_s: without renewal the lease would have expired
    row = await _lease(ctx, "test.slow")
    assert row.holder == "a" and row.lease_until > utcnow()
    async with ctx.db.transaction() as s:  # make it "due" so only the live lease protects it
        r = await s.get(WorkerLease, "test.slow")
        assert r is not None
        r.next_run_at = utcnow() - timedelta(seconds=1)
    assert await runner.acquire(ctx, job, "b") is False
    assert (await task)["test.slow"] == "done"


async def test_hung_job_is_bounded_and_backs_off(ctx: AppContext) -> None:
    async def hang(_c: AppContext) -> Any:
        await asyncio.sleep(30)

    job = Job("test.hang", 60, hang, lease_s=10, timeout_s=0.2)
    out = await runner.run_once(ctx, [job], holder="a")
    assert out["test.hang"] == {"error": "timeout"}
    row = await _lease(ctx, "test.hang")
    assert row.consecutive_failures == 1 and row.last_error == "timeout"
    assert row.next_run_at is not None and row.next_run_at > utcnow() + timedelta(seconds=10)
    assert row.lease_until <= utcnow()  # released, not left held


async def test_failures_back_off_exponentially_then_reset_on_success(ctx: AppContext) -> None:
    fail = {"on": True}

    async def flaky(_c: AppContext) -> Any:
        if fail["on"]:
            raise RuntimeError("boom")
        return "ok"

    job = Job("test.flaky", 5, flaky, lease_s=10)
    delays = []
    for _ in range(3):
        async with ctx.db.transaction() as s:
            r = await s.get(WorkerLease, job.name)
            if r is not None:
                r.next_run_at = utcnow() - timedelta(seconds=1)
        before = utcnow()
        await runner.run_once(ctx, [job], holder="a")
        row = await _lease(ctx, job.name)
        assert row.next_run_at is not None
        delays.append((row.next_run_at - before).total_seconds())
    assert row.consecutive_failures == 3 and row.last_error == "RuntimeError"
    assert delays[2] > 5  # backoff exceeds the normal interval
    assert all(15 <= d <= 120 for d in delays)
    fail["on"] = False
    async with ctx.db.transaction() as s:
        r = await s.get(WorkerLease, job.name)
        assert r is not None
        r.next_run_at = utcnow() - timedelta(seconds=1)
    await runner.run_once(ctx, [job], holder="a")
    row = await _lease(ctx, job.name)
    assert row.consecutive_failures == 0 and row.last_success_at is not None and row.last_error is None
    assert row.run_count == 4 and row.last_duration_ms is not None


def test_backoff_is_capped() -> None:
    assert runner.backoff_s(1, 3600) <= 30
    assert runner.backoff_s(30, 600) <= 600


async def test_supervisor_restarts_a_crashed_worker(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    stop = asyncio.Event()

    async def fake_run_forever(*_a: Any, **_k: Any) -> None:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("worker crashed")
        stop.set()

    monkeypatch.setattr(runner, "run_forever", fake_run_forever)
    restarts = await asyncio.wait_for(runner.supervise(ctx, stop, restart_delay_s=0.001), timeout=5)
    assert restarts == 2 and calls["n"] == 3


async def test_run_forever_heartbeats_runs_jobs_and_stops_cleanly(ctx: AppContext) -> None:
    calls: list[int] = []

    async def fn(_c: AppContext) -> Any:
        calls.append(1)

    stop = asyncio.Event()
    task = asyncio.create_task(runner.run_forever(ctx, stop, jobs=[Job("test.tick", 3600, fn)], tick_s=0.05))
    for _ in range(100):
        if calls:
            break
        await asyncio.sleep(0.02)
    ctx.settings.worker_mode = "embedded"
    assert (await runner.worker_status(ctx))["state"] in ("running", "degraded")
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert calls == [1]
    async with ctx.db.session() as s:
        inst = list((await s.execute(select(WorkerInstance))).scalars())
    assert len(inst) == 1 and inst[0].stopped_at is not None
    assert (await runner.worker_status(ctx))["state"] == "stalled"  # stopped worker is not live


async def test_watchdog_alerts_once_when_worker_is_missing(ctx: AppContext) -> None:
    ctx.settings.worker_mode = "embedded"
    assert await runner.watchdog(ctx) == "stalled"
    assert await runner.watchdog(ctx) == "stalled"
    async with ctx.db.session() as s:
        notes = list(
            (await s.execute(select(Notification).where(Notification.kind == "system.worker"))).scalars()
        )
    assert len(notes) == 1 and notes[0].severity == "critical"
    await runner.heartbeat(ctx, "w1", "embedded")
    assert await runner.watchdog(ctx) == "running"


async def test_watchdog_reports_repeatedly_failing_job(ctx: AppContext) -> None:
    ctx.settings.worker_mode = "embedded"
    await runner.heartbeat(ctx, "w1", "embedded")
    async with ctx.db.transaction() as s:
        s.add(
            WorkerLease(
                name="discovery.sources",
                holder="w1",
                lease_until=utcnow(),
                consecutive_failures=4,
                last_error="ConnectError",
                next_run_at=utcnow() + timedelta(minutes=5),
            )
        )
    assert await runner.watchdog(ctx) == "degraded"
    async with ctx.db.session() as s:
        n = (await s.execute(select(Notification).where(Notification.kind == "system.worker"))).scalar_one()
    assert "discovery.sources" in n.body and n.severity == "warning"


async def test_embedded_worker_starts_with_the_app(settings: Settings) -> None:
    from oasis_route.app import create_app
    from oasis_route.registry.catalog import sync_catalog

    s = settings.model_copy(update={"worker_mode": "embedded", "worker_tick_s": 0.05})
    c = AppContext(settings=s, db=Database(s.database_url))
    await sync_catalog(c.db, s)
    app = create_app(s, c)
    async with app.router.lifespan_context(app):
        for _ in range(200):
            async with c.db.session() as sess:
                lease = await sess.get(WorkerLease, "ledger.expire_reservations")
                inst = (await sess.execute(select(WorkerInstance))).scalars().first()
            if lease is not None and lease.last_success_at is not None and inst is not None:
                break
            await asyncio.sleep(0.02)
        assert inst is not None and inst.mode == "embedded"
        assert lease is not None and lease.last_success_at is not None
        st = await runner.worker_status(c)
        assert st["state"] in ("running", "degraded") and st["workers"]


async def test_admin_job_controls(client: httpx.AsyncClient, ctx: AppContext) -> None:
    admin = (await bootstrap(client))["api_key"]["secret"]
    r = await client.get("/api/v1/admin/jobs", headers=auth(admin))
    assert r.status_code == 200
    names = {j["name"] for j in r.json()["jobs"]}
    assert {"quota.refresh", "providers.health_probe", "connections.revalidate", "discovery.sources"} <= names
    r = await client.post("/api/v1/admin/jobs/quota.refresh/pause", headers=auth(admin))
    assert r.status_code == 200 and r.json()["paused"] is True
    jobs = [j for j in runner.default_jobs(ctx) if j.name == "quota.refresh"]
    assert await runner.run_once(ctx, jobs) == {}
    r = await client.post("/api/v1/admin/jobs/quota.refresh/resume", headers=auth(admin))
    assert r.json()["paused"] is False
    assert "quota.refresh" in await runner.run_once(ctx, jobs)
    r = await client.post("/api/v1/admin/jobs/quota.refresh/run", headers=auth(admin))
    assert r.status_code == 200
    assert "quota.refresh" in await runner.run_once(ctx, jobs)  # forced to run again immediately
    assert (await client.post("/api/v1/admin/jobs/nope/run", headers=auth(admin))).status_code == 404
    user = await create_user(client, admin, "u@example.com", declared_age=30, country="BD")
    r = await client.get("/api/v1/admin/jobs", headers=auth(user["api_key"]["secret"]))
    assert r.status_code == 403


async def test_one_credential_onboarding_end_to_end(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    """Fresh install, one provider key, Ready, request served, credential expires, Action required."""
    admin = (await bootstrap(client))["api_key"]["secret"]
    u = await create_user(client, admin, "dev@example.com", declared_age=30, country="BD")
    key, pid = u["api_key"]["secret"], u["project"]["id"]

    st = (await client.get(f"/api/v1/projects/{pid}/status", headers=auth(key))).json()
    assert st["status"] == "Not connected" and st["setup"]["complete"] is False
    assert st["setup"]["oasis_key_issued"] is True

    # The only thing the user does: paste one provider key. No pools, routes or jobs to configure.
    await connect_mock(client, key, pid)
    st = (await client.get(f"/api/v1/projects/{pid}/status", headers=auth(key))).json()
    assert st["status"] == "Ready", st
    assert st["setup"] == {
        "credential_validated": True,
        "models_synced": True,
        "routing_ready": True,
        "oasis_key_issued": True,
        "complete": True,
    }
    assert st["connections"][0]["status"] == "Connected"
    assert st["serving"]["next_request"]["provider"] == "mock"
    for word in ("lease", "breaker", "ledger", "reservation", "adapter"):
        assert word not in str(st).lower()

    r = await chat(client, key)
    assert r.status_code == 200
    st = (await client.get(f"/api/v1/projects/{pid}/status", headers=auth(key))).json()
    assert st["serving"]["last_request"]["provider"] == "mock"

    # The upstream credential expires; background revalidation (not the user) notices it.
    upstreams["mock-a.test"].revoked = True
    async with ctx.db.transaction() as s:
        for c in (await s.execute(select(Connection))).scalars():
            c.last_validated_at = utcnow() - timedelta(days=2)
    jobs = [j for j in runner.default_jobs(ctx) if j.name == "connections.revalidate"]
    out = await runner.run_once(ctx, jobs)
    assert out["connections.revalidate"]["checked"] == 1
    st = (await client.get(f"/api/v1/projects/{pid}/status", headers=auth(key))).json()
    assert st["status"] == "Action required"
    assert st["connections"][0]["status"] == "Action required"
    assert st["attention"], "the user is told what needs their attention"
    assert "mock-key" not in str(st)

    # No eligible route: the gateway refuses cleanly rather than guessing.
    r = await chat(client, key)
    assert r.status_code in (409, 429, 503)


async def test_pause_automation_skips_background_checks(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    admin = (await bootstrap(client))["api_key"]["secret"]
    u = await create_user(client, admin, "dev@example.com", declared_age=30, country="BD")
    key, pid = u["api_key"]["secret"], u["project"]["id"]
    await connect_mock(client, key, pid)
    r = await client.put(f"/api/v1/projects/{pid}/automation", json={"paused": True}, headers=auth(key))
    assert r.status_code == 200 and r.json() == {"paused": True}
    async with ctx.db.transaction() as s:
        for c in (await s.execute(select(Connection))).scalars():
            c.last_validated_at = utcnow() - timedelta(days=2)
    jobs = [j for j in runner.default_jobs(ctx) if j.name == "connections.revalidate"]
    assert (await runner.run_once(ctx, jobs))["connections.revalidate"]["checked"] == 0
    st = (await client.get(f"/api/v1/projects/{pid}/status", headers=auth(key))).json()
    assert st["automation"]["paused"] is True
    assert (await chat(client, key)).status_code == 200  # routing is unaffected by the pause


async def test_health_probe_rechecks_unhealthy_connections(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    from oasis_route.db.models import HealthRecord

    admin = (await bootstrap(client))["api_key"]["secret"]
    u = await create_user(client, admin, "dev@example.com", declared_age=30, country="BD")
    conn = await connect_mock(client, u["api_key"]["secret"], u["project"]["id"])
    async with ctx.db.transaction() as s:
        s.add(HealthRecord(connection_id=conn["id"], model_scope="*", state="open", open_until=utcnow()))
        c = await s.get(Connection, conn["id"])
        assert c is not None
        c.last_validated_at = utcnow() - timedelta(hours=1)
    upstreams["mock-a.test"].revoked = True
    jobs = [j for j in runner.default_jobs(ctx) if j.name == "providers.health_probe"]
    assert (await runner.run_once(ctx, jobs))["providers.health_probe"] == {"probed": 1}
    async with ctx.db.session() as s:
        c = await s.get(Connection, conn["id"])
        assert c is not None and c.status == "suspended"


async def test_readyz_reports_worker_state_without_failing(client: httpx.AsyncClient) -> None:
    r = await client.get("/readyz")
    assert r.status_code == 200 and r.json()["worker"] == "disabled"


async def test_console_is_served_with_strict_csp(client: httpx.AsyncClient) -> None:
    r = await client.get("/console/")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert r.headers["x-frame-options"] == "DENY"
    html = r.text
    assert '<script src="app.js"' in html and "<script>" not in html and "onclick=" not in html
    js = (await client.get("/console/app.js")).text
    assert "innerHTML" not in js and "eval(" not in js
    assert "sessionStorage" in js and "localStorage" not in js
