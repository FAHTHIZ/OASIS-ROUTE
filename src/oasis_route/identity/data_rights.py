"""Data export and deletion (account data rights).

Users can take a complete copy of what Oasis Route holds about a project or about themselves, and delete it.

Export contains settings, membership, Oasis Key metadata, connection metadata, quota pools, usage, routing
decisions, notifications and the audit trail. It never contains secrets: Oasis Key hashes, provider
credentials (or their ciphertext), OAuth state/verifier material and browser bindings are excluded.

Deletion destroys every stored provider credential belonging to the deleted scope (connection credentials,
pending OAuth verifiers, mail grants), then removes the rows. Audit events are kept: they are the
security record of what happened and contain no prompts or secrets. They are pruned by the configured
audit retention (``OASIS_ROUTE_AUDIT_RETENTION_DAYS``) like every other audit event.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.inspection import inspect

from oasis_route.db.models import (
    ApiKey,
    AuditEvent,
    Base,
    Connection,
    Credential,
    MailGrant,
    Notification,
    OAuthFlow,
    Project,
    ProjectMember,
    QuotaPool,
    RouteDecision,
    UsageRecord,
    User,
    utcnow,
)
from oasis_route.domain.enums import Role
from oasis_route.domain.errors import Conflict
from oasis_route.identity import audit

EXPORT_FORMAT = "oasis-route-export/1"

# Columns that hold secrets or secret-derived material. They never leave the server.
_EXCLUDED: dict[type[Base], set[str]] = {
    ApiKey: {"key_hash"},
    Connection: {"credential_id"},
    OAuthFlow: {"state_hash", "browser_binding_hash", "verifier_credential_id"},
    MailGrant: {"credential_id"},
}


def _row(obj: Base) -> dict[str, Any]:
    skip = _EXCLUDED.get(type(obj), set())
    out: dict[str, Any] = {}
    for attr in inspect(type(obj)).column_attrs:
        if attr.key in skip:
            continue
        v = getattr(obj, attr.key)
        if isinstance(v, datetime):
            v = v.isoformat()
        elif isinstance(v, bytes):  # pragma: no cover - no exported model has binary columns
            continue
        out[attr.key] = v
    return out


async def _all(session: AsyncSession, model: type[Base], *where: Any) -> list[dict[str, Any]]:
    q = select(model).where(*where)
    created = getattr(model, "created_at", None)
    if created is not None:
        q = q.order_by(created)
    return [_row(r) for r in (await session.execute(q)).scalars().all()]


async def export_project(session: AsyncSession, project: Project) -> dict[str, Any]:
    pid = project.id
    members = (
        await session.execute(
            select(ProjectMember.user_id, ProjectMember.role, User.email)
            .join(User, User.id == ProjectMember.user_id)
            .where(ProjectMember.project_id == pid)
        )
    ).all()
    return {
        "project": _row(project),
        "members": [{"user_id": m.user_id, "email": m.email, "role": m.role} for m in members],
        "oasis_keys": await _all(session, ApiKey, ApiKey.project_id == pid),
        "connections": await _all(session, Connection, Connection.project_id == pid),
        "oauth_flows": await _all(session, OAuthFlow, OAuthFlow.project_id == pid),
        "quota_pools": await _all(session, QuotaPool, QuotaPool.project_id == pid),
        "usage_records": await _all(session, UsageRecord, UsageRecord.project_id == pid),
        "route_decisions": await _all(session, RouteDecision, RouteDecision.project_id == pid),
        "notifications": await _all(session, Notification, Notification.project_id == pid),
        "audit_events": await _all(session, AuditEvent, AuditEvent.project_id == pid),
    }


async def owned_projects(session: AsyncSession, user_id: str) -> list[Project]:
    q = select(Project).where(Project.owner_id == user_id).order_by(Project.created_at)
    return list((await session.execute(q)).scalars().all())


async def export_user(session: AsyncSession, user: User) -> dict[str, Any]:
    memberships = (
        await session.execute(
            select(ProjectMember.project_id, ProjectMember.role).where(ProjectMember.user_id == user.id)
        )
    ).all()
    return {
        "format": EXPORT_FORMAT,
        "exported_at": utcnow().isoformat(),
        "user": _row(user),
        "memberships": [{"project_id": m.project_id, "role": m.role} for m in memberships],
        "notifications": await _all(session, Notification, Notification.user_id == user.id),
        "mail_grants": await _all(session, MailGrant, MailGrant.user_id == user.id),
        "audit_events": await _all(session, AuditEvent, AuditEvent.actor_user_id == user.id),
        "owned_projects": [await export_project(session, p) for p in await owned_projects(session, user.id)],
    }


async def _destroy_credentials(session: AsyncSession, ids: set[str | None]) -> int:
    real = {i for i in ids if i}
    if not real:
        return 0
    res = await session.execute(delete(Credential).where(Credential.id.in_(real)))
    return int(getattr(res, "rowcount", 0) or 0)


async def delete_project(session: AsyncSession, project: Project, *, actor_user_id: str) -> dict[str, int]:
    """Delete a project and everything in it. Provider credentials are destroyed first."""
    pid = project.id
    cred_ids: set[str | None] = set(
        (await session.execute(select(Connection.credential_id).where(Connection.project_id == pid)))
        .scalars()
        .all()
    )
    cred_ids |= set(
        (await session.execute(select(OAuthFlow.verifier_credential_id).where(OAuthFlow.project_id == pid)))
        .scalars()
        .all()
    )
    counts = {
        "connections": await _count(session, Connection, Connection.project_id == pid),
        "oasis_keys": await _count(session, ApiKey, ApiKey.project_id == pid),
        "usage_records": await _count(session, UsageRecord, UsageRecord.project_id == pid),
        "route_decisions": await _count(session, RouteDecision, RouteDecision.project_id == pid),
    }
    # Detach credentials from connections, then remove the project (the database cascades the rest).
    await session.execute(update(Connection).where(Connection.project_id == pid).values(credential_id=None))
    await session.execute(delete(Project).where(Project.id == pid))
    counts["vault_entries_destroyed"] = await _destroy_credentials(session, cred_ids)
    session.expunge_all()
    await audit.record(
        session,
        "project.deleted",
        actor_user_id=actor_user_id,
        project_id=pid,
        target_type="project",
        target_id=pid,
        details=counts,
    )
    return counts


async def _count(session: AsyncSession, model: type[Base], *where: Any) -> int:
    return int((await session.execute(select(func.count()).select_from(model).where(*where))).scalar_one())


async def delete_user(session: AsyncSession, user: User, *, actor_user_id: str) -> dict[str, int]:
    """Delete a user, the projects they own, their keys and their mail data.

    Refuses when it would remove the last active administrator, or when an owned project has other members
    (deleting it would take their data too; remove those members or delete the project explicitly first).
    """
    uid = user.id
    if user.role == Role.ADMIN.value:
        admins = await _count(session, User, User.role == Role.ADMIN.value, User.is_active.is_(True))
        if admins <= 1 and user.is_active:
            raise Conflict("cannot delete the last active administrator")
    owned = await owned_projects(session, uid)
    for p in owned:
        others = await _count(
            session, ProjectMember, ProjectMember.project_id == p.id, ProjectMember.user_id != uid
        )
        if others:
            raise Conflict(
                f"project {p.id} has other members; remove them or delete the project first",
                details={"project_id": p.id},
            )
    deleted_projects = 0
    for p in owned:
        await delete_project(session, p, actor_user_id=actor_user_id)
        deleted_projects += 1
    cred_ids: set[str | None] = set(
        (await session.execute(select(MailGrant.credential_id).where(MailGrant.user_id == uid)))
        .scalars()
        .all()
    )
    cred_ids |= set(
        (await session.execute(select(OAuthFlow.verifier_credential_id).where(OAuthFlow.user_id == uid)))
        .scalars()
        .all()
    )
    keys = await session.execute(delete(ApiKey).where(ApiKey.user_id == uid))
    await session.execute(delete(MailGrant).where(MailGrant.user_id == uid))
    await session.execute(delete(OAuthFlow).where(OAuthFlow.user_id == uid))
    await session.execute(delete(User).where(User.id == uid))
    destroyed = await _destroy_credentials(session, cred_ids)
    session.expunge_all()
    counts = {
        "projects_deleted": deleted_projects,
        "oasis_keys_deleted": int(getattr(keys, "rowcount", 0) or 0),
        "vault_entries_destroyed": destroyed,
    }
    await audit.record(
        session,
        "user.deleted",
        actor_user_id=actor_user_id,
        target_type="user",
        target_id=uid,
        details=counts,
    )
    return counts
