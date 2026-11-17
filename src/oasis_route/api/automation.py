"""Zero-operations status and automation controls.

``GET /projects/{id}/status`` answers the questions an ordinary user cares about, in plain language: is my
connection working, is Oasis Route ready, which model is serving me, is capacity healthy, does anything need
my attention. Internal terms (leases, breakers, ledgers) are deliberately absent; operators get those from
``GET /admin/jobs``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oasis_route.api.deps import Principal, get_ctx, get_session, project_access, require_admin, require_scope
from oasis_route.context import AppContext
from oasis_route.db.models import (
    ApiKey,
    Connection,
    HealthRecord,
    Model,
    Notification,
    RouteDecision,
    WorkerLease,
    utcnow,
)
from oasis_route.domain.enums import ApiKeyScope, ConnectionStatus
from oasis_route.domain.errors import NotFound
from oasis_route.identity import audit
from oasis_route.worker import runner

router = APIRouter(prefix="/api/v1", tags=["status"])
READ = Depends(require_scope(ApiKeyScope.READ))
MANAGE = Depends(require_scope(ApiKeyScope.MANAGE))

CONNECTED = "Connected"
CHECKING = "Checking provider"
DEGRADED = "Temporarily degraded"
ACTION = "Action required"
READY = "Ready"
NOT_CONNECTED = "Not connected"

_ACTION_KINDS = ("connection.suspended", "models.removed", "system.worker", "mail.key_security")


def _connection_label(c: Connection, health: list[HealthRecord], now: datetime) -> tuple[str, str]:
    if c.status == ConnectionStatus.PENDING_VALIDATION.value:
        return CHECKING, "Checking your credential with the provider."
    if c.status != ConnectionStatus.ACTIVE.value:
        why = c.last_error or "The provider rejected this credential."
        return ACTION, f"{why} Update the credential or reconnect this provider."
    open_ = [h for h in health if h.state != "healthy" and (h.open_until is None or h.open_until > now)]
    if any(h.model_scope == "*" for h in open_):
        return DEGRADED, "The provider is having problems; requests are going to other providers for now."
    if open_:
        return CONNECTED, "Working. Some models are temporarily unavailable and are being skipped."
    return CONNECTED, "Working."


_SPEND_REASONS = {"cost_unknown", "paid_route_blocked"}


async def _routing_ready(session: AsyncSession, project: Any, ctx: AppContext) -> dict[str, Any] | None:
    """The first candidate ``auto`` would use, or ``{"spend_blocked": True}`` when only the spend policy
    stands in the way, or ``None``."""
    from oasis_route.gateway.engine import GatewayError, parse_body
    from oasis_route.routing.router import build_plan

    try:
        parsed = parse_body(
            {"model": "auto", "messages": [{"role": "user", "content": "status"}]},
            project,
            ctx.settings.default_max_output_tokens,
        )
        plan = await build_plan(session, project, parsed.options)
    except GatewayError:
        return None
    if not plan.candidates:
        if plan.rejected and all(r.reason in _SPEND_REASONS for r in plan.rejected):
            return {"spend_blocked": True}
        return None
    c = plan.candidates[0]
    return {"provider": c.provider_id, "model": c.upstream_model}


@router.get("/projects/{project_id}/status")
async def project_status(
    project_id: str,
    p: Principal = READ,
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Plain-language readiness for the console home screen and for scripted health checks."""
    now = utcnow()
    async with ctx.db.session() as session:
        project = await project_access(project_id, p, session)
        conns = list(
            (
                await session.execute(
                    select(Connection).where(
                        Connection.project_id == project_id,
                        Connection.status != ConnectionStatus.REVOKED.value,
                    )
                )
            ).scalars()
        )
        health: dict[str, list[HealthRecord]] = {}
        if conns:
            for h in (
                await session.execute(
                    select(HealthRecord).where(HealthRecord.connection_id.in_([c.id for c in conns]))
                )
            ).scalars():
                health.setdefault(h.connection_id, []).append(h)
        active_providers = {c.provider_id for c in conns if c.status == ConnectionStatus.ACTIVE.value}
        models = 0
        if active_providers:
            models = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(Model)
                        .where(Model.provider_id.in_(active_providers), Model.lifecycle != "removed")
                    )
                ).scalar_one()
            )
        has_key = (
            await session.execute(
                select(func.count())
                .select_from(ApiKey)
                .where(ApiKey.project_id == project_id, ApiKey.revoked_at.is_(None))
            )
        ).scalar_one() > 0
        ready = await _routing_ready(session, project, ctx) if active_providers else None
        spend_blocked = bool(ready and ready.get("spend_blocked"))
        serving = None if spend_blocked else ready
        last = (
            await session.execute(
                select(RouteDecision)
                .where(RouteDecision.project_id == project_id, RouteDecision.served_model.is_not(None))
                .order_by(RouteDecision.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        attention = list(
            (
                await session.execute(
                    select(Notification)
                    .where(
                        (Notification.project_id == project_id)
                        | ((Notification.project_id.is_(None)) & (Notification.user_id == p.user.id)),
                        Notification.read_at.is_(None),
                        Notification.severity.in_(("warning", "critical")),
                    )
                    .order_by(Notification.created_at.desc())
                    .limit(10)
                )
            ).scalars()
        )
        from oasis_route.capacity import service as capacity

        cap = await capacity.project_report(session, project)

    items = []
    for c in conns:
        label, message = _connection_label(c, health.get(c.id, []), now)
        items.append(
            {
                "id": c.id,
                "name": c.display_name or c.provider_id,
                "provider": c.provider_id,
                "status": label,
                "message": message,
                "last_checked_at": c.last_validated_at,
            }
        )
    labels = {i["status"] for i in items}
    if not items:
        overall, summary = NOT_CONNECTED, "Add a provider API key to get started."
    elif spend_blocked:
        overall, summary = (
            ACTION,
            "Free-only mode is on and no connected model is confirmed free. Mark a connection as a "
            "free-tier account, connect a provider with free models, or allow paid usage for this project.",
        )
    elif serving is not None:
        overall = READY
        summary = "Ready. Requests are routed automatically to the strongest available model."
        if ACTION in labels:
            summary += " One connection needs your attention."
    elif CHECKING in labels:
        overall, summary = CHECKING, "Checking your provider connection."
    elif DEGRADED in labels or CONNECTED in labels:
        overall, summary = (
            DEGRADED,
            "Connected, but no model can take requests right now. Retrying automatically.",
        )
    else:
        overall, summary = ACTION, "Your provider connections need attention before requests can be served."

    allocs = cap.get("allocations", [])
    if not allocs:
        capacity_label = "Unknown" if active_providers else "None"
        capacity_msg = (
            "The provider has not reported limits yet; they are learned automatically from responses."
            if active_providers
            else "Connect a provider to see capacity."
        )
    elif any(a.get("routable_now") for a in allocs):
        capacity_label, capacity_msg = "Healthy", "Capacity is available."
    else:
        capacity_label, capacity_msg = "Exhausted", "Capacity is used up until the next provider reset."

    wst = await runner.worker_status(ctx, now)
    background = {
        "running": "Running",
        "degraded": "Running with issues",
        "stalled": "Stopped",
        "disabled": "Disabled",
    }[wst["state"]]
    return {
        "status": overall,
        "summary": summary,
        "setup": {
            "credential_validated": bool(active_providers),
            "models_synced": models > 0,
            "routing_ready": serving is not None,
            "oasis_key_issued": bool(has_key),
            "complete": bool(active_providers) and models > 0 and serving is not None and bool(has_key),
        },
        "spend_policy": project.spend_policy,
        "connections": items,
        "serving": {
            "next_request": serving,
            "last_request": (
                {"provider": last.served_provider, "model": last.served_model, "at": last.created_at}
                if last
                else None
            ),
        },
        "capacity": {"status": capacity_label, "message": capacity_msg},
        "attention": [
            {"id": n.id, "title": n.title, "body": n.body, "severity": n.severity, "at": n.created_at}
            for n in attention
        ],
        "automation": {"paused": project.automation_paused, "background": background},
    }


class AutomationIn(BaseModel):
    paused: bool


@router.put("/projects/{project_id}/automation")
async def set_automation(
    project_id: str,
    body: AutomationIn,
    p: Principal = MANAGE,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Pause or resume background maintenance for this project. Routing is unaffected."""
    project = await project_access(project_id, p, session)
    project.automation_paused = body.paused
    await audit.record(
        session,
        "project.automation_paused" if body.paused else "project.automation_resumed",
        actor_user_id=p.user.id,
        actor_key_id=p.key.id,
        project_id=project.id,
    )
    return {"paused": project.automation_paused}


# ------------------------------------------------------------------------------------------- operators


@router.get("/admin/jobs")
async def list_jobs(
    _: Principal = Depends(require_admin), ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    """Background workers and per-job schedule, last result, failures and backoff."""
    return await runner.worker_status(ctx)


async def _lease(session: AsyncSession, ctx: AppContext, name: str) -> WorkerLease:
    if name not in {j.name for j in runner.default_jobs(ctx)}:
        raise NotFound("unknown job")
    row = await session.get(WorkerLease, name)
    if row is None:
        row = WorkerLease(name=name, holder="", lease_until=utcnow() - timedelta(seconds=1))
        session.add(row)
    return row


@router.post("/admin/jobs/{name}/{action}")
async def job_action(
    name: str,
    action: str,
    p: Principal = Depends(require_admin),
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """``pause`` | ``resume`` | ``run`` (run as soon as a worker is free)."""
    if action not in ("pause", "resume", "run"):
        raise NotFound("unknown action")
    row = await _lease(session, ctx, name)
    if action == "pause":
        row.paused = True
    elif action == "resume":
        row.paused = False
    else:
        row.next_run_at = utcnow()
    await audit.record(
        session, f"job.{action}", actor_user_id=p.user.id, actor_key_id=p.key.id, details={"job": name}
    )
    return {"name": name, "paused": row.paused, "next_run_at": row.next_run_at}
