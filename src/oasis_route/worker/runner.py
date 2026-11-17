"""Lease-coordinated background worker.

Every job is guarded by a row in ``worker_leases``: a job runs only when it is due and its lease is free
or expired, so any number of worker processes (or API replicas running ``--with-worker``) can run safely.
A crashed worker's lease simply expires.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from oasis_route.context import AppContext
from oasis_route.db.models import Connection, IdempotencyRecord, Notification, WorkerLease, utcnow
from oasis_route.domain.enums import ConnectionStatus

log = logging.getLogger(__name__)
JobFn = Callable[[AppContext], Awaitable[Any]]


@dataclass(frozen=True)
class Job:
    name: str
    interval_s: float
    fn: JobFn
    lease_s: float = 300.0


async def _expire_reservations(ctx: AppContext) -> Any:
    from oasis_route.quota import ledger

    return {"expired": await ledger.expire_stale(ctx.db)}


async def _discovery(ctx: AppContext) -> Any:
    from oasis_route.discovery import service

    res = await service.run_due_sources(ctx)
    return {"sources": len(res), "changed": sum(1 for r in res if r.changed)}


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
                    .where(
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
    return {"notifications": getattr(n, "rowcount", 0), "idempotency": getattr(i, "rowcount", 0)}


async def _mail_sync(ctx: AppContext) -> Any:
    from oasis_route.notifications import mail

    return await mail.sync_all(ctx)


def default_jobs(ctx: AppContext) -> list[Job]:
    s = ctx.settings
    return [
        Job("ledger.expire_reservations", 30, _expire_reservations, lease_s=120),
        Job("discovery.sources", s.worker_interval_s, _discovery, lease_s=900),
        Job("offers.lifecycle", 3600, _offer_lifecycle),
        Job("connections.revalidate", 3600, _revalidate_connections, lease_s=600),
        Job("retention.evidence", 86_400, _evidence_retention),
        Job("retention.records", 3600, _retention),
        Job("mail.gmail_sync", 900, _mail_sync, lease_s=600),
    ]


def holder_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def acquire(ctx: AppContext, job: Job, holder: str, now: datetime | None = None) -> bool:
    """Take the job's lease iff it is due and not held by a live worker."""
    now = now or utcnow()
    try:
        async with ctx.db.transaction() as s:
            row = (
                await s.execute(select(WorkerLease).where(WorkerLease.name == job.name).with_for_update())
            ).scalar_one_or_none()
            if row is None:
                s.add(
                    WorkerLease(
                        name=job.name, holder=holder, lease_until=now + timedelta(seconds=job.lease_s)
                    )
                )
                return True
            if row.lease_until > now and row.holder != holder:
                return False
            if row.last_run_at is not None and row.last_run_at + timedelta(seconds=job.interval_s) > now:
                return False
            row.holder = holder
            row.lease_until = now + timedelta(seconds=job.lease_s)
            return True
    except IntegrityError:
        return False  # another worker inserted the lease first


async def finish(ctx: AppContext, job: Job, holder: str, result: str) -> None:
    now = utcnow()
    async with ctx.db.transaction() as s:
        row = await s.get(WorkerLease, job.name)
        if row is None or row.holder != holder:
            return
        row.last_run_at = now
        row.last_result = result[:4000]
        row.lease_until = now  # release


async def run_once(
    ctx: AppContext, jobs: list[Job] | None = None, holder: str | None = None
) -> dict[str, Any]:
    holder = holder or holder_id()
    ran: dict[str, Any] = {}
    for job in jobs or default_jobs(ctx):
        if not await acquire(ctx, job, holder):
            continue
        try:
            res = await job.fn(ctx)
            text = json.dumps({"ok": True, "result": res}, default=str)
            ran[job.name] = res
        except Exception as exc:
            log.exception("job failed", extra={"job": job.name})
            text = json.dumps({"ok": False, "error": type(exc).__name__})
            ran[job.name] = {"error": type(exc).__name__}
        await finish(ctx, job, holder, text)
    return ran


async def run_forever(ctx: AppContext, stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    holder = holder_id()
    tick = min(30.0, ctx.settings.worker_interval_s)
    log.info("worker started", extra={"holder": holder})
    while not stop.is_set():
        try:
            await run_once(ctx, holder=holder)
        except Exception:  # pragma: no cover - database blips must not kill the worker
            log.exception("worker tick failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=tick)
