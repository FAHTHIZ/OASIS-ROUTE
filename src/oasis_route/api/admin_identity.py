"""Admin API — identity: bootstrap, users, projects, members, API keys, audit."""

from __future__ import annotations

import hmac
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oasis_route.api.deps import (
    Principal,
    get_ctx,
    get_session,
    project_access,
    require_admin,
    require_scope,
)
from oasis_route.context import AppContext
from oasis_route.db.models import ApiKey, AuditEvent, Project, ProjectMember, User, utcnow
from oasis_route.domain.enums import ApiKeyScope, ProjectRole, Role, RoutingMode
from oasis_route.domain.errors import Conflict, Forbidden, NotFound, Unauthorized, ValidationFailed
from oasis_route.identity import audit, data_rights, service

router = APIRouter(prefix="/api/v1", tags=["identity"])


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserOut(ORM):
    id: str
    email: str
    display_name: str
    role: str
    is_active: bool
    country: str | None
    declared_age: int | None
    created_at: datetime


class DataPolicy(BaseModel):
    require_no_training: bool = False
    require_zero_retention: bool = False
    allowed_regions: list[str] = Field(default_factory=list, description="Empty means any region.")
    provider_allowlist: list[str] = Field(default_factory=list)
    provider_denylist: list[str] = Field(default_factory=list)


class ProjectOut(ORM):
    id: str
    name: str
    owner_id: str
    routing_mode: str
    data_policy: dict[str, Any]
    max_fallback_hops: int | None
    allow_downgrade: bool
    allow_unknown_quota: bool
    kill_switch: bool
    rate_limit_rpm: int | None
    created_at: datetime


class ApiKeyOut(ORM):
    id: str
    project_id: str
    name: str
    prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    rotated_from_id: str | None


class ApiKeyCreated(BaseModel):
    key: ApiKeyOut
    secret: str = Field(description="Shown once. Store it securely; it cannot be retrieved again.")


class BootstrapIn(BaseModel):
    token: str
    email: str
    display_name: str = ""
    project_name: str = "default"


class BootstrapOut(BaseModel):
    user: UserOut
    project: ProjectOut
    api_key: ApiKeyCreated


@router.post("/bootstrap", response_model=BootstrapOut, status_code=201)
async def bootstrap(
    body: BootstrapIn, ctx: AppContext = Depends(get_ctx), session: AsyncSession = Depends(get_session)
) -> BootstrapOut:
    """Create the first administrator. Only works while no users exist and with the bootstrap token."""
    configured = ctx.settings.admin_bootstrap_token
    if configured is None:
        raise Forbidden("bootstrap disabled: set OASIS_ROUTE_ADMIN_BOOTSTRAP_TOKEN")
    if not hmac.compare_digest(body.token.encode(), configured.get_secret_value().encode()):
        raise Unauthorized("invalid bootstrap token")
    if await service.user_count(session) > 0:
        raise Conflict("instance already bootstrapped")
    user = await service.create_user(session, body.email, display_name=body.display_name, role=Role.ADMIN)
    project = await service.create_project(session, user, body.project_name)
    key, secret = await service.issue_api_key(
        session,
        project=project,
        user=user,
        pepper=ctx.settings.pepper_bytes(),
        name="bootstrap-admin",
        scopes=service.ALL_SCOPES,
    )
    await audit.record(session, "instance.bootstrapped", actor_user_id=user.id, project_id=project.id)
    return BootstrapOut(
        user=UserOut.model_validate(user),
        project=ProjectOut.model_validate(project),
        api_key=ApiKeyCreated(key=ApiKeyOut.model_validate(key), secret=secret),
    )


class MeOut(BaseModel):
    user: UserOut
    project: ProjectOut
    key: ApiKeyOut


@router.get("/me", response_model=MeOut)
async def me(p: Principal = Depends(require_scope(ApiKeyScope.READ))) -> MeOut:
    return MeOut(
        user=UserOut.model_validate(p.user),
        project=ProjectOut.model_validate(p.project),
        key=ApiKeyOut.model_validate(p.key),
    )


class UserCreate(BaseModel):
    email: str
    display_name: str = ""
    role: Role = Role.MEMBER
    country: str | None = Field(default=None, min_length=2, max_length=2)
    declared_age: int | None = Field(default=None, ge=0, le=150)
    project_name: str = "default"


class UserCreated(BaseModel):
    user: UserOut
    project: ProjectOut
    api_key: ApiKeyCreated


