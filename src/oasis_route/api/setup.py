"""Turnkey onboarding API: local server detection, connectable providers, route classification, setup."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from oasis_route import onboarding
from oasis_route.api.deps import Principal, get_ctx, project_access, require_scope
from oasis_route.context import AppContext
from oasis_route.domain.enums import ApiKeyScope
from oasis_route.identity import audit

router = APIRouter(prefix="/api/v1", tags=["setup"])


def _base(request: Request, ctx: AppContext) -> str:
    return (ctx.settings.public_url or str(request.base_url)).rstrip("/")


@router.get("/projects/{project_id}/routes")
async def list_routes(
    project_id: str,
    p: Principal = Depends(require_scope(ApiKeyScope.READ)),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Every route of the project classified as free / paid / unknown and eligible / blocked / exhausted."""
    async with ctx.db.session() as session:
        project = await project_access(project_id, p, session)
        return await onboarding.classify_routes(session, project)


@router.get("/projects/{project_id}/setup")
async def get_setup(
    project_id: str,
    request: Request,
    p: Principal = Depends(require_scope(ApiKeyScope.READ)),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    async with ctx.db.session() as session:
        project = await project_access(project_id, p, session)
    return await onboarding.setup_plan(ctx, project, _base(request, ctx))


class SetupIn(BaseModel):
    connect_local: bool = True


@router.post("/projects/{project_id}/setup")
async def run_setup(
    project_id: str,
    body: SetupIn,
    request: Request,
    p: Principal = Depends(require_scope(ApiKeyScope.MANAGE)),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    async with ctx.db.session() as session:
        await project_access(project_id, p, session, write=True)
    out = await onboarding.apply_setup(
        ctx, project_id, p.user, connect_local=body.connect_local, base_url=_base(request, ctx)
    )
    async with ctx.db.transaction() as session:
        await audit.record(
            session,
            "project.setup_applied",
            actor_user_id=p.user.id,
            project_id=project_id,
            details={"connected_local": [c["provider_id"] for c in out["connected_local"]]},
        )
    return out
