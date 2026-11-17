"""Audit trail. Details are redacted before persistence."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oasis_route.db.models import AuditEvent
from oasis_route.security.redaction import redact


async def record(
    session: AsyncSession,
    action: str,
    *,
    actor_user_id: str | None = None,
    actor_key_id: str | None = None,
    project_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    ev = AuditEvent(
        action=action,
        actor_user_id=actor_user_id,
        actor_key_id=actor_key_id,
        project_id=project_id,
        target_type=target_type,
        target_id=target_id,
        details=redact(details or {}),
    )
    session.add(ev)
    await session.flush()
    return ev
