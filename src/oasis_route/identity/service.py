"""Users, projects, membership, and API key lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oasis_route.db.models import ApiKey, Project, ProjectMember, User, utcnow
from oasis_route.domain.enums import ApiKeyScope, ProjectRole, Role
from oasis_route.domain.errors import Conflict, Forbidden, NotFound, ValidationFailed
from oasis_route.identity import audit
from oasis_route.security.api_keys import issue_key

ALL_SCOPES = [s.value for s in ApiKeyScope]


async def user_count(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count()).select_from(User))).scalar_one())


async def create_user(
    session: AsyncSession,
    email: str,
    *,
    display_name: str = "",
    role: Role = Role.MEMBER,
    country: str | None = None,
    declared_age: int | None = None,
) -> User:
    email = email.strip().lower()
    if "@" not in email:
        raise ValidationFailed("invalid email")
    if (await session.execute(select(User).where(User.email == email))).scalar_one_or_none():
        raise Conflict("user already exists")
    user = User(
        email=email,
        display_name=display_name,
        role=role.value,
        country=country.upper() if country else None,
        declared_age=declared_age,
    )
    session.add(user)
    await session.flush()
    return user


async def create_project(session: AsyncSession, owner: User, name: str) -> Project:
    project = Project(name=name, owner_id=owner.id, data_policy={})
    session.add(project)
    await session.flush()
    session.add(ProjectMember(project_id=project.id, user_id=owner.id, role=ProjectRole.OWNER.value))
    await session.flush()
    return project


async def issue_api_key(
    session: AsyncSession,
    *,
    project: Project,
    user: User,
    pepper: bytes,
    name: str = "",
    scopes: list[str] | None = None,
    expires_in_days: int | None = None,
    env: str = "live",
    rotated_from: str | None = None,
) -> tuple[ApiKey, str]:
    scopes = scopes or [ApiKeyScope.INFERENCE.value]
    bad = set(scopes) - set(ALL_SCOPES)
    if bad:
        raise ValidationFailed(f"unknown scopes: {sorted(bad)}")
    issued = issue_key(pepper, env)
    expires_at: datetime | None = utcnow() + timedelta(days=expires_in_days) if expires_in_days else None
    key = ApiKey(
        project_id=project.id,
        user_id=user.id,
        name=name,
        prefix=issued.prefix,
        key_hash=issued.key_hash,
        scopes=sorted(set(scopes)),
        expires_at=expires_at,
        rotated_from_id=rotated_from,
    )
    session.add(key)
    await session.flush()
    await audit.record(
        session,
        "api_key.created" if not rotated_from else "api_key.rotated",
        actor_user_id=user.id,
        project_id=project.id,
        target_type="api_key",
        target_id=key.id,
        details={"prefix": key.prefix, "scopes": key.scopes, "rotated_from": rotated_from},
    )
    return key, issued.plaintext


async def get_key(session: AsyncSession, project_id: str, key_id: str) -> ApiKey:
    key = (
        await session.execute(select(ApiKey).where(ApiKey.id == key_id, ApiKey.project_id == project_id))
    ).scalar_one_or_none()
    if key is None:
        raise NotFound("api key not found")
    return key


async def revoke_api_key(session: AsyncSession, key: ApiKey, actor: User) -> None:
    if key.revoked_at is None:
        key.revoked_at = utcnow()
        await audit.record(
            session,
            "api_key.revoked",
            actor_user_id=actor.id,
            project_id=key.project_id,
            target_type="api_key",
            target_id=key.id,
            details={"prefix": key.prefix},
        )


async def rotate_api_key(
    session: AsyncSession, key: ApiKey, project: Project, actor: User, pepper: bytes, grace_hours: int = 0
) -> tuple[ApiKey, str]:
    if key.revoked_at is not None:
        raise Conflict("cannot rotate a revoked key")
    new_key, plaintext = await issue_api_key(
        session,
        project=project,
        user=actor,
        pepper=pepper,
        name=key.name,
        scopes=list(key.scopes),
        rotated_from=key.id,
        env="live" if key.prefix.startswith("or_live_") else "test",
    )
    if grace_hours > 0:
        key.expires_at = utcnow() + timedelta(hours=grace_hours)
    else:
        key.revoked_at = utcnow()
    return new_key, plaintext


async def member_role(session: AsyncSession, project_id: str, user_id: str) -> str | None:
    row = (
        await session.execute(
            select(ProjectMember.role).where(
                ProjectMember.project_id == project_id, ProjectMember.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    return row


async def get_project_for(session: AsyncSession, project_id: str, user: User) -> Project:
    """Load a project the user may access. Admins may access any project."""
    project = await session.get(Project, project_id)
    if project is None:
        raise NotFound("project not found")
    if user.role != Role.ADMIN.value and await member_role(session, project_id, user.id) is None:
        # Do not reveal existence of other tenants' projects.
        raise NotFound("project not found")
    return project


async def add_member(
    session: AsyncSession, project: Project, user: User, role: ProjectRole, actor: User
) -> ProjectMember:
    if await member_role(session, project.id, user.id):
        raise Conflict("already a member")
    m = ProjectMember(project_id=project.id, user_id=user.id, role=role.value)
    session.add(m)
    await audit.record(
        session,
        "project.member_added",
        actor_user_id=actor.id,
        project_id=project.id,
        target_type="user",
        target_id=user.id,
        details={"role": role.value},
    )
    await session.flush()
    return m


def require_project_role(role: str | None, allowed: set[ProjectRole], user: User) -> None:
    if user.role == Role.ADMIN.value:
        return
    if role is None or ProjectRole(role) not in allowed:
        raise Forbidden("insufficient project role")
