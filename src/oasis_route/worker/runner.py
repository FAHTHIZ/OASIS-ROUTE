"""Lease-coordinated, supervised background worker.

Every job is guarded by a row in ``worker_leases``: a job runs only when it is due (``next_run_at``) and its
lease is free or expired, so any number of worker processes or API replicas can run safely. While a job runs
its lease is renewed by a heartbeat, and execution is bounded by ``Job.timeout_s``, so a live job is never
reclaimed and a hung one cannot hold its lease forever. Failures back off exponentially with jitter. Each
worker loop records a heartbeat in ``worker_instances`` so a stalled or missing worker is detected and
surfaced (see :func:`worker_status` and :func:`watchdog`). By default the API process supervises the worker
itself (``worker_mode=embedded``), restarting it after a crash; no manual command is required.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from oasis_route import __version__
from oasis_route.context import AppContext
from oasis_route.db.models import (
    AuditEvent,
    Connection,
    HealthRecord,
    IdempotencyRecord,
    Notification,
    Project,
    QuotaPool,
    RouteDecision,
    UsageRecord,
    WorkerInstance,
    WorkerLease,
    utcnow,
)
from oasis_route.domain.enums import ConnectionStatus

log = logging.getLogger(__name__)
JobFn = Callable[[AppContext], Awaitable[Any]]


@dataclass(frozen=True)
class Job:
    name: str
    interval_s: float
    fn: JobFn
    lease_s: float = 300.0
    timeout_s: float | None = None  # bounded execution; defaults to 4 x lease_s
    description: str = ""

    @property
    def max_runtime_s(self) -> float:
        return self.timeout_s if self.timeout_s is not None else self.lease_s * 4


async def _expire_reservations(ctx: AppContext) -> Any:
    from oasis_route.quota import ledger

    return {"expired": await ledger.expire_stale(ctx.db)}


async def _discovery(ctx: AppContext) -> Any:
    from oasis_route.discovery import service

    res = await service.run_due_sources(ctx)
    return {
        "sources": len(res),
        "changed": sum(1 for r in res if r.changed),
        "offers": len({o for r in res for o in r.offers}),
        "new_sources": sum(r.discovered_sources for r in res),
        "provider_candidates": sum(r.provider_candidates for r in res),
        "errors": sorted({r.status for r in res if r.status.startswith(("http_", "network", "disallowed"))}),
    }


async def _seed_sources(ctx: AppContext) -> Any:
    """Add built-in official sources that are missing (e.g. after an upgrade adds new seeds)."""
    if not ctx.settings.discovery_seed_sources:
        return {"skipped": "disabled"}
    from oasis_route.discovery.seeds import sync_seed_sources

    return {"inserted": await sync_seed_sources(ctx.db)}


async def _offer_lifecycle(ctx: AppContext) -> Any:
    from oasis_route.discovery import service

    return await service.expire_and_stale_offers(ctx)


async def _evidence_retention(ctx: AppContext) -> Any:
    from oasis_route.discovery import service

    return {"removed": await service.prune_evidence(ctx)}


async def _revalidate_connections(ctx: AppContext) -> Any:
    """Detect expired/revoked upstream credentials early with a safe, non-billable validation call."""
    from oasis_route.connections import service as conns

    cutoff = utcnow() - timedelta(hours=24)
    async with ctx.db.session() as s:
        ids = list(
            (
                await s.execute(
                    select(Connection.id)
                    .join(Project, Project.id == Connection.project_id)
                    .where(
                        Project.automation_paused.is_(False),
                        Connection.status == ConnectionStatus.ACTIVE.value,
                        (Connection.last_validated_at.is_(None)) | (Connection.last_validated_at < cutoff),
                    )
                    .limit(20)
                )
            ).scalars()
        )
    ok = 0
    for cid in ids:
        try:
            r = await conns.validate(ctx, cid)
            ok += 1 if r.status == ConnectionStatus.ACTIVE.value else 0
        except Exception:  # pragma: no cover - keep going
            log.exception("revalidation failed", extra={"connection_id": cid})
    return {"checked": len(ids), "active": ok}


async def _retention(ctx: AppContext) -> Any:
    now = utcnow()
    async with ctx.db.transaction() as s:
        n = await s.execute(delete(Notification).where(Notification.delete_after < now))
        i = await s.execute(
            delete(IdempotencyRecord).where(IdempotencyRecord.created_at < now - timedelta(days=1))
        )
        aged: dict[str, int] = {}
        st = ctx.settings
        for name, model, days in (
            ("usage_records", UsageRecord, st.usage_retention_days),
            ("route_decisions", RouteDecision, st.route_decision_retention_days),
            ("audit_events", AuditEvent, st.audit_retention_days),
        ):
            if days > 0:
                r = await s.execute(delete(model).where(model.created_at < now - timedelta(days=days)))
                aged[name] = int(getattr(r, "rowcount", 0) or 0)
    from oasis_route.connections import oauth

    return {
        "notifications": getattr(n, "rowcount", 0),
        "idempotency": getattr(i, "rowcount", 0),
        **aged,
        "oauth_flows_expired": await oauth.purge_expired(ctx),
    }


async def _oauth_refresh(ctx: AppContext) -> Any:
    from oasis_route.connections import oauth

    return await oauth.refresh_tokens(ctx)


async def _mail_sync(ctx: AppContext) -> Any:
    from oasis_route.notifications import mail

    return await mail.sync_all(ctx)


async def _health_probe(ctx: AppContext) -> Any:
    """Probe connections whose circuit breaker is open with a non-billable validation call.

    This detects revoked credentials and model removals during an outage without spending quota; traffic
    itself still decides when the breaker closes (half-open probe), so routing stays authoritative.
    """
    from oasis_route.connections import service as conns

    now = utcnow()
    cutoff = now - timedelta(minutes=15)
    async with ctx.db.session() as s:
        ids = list(
            (
                await s.execute(
                    select(Connection.id)
                    .join(HealthRecord, HealthRecord.connection_id == Connection.id)
                    .join(Project, Project.id == Connection.project_id)
                    .where(
                        Project.automation_paused.is_(False),
                        Connection.status == ConnectionStatus.ACTIVE.value,
                        HealthRecord.state != "healthy",
                        (Connection.last_validated_at.is_(None)) | (Connection.last_validated_at < cutoff),
                    )
                    .distinct()
                    .limit(20)
                )
            ).scalars()
        )
    for cid in ids:
        try:
            await conns.validate(ctx, cid)
        except Exception:  # pragma: no cover
            log.exception("health probe failed", extra={"connection_id": cid})
    return {"probed": len(ids)}


async def rewrap_credentials(ctx: AppContext, batch: int = 200) -> dict[str, int]:
    """Finish a KEK rotation: re-wrap every DEK still under a previous master key with the current one.

    Ciphertext is untouched and secrets are never decrypted here (only the 32-byte DEK is unwrapped), so this
    is cheap and safe to run repeatedly. Once ``remaining`` is 0 the old key can be removed from config.
    """
    from oasis_route.db.models import Credential
    from oasis_route.security.vault import SealedSecret, VaultError

    current = ctx.vault_.keys.current_kek_id
    done = failed = 0
    async with ctx.db.transaction() as s:
        rows = list(
            (await s.execute(select(Credential).where(Credential.kek_id != current).limit(batch))).scalars()
        )
        for c in rows:
            try:
                new = ctx.vault_.rewrap(SealedSecret(c.kek_id, c.wrapped_dek, c.ciphertext, c.aad))
            except VaultError:
                failed += 1  # KEK not configured; reported, never fatal
                continue
            c.kek_id, c.wrapped_dek = new.kek_id, new.wrapped_dek
            done += 1
    async with ctx.db.session() as s:
        from sqlalchemy import func

        remaining = int(
            (
                await s.execute(
                    select(func.count()).select_from(Credential).where(Credential.kek_id != current)
                )
            ).scalar_one()
        )
    return {"rewrapped": done, "failed": failed, "remaining": remaining}


async def _quota_refresh(ctx: AppContext) -> Any:
    """Advance quota windows whose reset time has passed so capacity reports stay current."""
    from oasis_route.quota import ledger

    now = utcnow()
    async with ctx.db.transaction() as s:
        pools = list(
            (
                await s.execute(
                    select(QuotaPool)
                    .join(Project, Project.id == QuotaPool.project_id)
                    .where(Project.automation_paused.is_(False))
                )
            ).scalars()
        )
        before = {p.id: p.window_start for p in pools}
        await ledger.roll_windows(s, pools, now)
    return {"pools": len(pools), "rolled": sum(1 for p in pools if p.window_start != before[p.id])}


def default_jobs(ctx: AppContext) -> list[Job]:
    s = ctx.settings
    return [
        Job(
            "ledger.expire_reservations",
            30,
            _expire_reservations,
            lease_s=120,
            description="Release reservations abandoned by crashed requests",
        ),
        Job("quota.refresh", 300, _quota_refresh, lease_s=120, description="Advance quota reset windows"),
        Job(
            "connections.oauth_refresh",
            600,
            _oauth_refresh,
            lease_s=300,
            description="Refresh expiring OAuth sign-ins with the provider's refresh_token grant",
        ),
        Job(
            "providers.health_probe",
            300,
            _health_probe,
            lease_s=600,
            description="Check unhealthy provider connections",
        ),
        Job(
            "discovery.sources",
            s.worker_interval_s,
            _discovery,
            lease_s=900,
            description="Check approved discovery sources for offer changes",
        ),
        Job(
            "discovery.seed_sources",
            86_400,
            _seed_sources,
            description="Register missing built-in official provider sources",
        ),
        Job("offers.lifecycle", 3600, _offer_lifecycle, description="Expire and mark stale offers"),
        Job(
            "connections.revalidate",
            3600,
            _revalidate_connections,
            lease_s=600,
            description="Re-check provider credentials",
        ),
        Job(
            "vault.rewrap",
            3600,
            rewrap_credentials,
            lease_s=600,
            description="Re-wrap credentials after a master key rotation",
        ),
        Job("retention.evidence", 86_400, _evidence_retention, description="Delete expired evidence"),
        Job("retention.records", 3600, _retention, description="Delete expired notifications and records"),
        Job("mail.gmail_sync", 900, _mail_sync, lease_s=600, description="Optional Gmail provider notices"),
    ]


def holder_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{os.urandom(3).hex()}"


def backoff_s(failures: int, max_backoff_s: float) -> float:
    """Exponential backoff with jitter: ~30s, 60s, 120s ... capped at ``max_backoff_s``."""
    base = min(max_backoff_s, 30.0 * float(2 ** max(0, failures - 1)))
    return float(base * (0.5 + random.random() / 2))


async def acquire(ctx: AppContext, job: Job, holder: str, now: datetime | None = None) -> bool:
    """Take the job's lease iff it is due, not paused, and not held by a live worker."""
    now = now or utcnow()
    try:
        async with ctx.db.transaction() as s:
            row = (
                await s.execute(select(WorkerLease).where(WorkerLease.name == job.name).with_for_update())
            ).scalar_one_or_none()
            if row is None:
                s.add(
                    WorkerLease(
                        name=job.name,
                        holder=holder,
                        lease_until=now + timedelta(seconds=job.lease_s),
                        last_started_at=now,
                    )
                )
                return True
            if row.paused:
                return False
            if row.lease_until > now and row.holder != holder:
                # A lease left by a worker that stopped or stopped heart-beating (a restart, a
                # crash) is abandoned: take it rather than wait up to lease_s for it to expire.
                owner = await s.get(WorkerInstance, row.holder)
                stall = timedelta(seconds=ctx.settings.worker_stall_after_s)
                if owner is None or (owner.stopped_at is None and owner.heartbeat_at >= now - stall):
                    return False
            due = row.next_run_at
            if due is None and row.last_run_at is not None:
                due = row.last_run_at + timedelta(seconds=job.interval_s)
            if due is not None and due > now:
                return False
            row.holder = holder
            row.lease_until = now + timedelta(seconds=job.lease_s)
            row.last_started_at = now
            return True
    except IntegrityError:
        return False  # another worker inserted the lease first


