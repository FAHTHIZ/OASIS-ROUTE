"""Admin API: capacity analytics, usage history, route decisions, notifications."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oasis_route.api.deps import Principal, get_session, project_access, require_scope
from oasis_route.capacity import service as capacity
from oasis_route.db.models import Notification, RouteDecision, UsageRecord, utcnow
from oasis_route.domain.enums import ApiKeyScope
from oasis_route.domain.errors import NotFound

router = APIRouter(prefix="/api/v1", tags=["capacity"])
READ = Depends(require_scope(ApiKeyScope.READ))


@router.get("/projects/{project_id}/capacity")
async def get_capacity(
    project_id: str, p: Principal = READ, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Theoretical, authorized, usable, consumed, and forecast capacity (see ROUTING_AND_QUOTAS.md)."""
    project = await project_access(project_id, p, session)
    return await capacity.project_report(session, project)


@router.get("/projects/{project_id}/usage")
async def list_usage(
    project_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    since: datetime | None = Query(default=None),
    p: Principal = READ,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    await project_access(project_id, p, session)
    q = select(UsageRecord).where(UsageRecord.project_id == project_id)
    if since is not None:
        q = q.where(UsageRecord.created_at >= since)
    rows = (await session.execute(q.order_by(UsageRecord.created_at.desc()).limit(limit))).scalars()
    return [
        {
            "request_id": u.request_id,
            "attempt": u.attempt,
            "provider_id": u.provider_id,
            "upstream_model": u.upstream_model,
            "connection_id": u.connection_id,
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "usage_source": "provider_reported" if u.provider_reported else "estimated",
            "outcome": u.outcome,
            "error_category": u.error_category,
            "latency_ms": u.latency_ms,
            "streamed": u.streamed,
            "created_at": u.created_at,
        }
        for u in rows
    ]


@router.get("/projects/{project_id}/route-decisions")
async def list_route_decisions(
    project_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    p: Principal = READ,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    await project_access(project_id, p, session)
    rows = (
        await session.execute(
            select(RouteDecision)
            .where(RouteDecision.project_id == project_id)
            .order_by(RouteDecision.created_at.desc())
            .limit(limit)
        )
    ).scalars()
    return [_decision_out(d) for d in rows]


def _decision_out(d: RouteDecision) -> dict[str, Any]:
    return {
        "request_id": d.request_id,
        "requested_model": d.requested_model,
        "task": d.task,
        "mode": d.mode,
        "candidates": d.candidates,
        "rejected": d.rejected,
        "attempts": d.attempts,
        "served_model": d.served_model,
        "served_provider": d.served_provider,
        "fallback_used": d.fallback_used,
        "downgraded": d.downgraded,
        "outcome": d.outcome,
        "explanation": d.explanation or {},
        "created_at": d.created_at,
    }


@router.get("/projects/{project_id}/route-decisions/{request_id}")
async def get_route_decision(
    project_id: str,
    request_id: str,
    p: Principal = READ,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Explain one past request: the route chosen and why, alternatives, rejections, cost, fallback chain."""
    await project_access(project_id, p, session)
    d = (
        await session.execute(
            select(RouteDecision).where(
                RouteDecision.project_id == project_id, RouteDecision.request_id == request_id
            )
        )
    ).scalar_one_or_none()
    if d is None:
        raise NotFound("no route decision for this request id in this project")
    return _decision_out(d)


class NotificationOut(BaseModel):
    id: str
    project_id: str | None
    kind: str
    severity: str
    title: str
    body: str
    facts: dict[str, Any]
    created_at: datetime
    read_at: datetime | None


def _visible(p: Principal) -> Any:
    """A caller sees its project's notifications and its own; admins also see instance-wide ones."""
    conds = [Notification.project_id == p.project.id, Notification.user_id == p.user.id]
    if p.is_admin:
        conds.append(Notification.project_id.is_(None) & Notification.user_id.is_(None))
    return or_(*conds)


async def _get_visible(session: AsyncSession, p: Principal, nid: str) -> Notification:
    n = (
        await session.execute(select(Notification).where(Notification.id == nid, _visible(p)))
    ).scalar_one_or_none()
    if n is None:
        raise NotFound("notification not found")
    return n


@router.get("/notifications", response_model=list[NotificationOut])
async def list_notifications(
    unread: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    p: Principal = READ,
    session: AsyncSession = Depends(get_session),
) -> list[NotificationOut]:
    q = select(Notification).where(_visible(p))
    if unread:
        q = q.where(Notification.read_at.is_(None))
    rows = (await session.execute(q.order_by(Notification.created_at.desc()).limit(limit))).scalars()
    return [NotificationOut.model_validate(n, from_attributes=True) for n in rows]


@router.post("/notifications/{notification_id}/read", response_model=NotificationOut)
async def mark_read(
    notification_id: str, p: Principal = READ, session: AsyncSession = Depends(get_session)
) -> NotificationOut:
    n = await _get_visible(session, p, notification_id)
    n.read_at = n.read_at or utcnow()
    await session.flush()
    return NotificationOut.model_validate(n, from_attributes=True)


@router.delete("/notifications/{notification_id}", status_code=204)
async def delete_notification(
    notification_id: str, p: Principal = READ, session: AsyncSession = Depends(get_session)
) -> Response:
    n = await _get_visible(session, p, notification_id)
    await session.delete(n)
    return Response(status_code=204)