@router.post("/users", response_model=UserCreated, status_code=201)
async def create_user(
    body: UserCreate,
    p: Principal = Depends(require_admin),
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> UserCreated:
    user = await service.create_user(
        session,
        body.email,
        display_name=body.display_name,
        role=body.role,
        country=body.country,
        declared_age=body.declared_age,
    )
    project = await service.create_project(session, user, body.project_name)
    key, secret = await service.issue_api_key(
        session,
        project=project,
        user=user,
        pepper=ctx.settings.pepper_bytes(),
        name="initial",
        scopes=service.ALL_SCOPES,
    )
    await audit.record(
        session,
        "user.created",
        actor_user_id=p.user.id,
        target_type="user",
        target_id=user.id,
        details={"role": user.role},
    )
    return UserCreated(
        user=UserOut.model_validate(user),
        project=ProjectOut.model_validate(project),
        api_key=ApiKeyCreated(key=ApiKeyOut.model_validate(key), secret=secret),
    )


@router.get("/users", response_model=list[UserOut])
async def list_users(
    _: Principal = Depends(require_admin), session: AsyncSession = Depends(get_session)
) -> list[UserOut]:
    rows = (await session.execute(select(User).order_by(User.created_at))).scalars().all()
    return [UserOut.model_validate(u) for u in rows]


class UserPatch(BaseModel):
    is_active: bool | None = None
    country: str | None = Field(default=None, min_length=2, max_length=2)
    declared_age: int | None = Field(default=None, ge=0, le=150)
    display_name: str | None = None


@router.patch("/users/{user_id}", response_model=UserOut)
async def patch_user(
    user_id: str,
    body: UserPatch,
    p: Principal = Depends(require_scope(ApiKeyScope.MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    if user_id != p.user.id and not p.is_admin:
        raise NotFound("user not found")
    user = await session.get(User, user_id)
    if user is None:
        raise NotFound("user not found")
    if body.is_active is not None:
        if not p.is_admin:
            raise Forbidden("only administrators may change activation")
        user.is_active = body.is_active
    if body.country is not None:
        user.country = body.country.upper()
    if body.declared_age is not None:
        user.declared_age = body.declared_age
    if body.display_name is not None:
        user.display_name = body.display_name
    await audit.record(
        session,
        "user.updated",
        actor_user_id=p.user.id,
        target_type="user",
        target_id=user.id,
        details=body.model_dump(exclude_none=True),
    )
    return UserOut.model_validate(user)


# ------------------------------------------------------------------------------------------- projects
class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ProjectCreated(BaseModel):
    project: ProjectOut
    api_key: ApiKeyCreated


@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(
    p: Principal = Depends(require_scope(ApiKeyScope.READ)), session: AsyncSession = Depends(get_session)
) -> list[ProjectOut]:
    q = select(Project).order_by(Project.created_at)
    if not p.is_admin:
        q = q.join(ProjectMember, ProjectMember.project_id == Project.id).where(
            ProjectMember.user_id == p.user.id
        )
    return [ProjectOut.model_validate(x) for x in (await session.execute(q)).scalars().all()]


@router.post("/projects", response_model=ProjectCreated, status_code=201)
async def create_project(
    body: ProjectCreate,
    p: Principal = Depends(require_scope(ApiKeyScope.MANAGE)),
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> ProjectCreated:
    project = await service.create_project(session, p.user, body.name)
    key, secret = await service.issue_api_key(
        session,
        project=project,
        user=p.user,
        pepper=ctx.settings.pepper_bytes(),
        name="initial",
        scopes=service.ALL_SCOPES,
    )
    await audit.record(session, "project.created", actor_user_id=p.user.id, project_id=project.id)
    return ProjectCreated(
        project=ProjectOut.model_validate(project),
        api_key=ApiKeyCreated(key=ApiKeyOut.model_validate(key), secret=secret),
    )


@router.get("/projects/{project_id}", response_model=ProjectOut)
async def get_project(
    project_id: str,
    p: Principal = Depends(require_scope(ApiKeyScope.READ)),
    session: AsyncSession = Depends(get_session),
) -> ProjectOut:
    return ProjectOut.model_validate(await project_access(project_id, p, session))


class ProjectPatch(BaseModel):
    name: str | None = None
    routing_mode: RoutingMode | None = None
    data_policy: DataPolicy | None = None
    max_fallback_hops: int | None = Field(default=None, ge=0, le=10)
    allow_downgrade: bool | None = None
    allow_unknown_quota: bool | None = None
    kill_switch: bool | None = None
    rate_limit_rpm: int | None = Field(default=None, ge=1)


@router.patch("/projects/{project_id}", response_model=ProjectOut)
async def patch_project(
    project_id: str,
    body: ProjectPatch,
    p: Principal = Depends(require_scope(ApiKeyScope.MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> ProjectOut:
    project = await project_access(project_id, p, session, write=True)
    role = await service.member_role(session, project.id, p.user.id)
    service.require_project_role(role, {ProjectRole.OWNER}, p.user)
    changes = body.model_dump(exclude_unset=True, mode="json")
    for k, v in changes.items():
        setattr(project, k, v)
    await audit.record(
        session,
        "project.updated",
        actor_user_id=p.user.id,
        actor_key_id=p.key.id,
        project_id=project.id,
        details=changes,
    )
    return ProjectOut.model_validate(project)


class MemberIn(BaseModel):
    email: str
    role: ProjectRole = ProjectRole.DEVELOPER


@router.post("/projects/{project_id}/members", status_code=201)
async def add_member(
    project_id: str,
    body: MemberIn,
    p: Principal = Depends(require_scope(ApiKeyScope.MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    project = await project_access(project_id, p, session, write=True)
    service.require_project_role(
        await service.member_role(session, project.id, p.user.id), {ProjectRole.OWNER}, p.user
    )
    user = (await session.execute(select(User).where(User.email == body.email.lower()))).scalar_one_or_none()
    if user is None:
        raise NotFound("user not found")
    m = await service.add_member(session, project, user, body.role, p.user)
    return {"project_id": m.project_id, "user_id": m.user_id, "role": m.role}


# ------------------------------------------------------------------------------------------- api keys
class KeyCreate(BaseModel):
    name: str = ""
    scopes: list[ApiKeyScope] = Field(default_factory=lambda: [ApiKeyScope.INFERENCE])
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


@router.get("/projects/{project_id}/keys", response_model=list[ApiKeyOut])
async def list_keys(
    project_id: str,
    p: Principal = Depends(require_scope(ApiKeyScope.READ)),
    session: AsyncSession = Depends(get_session),
) -> list[ApiKeyOut]:
    project = await project_access(project_id, p, session)
    rows = (
        (
            await session.execute(
                select(ApiKey).where(ApiKey.project_id == project.id).order_by(ApiKey.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [ApiKeyOut.model_validate(k) for k in rows]


@router.post("/projects/{project_id}/keys", response_model=ApiKeyCreated, status_code=201)
async def create_key(
    project_id: str,
    body: KeyCreate,
    p: Principal = Depends(require_scope(ApiKeyScope.MANAGE)),
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> ApiKeyCreated:
    project = await project_access(project_id, p, session, write=True)
    role = await service.member_role(session, project.id, p.user.id)
    service.require_project_role(role, {ProjectRole.OWNER, ProjectRole.DEVELOPER}, p.user)
    key, secret = await service.issue_api_key(
        session,
        project=project,
        user=p.user,
        pepper=ctx.settings.pepper_bytes(),
        name=body.name,
        scopes=[s.value for s in body.scopes],
        expires_in_days=body.expires_in_days,
    )
    return ApiKeyCreated(key=ApiKeyOut.model_validate(key), secret=secret)


class RotateIn(BaseModel):
    grace_hours: int = Field(default=0, ge=0, le=168)


@router.post("/projects/{project_id}/keys/{key_id}/rotate", response_model=ApiKeyCreated)
async def rotate_key(
    project_id: str,
    key_id: str,
    body: RotateIn | None = None,
    p: Principal = Depends(require_scope(ApiKeyScope.MANAGE)),
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> ApiKeyCreated:
    project = await project_access(project_id, p, session, write=True)
    key = await service.get_key(session, project.id, key_id)
    new, secret = await service.rotate_api_key(
        session, key, project, p.user, ctx.settings.pepper_bytes(), (body or RotateIn()).grace_hours
    )
    return ApiKeyCreated(key=ApiKeyOut.model_validate(new), secret=secret)


@router.delete("/projects/{project_id}/keys/{key_id}", response_model=ApiKeyOut)
async def revoke_key(
    project_id: str,
    key_id: str,
    p: Principal = Depends(require_scope(ApiKeyScope.MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> ApiKeyOut:
    project = await project_access(project_id, p, session, write=True)
    key = await service.get_key(session, project.id, key_id)
    await service.revoke_api_key(session, key, p.user)
    return ApiKeyOut.model_validate(key)


# ------------------------------------------------------------------------------------------- audit
class AuditOut(ORM):
    id: str
    created_at: datetime
    actor_user_id: str | None
    actor_key_id: str | None
    project_id: str | None
    action: str
    target_type: str | None
    target_id: str | None
    details: dict[str, Any]


@router.get("/projects/{project_id}/audit", response_model=list[AuditOut])
async def project_audit(
    project_id: str,
    limit: int = Query(100, ge=1, le=1000),
    p: Principal = Depends(require_scope(ApiKeyScope.READ)),
    session: AsyncSession = Depends(get_session),
) -> list[AuditOut]:
    project = await project_access(project_id, p, session)
    rows = (
        (
            await session.execute(
                select(AuditEvent)
                .where(AuditEvent.project_id == project.id)
                .order_by(AuditEvent.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [AuditOut.model_validate(r) for r in rows]


@router.get("/audit", response_model=list[AuditOut])
async def instance_audit(
    limit: int = Query(100, ge=1, le=1000),
    _: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[AuditOut]:
    rows = await session.execute(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(limit))
    return [AuditOut.model_validate(r) for r in rows.scalars().all()]


# ------------------------------------------------------------------------------------------- data rights
def _attachment(data: dict[str, Any], filename: str) -> JSONResponse:
    return JSONResponse(
        data,
        headers={
            "content-disposition": f'attachment; filename="{filename}"',
            "cache-control": "no-store",
        },
    )


@router.get("/projects/{project_id}/export")
async def export_project(
    project_id: str,
    p: Principal = Depends(require_scope(ApiKeyScope.MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Everything stored for a project, without secrets. Project owners and administrators."""
    project = await project_access(project_id, p, session, write=True)
    service.require_project_role(
        await service.member_role(session, project.id, p.user.id), {ProjectRole.OWNER}, p.user
    )
    data = {
        "format": data_rights.EXPORT_FORMAT,
        "exported_at": utcnow().isoformat(),
        **await data_rights.export_project(session, project),
    }
    await audit.record(
        session, "project.exported", actor_user_id=p.user.id, actor_key_id=p.key.id, project_id=project.id
    )
    return _attachment(data, f"oasis-route-project-{project.id}.json")


class DeleteConfirm(BaseModel):
    confirm: str = Field(description="Must equal the id of the thing being deleted.")


@router.delete("/projects/{project_id}")
async def delete_project(
    project_id: str,
    body: DeleteConfirm,
    p: Principal = Depends(require_scope(ApiKeyScope.MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Delete a project, destroy its provider credentials and revoke its Oasis Keys. Irreversible."""
    project = await project_access(project_id, p, session, write=True)
    service.require_project_role(
        await service.member_role(session, project.id, p.user.id), {ProjectRole.OWNER}, p.user
    )
    if body.confirm != project.id:
        raise ValidationFailed("confirm must equal the project id")
    counts = await data_rights.delete_project(session, project, actor_user_id=p.user.id)
    return {"deleted": True, "project_id": project_id, **counts}


async def _user_for(user_id: str, p: Principal, session: AsyncSession) -> User:
    if user_id != p.user.id and not p.is_admin:
        raise NotFound("user not found")
    user = await session.get(User, user_id)
    if user is None:
        raise NotFound("user not found")
    return user


@router.get("/users/{user_id}/export")
async def export_user(
    user_id: str,
    p: Principal = Depends(require_scope(ApiKeyScope.MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Everything stored about a user, including the projects they own, without secrets."""
    user = await _user_for(user_id, p, session)
    data = await data_rights.export_user(session, user)
    await audit.record(
        session, "user.exported", actor_user_id=p.user.id, target_type="user", target_id=user.id
    )
    return _attachment(data, f"oasis-route-user-{user.id}.json")


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    body: DeleteConfirm,
    p: Principal = Depends(require_scope(ApiKeyScope.MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Delete a user account with its owned projects, keys, credentials and mail data. Irreversible."""
    user = await _user_for(user_id, p, session)
    if body.confirm != user.id:
        raise ValidationFailed("confirm must equal the user id")
    counts = await data_rights.delete_user(session, user, actor_user_id=p.user.id)
    return {"deleted": True, "user_id": user_id, **counts}
