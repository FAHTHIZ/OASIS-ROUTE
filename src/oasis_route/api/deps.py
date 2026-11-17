"""Shared FastAPI dependencies: context, sessions, authentication, scope checks."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oasis_route.context import AppContext
from oasis_route.db.models import ApiKey, Project, User, utcnow
from oasis_route.domain.enums import ApiKeyScope, Role
from oasis_route.domain.errors import Forbidden, RateLimited, Unauthorized
from oasis_route.security.api_keys import parse_prefix, verify_key


def get_ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


async def get_session(ctx: AppContext = Depends(get_ctx)) -> AsyncIterator[AsyncSession]:
    async with ctx.db.transaction() as s:
        yield s


@dataclass
class Principal:
    user: User
    project: Project
    key: ApiKey

    @property
    def scopes(self) -> set[str]:
        return set(self.key.scopes)

    @property
    def is_admin(self) -> bool:
        return self.user.role == Role.ADMIN.value


def extract_bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key")


async def authenticate_key(ctx: AppContext, session: AsyncSession, plaintext: str | None) -> Principal:
    if not plaintext:
        raise Unauthorized("missing API key")
    prefix = parse_prefix(plaintext)
    if prefix is None:
        raise Unauthorized("invalid API key")
    key = (await session.execute(select(ApiKey).where(ApiKey.prefix == prefix))).scalar_one_or_none()
    if key is None or not verify_key(plaintext, key.key_hash, ctx.settings.pepper_bytes()):
        raise Unauthorized("invalid API key")
    now = utcnow()
    if key.revoked_at is not None:
        raise Unauthorized("API key revoked")
    if key.expires_at is not None and key.expires_at <= now:
        raise Unauthorized("API key expired")
    user = await session.get(User, key.user_id)
    project = await session.get(Project, key.project_id)
    if user is None or not user.is_active or project is None:
        raise Unauthorized("API key owner inactive")
    if key.last_used_at is None or now - key.last_used_at > timedelta(seconds=60):
        key.last_used_at = now
    return Principal(user=user, project=project, key=key)


async def get_principal(
    request: Request, ctx: AppContext = Depends(get_ctx), session: AsyncSession = Depends(get_session)
) -> Principal:
    p = await authenticate_key(ctx, session, extract_bearer(request))
    limit = p.project.rate_limit_rpm or ctx.settings.rate_limit_per_key_rpm
    ok, retry = ctx.limiter.check(f"key:{p.key.id}", limit)
    if not ok:
        raise RateLimited("rate limit exceeded for this API key", details={"retry_after_s": round(retry, 2)})
    request.state.principal = p
    return p


def require_scope(scope: ApiKeyScope) -> Callable[..., Awaitable[Principal]]:
    async def _dep(p: Principal = Depends(get_principal)) -> Principal:
        if scope.value not in p.scopes and not (
            scope is ApiKeyScope.READ and ApiKeyScope.MANAGE.value in p.scopes
        ):
            raise Forbidden(f"API key lacks scope '{scope.value}'")
        return p

    return _dep


async def require_admin(p: Principal = Depends(require_scope(ApiKeyScope.MANAGE))) -> Principal:
    if not p.is_admin:
        raise Forbidden("instance administrator required")
    return p


async def project_access(
    project_id: str, p: Principal, session: AsyncSession, *, write: bool = False
) -> Project:
    """Resolve a project for the caller. Keys are bound to their own project; admins may access any project.

    Other tenants' projects are reported as not found to avoid existence disclosure.
    """
    from oasis_route.domain.errors import NotFound

    if project_id != p.project.id and not p.is_admin:
        raise NotFound("project not found")
    if write and ApiKeyScope.MANAGE.value not in p.scopes:
        raise Forbidden("API key lacks scope 'manage'")
    project = p.project if project_id == p.project.id else await session.get(Project, project_id)
    if project is None:
        raise NotFound("project not found")
    return project
