"""In-app notifications. Facts are redacted; duplicates of an unread notification are coalesced."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oasis_route.db.models import Notification, utcnow
from oasis_route.security.redaction import redact

DEFAULT_RETENTION = timedelta(days=90)


async def notify(
    session: AsyncSession,
    *,
    kind: str,
    title: str,
    body: str = "",
    severity: str = "info",
    project_id: str | None = None,
    user_id: str | None = None,
    facts: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
) -> Notification:
    facts = redact(dict(facts or {}))
    if dedupe_key:
        facts["dedupe_key"] = dedupe_key
        existing = (
            await session.execute(
                select(Notification).where(
                    Notification.project_id == project_id,
                    Notification.kind == kind,
                    Notification.read_at.is_(None),
                )
            )
        ).scalars()
        for n in existing:
            if n.facts.get("dedupe_key") == dedupe_key:
                n.body = body
                n.facts = facts
                return n
    n = Notification(
        project_id=project_id,
        user_id=user_id,
        kind=kind,
        severity=severity,
        title=title,
        body=body,
        channel="in_app",
        facts=facts,
        delete_after=utcnow() + DEFAULT_RETENTION,
    )
    session.add(n)
    await session.flush()
    return n