async def renew(ctx: AppContext, job: Job, holder: str) -> bool:
    """Extend a held lease. Returns False if the lease was lost (another holder took it)."""
    now = utcnow()
    async with ctx.db.transaction() as s:
        row = await s.get(WorkerLease, job.name)
        if row is None or row.holder != holder:
            return False
        row.lease_until = now + timedelta(seconds=job.lease_s)
        return True


async def finish(
    ctx: AppContext,
    job: Job,
    holder: str,
    result: str,
    *,
    ok: bool = True,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    now = utcnow()
    async with ctx.db.transaction() as s:
        row = await s.get(WorkerLease, job.name)
        if row is None or row.holder != holder:
            return
        row.last_run_at = now
        row.last_result = result[:4000]
        row.lease_until = now  # release
        row.run_count = (row.run_count or 0) + 1
        row.last_duration_ms = duration_ms
        if ok:
            row.consecutive_failures = 0
            row.last_success_at = now
            row.last_error = None
            row.next_run_at = now + timedelta(seconds=job.interval_s)
        else:
            row.consecutive_failures = (row.consecutive_failures or 0) + 1
            row.last_error = (error or "error")[:300]
            delay = backoff_s(row.consecutive_failures, ctx.settings.worker_max_backoff_s)
            row.next_run_at = now + timedelta(seconds=delay)


async def _run_job(ctx: AppContext, job: Job, holder: str) -> Any:
    """Run one job with a lease heartbeat and bounded execution."""
    stop = asyncio.Event()

    async def heartbeat() -> None:
        interval = max(0.05, job.lease_s / 3)
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
            if not stop.is_set():
                try:
                    await renew(ctx, job, holder)
                except Exception:  # pragma: no cover - transient DB error; lease_s covers one miss
                    log.warning("lease renewal failed", extra={"job": job.name})

    hb = asyncio.create_task(heartbeat())
    try:
        return await asyncio.wait_for(job.fn(ctx), timeout=job.max_runtime_s)
    finally:
        stop.set()
        await hb


async def run_once(
    ctx: AppContext, jobs: list[Job] | None = None, holder: str | None = None
) -> dict[str, Any]:
    holder = holder or holder_id()
    ran: dict[str, Any] = {}
    for job in jobs or default_jobs(ctx):
        if not await acquire(ctx, job, holder):
            continue
        t0 = time.perf_counter()
        ok, err = True, None
        try:
            res = await _run_job(ctx, job, holder)
            text = json.dumps({"ok": True, "result": res}, default=str)
            ran[job.name] = res
        except Exception as exc:
            ok, err = False, "timeout" if isinstance(exc, TimeoutError) else type(exc).__name__
            log.exception("job failed", extra={"job": job.name})
            text = json.dumps({"ok": False, "error": err})
            ran[job.name] = {"error": err}
        ms = int((time.perf_counter() - t0) * 1000)
        ctx.metrics.worker_jobs.labels(job.name, "success" if ok else "failure").inc()
        await finish(ctx, job, holder, text, ok=ok, error=err, duration_ms=ms)
    return ran


async def heartbeat(ctx: AppContext, holder: str, mode: str, *, stopped: bool = False) -> None:
    now = utcnow()
    async with ctx.db.transaction() as s:
        row = await s.get(WorkerInstance, holder)
        if row is None:
            s.add(
                WorkerInstance(
                    holder=holder, mode=mode, version=__version__, started_at=now, heartbeat_at=now
                )
            )
        else:
            row.heartbeat_at = now
            row.stopped_at = now if stopped else None
        # Forget instances that have been gone for a day.
        await s.execute(delete(WorkerInstance).where(WorkerInstance.heartbeat_at < now - timedelta(days=1)))


async def run_forever(
    ctx: AppContext,
    stop: asyncio.Event | None = None,
    *,
    mode: str = "standalone",
    jobs: list[Job] | None = None,
    tick_s: float | None = None,
) -> None:
    stop = stop or asyncio.Event()
    holder = holder_id()
    tick = tick_s if tick_s is not None else ctx.settings.worker_tick_s
    log.info("worker started", extra={"holder": holder, "mode": mode})
    try:
        while not stop.is_set():
            try:
                await heartbeat(ctx, holder, mode)
                await run_once(ctx, jobs, holder=holder)
            except Exception:  # database blips must not kill the worker
                log.exception("worker tick failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=tick)
    finally:
        with contextlib.suppress(Exception):
            await heartbeat(ctx, holder, mode, stopped=True)
        log.info("worker stopped", extra={"holder": holder})


async def supervise(
    ctx: AppContext,
    stop: asyncio.Event,
    *,
    mode: str = "embedded",
    jobs: list[Job] | None = None,
    tick_s: float | None = None,
    restart_delay_s: float = 1.0,
) -> int:
    """Keep the worker loop running until ``stop``; restart it with backoff if it ever crashes.

    Returns the number of restarts (useful for tests).
    """
    restarts = 0
    while not stop.is_set():
        try:
            await run_forever(ctx, stop, mode=mode, jobs=jobs, tick_s=tick_s)
        except asyncio.CancelledError:
            raise
        except BaseException:
            restarts += 1
            ctx.metrics.worker_restarts.inc()
            log.exception("worker crashed; restarting", extra={"restarts": restarts})
            delay = min(60.0, restart_delay_s * (2 ** min(restarts, 6))) * (0.5 + random.random() / 2)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
    return restarts


# ------------------------------------------------------------------------------------------- status


async def worker_status(ctx: AppContext, now: datetime | None = None) -> dict[str, Any]:
    """Operational view of background automation: live workers and per-job schedule/result."""
    now = now or utcnow()
    stall = timedelta(seconds=ctx.settings.worker_stall_after_s)
    known = {j.name: j for j in default_jobs(ctx)}
    async with ctx.db.session() as s:
        instances = list((await s.execute(select(WorkerInstance))).scalars())
        leases = {r.name: r for r in (await s.execute(select(WorkerLease))).scalars()}
    live = [i for i in instances if i.stopped_at is None and i.heartbeat_at >= now - stall]
    jobs: list[dict[str, Any]] = []
    for name in sorted(set(known) | set(leases)):
        r, j = leases.get(name), known.get(name)
        overdue = False
        if r is not None and j is not None and not r.paused:
            due = r.next_run_at or (
                r.last_run_at + timedelta(seconds=j.interval_s) if r.last_run_at else None
            )
            overdue = due is not None and due + stall < now
        jobs.append(
            {
                "name": name,
                "description": j.description if j else "",
                "interval_s": j.interval_s if j else None,
                "paused": bool(r and r.paused),
                "running": bool(
                    r
                    and r.lease_until > now
                    and r.last_started_at
                    and (r.last_run_at is None or r.last_started_at > r.last_run_at)
                ),
                "last_started_at": r.last_started_at if r else None,
                "last_success_at": r.last_success_at if r else None,
                "last_duration_ms": r.last_duration_ms if r else None,
                "next_run_at": r.next_run_at if r else None,
                "consecutive_failures": r.consecutive_failures if r else 0,
                "last_error": r.last_error if r else None,
                "run_count": r.run_count if r else 0,
                "overdue": overdue,
            }
        )
    if ctx.settings.worker_mode == "off":
        state = "disabled"
    elif not live:
        state = "stalled"
    elif any(j["consecutive_failures"] >= 3 or j["overdue"] for j in jobs):
        state = "degraded"
    else:
        state = "running"
    return {
        "state": state,
        "mode": ctx.settings.worker_mode,
        "workers": [
            {
                "holder": i.holder,
                "mode": i.mode,
                "version": i.version,
                "started_at": i.started_at,
                "heartbeat_at": i.heartbeat_at,
            }
            for i in live
        ],
        "jobs": jobs,
    }


async def watchdog(ctx: AppContext, now: datetime | None = None) -> str:
    """Raise (deduplicated) admin alerts when background automation is stalled or failing.

    Once every job is healthy again, open alerts are marked read with a "Recovered" note.
    """
    from oasis_route.notifications.service import notify, resolve

    st = await worker_status(ctx, now)
    if st["state"] in ("stalled", "degraded"):
        failing = [j["name"] for j in st["jobs"] if j["consecutive_failures"] >= 3 or j["overdue"]]
        async with ctx.db.transaction() as s:
            await notify(
                s,
                kind="system.worker",
                severity="critical" if st["state"] == "stalled" else "warning",
                title="Background maintenance stopped"
                if st["state"] == "stalled"
                else "Background maintenance is failing",
                body="No worker has reported in recently. Routing still works, but discovery, quota "
                "refresh and credential checks are paused until it recovers."
                if st["state"] == "stalled"
                else "Some maintenance tasks keep failing: " + ", ".join(failing),
                facts={"state": st["state"], "jobs": failing},
                dedupe_key=f"worker:{st['state']}",
            )
    elif st["state"] == "running":
        async with ctx.db.transaction() as s:
            stamp = (now or utcnow()).strftime("%Y-%m-%d %H:%M UTC")
            await resolve(s, kind="system.worker", note=f"Recovered at {stamp}.")
    return str(st["state"])
