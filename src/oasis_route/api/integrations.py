"""Client configuration recipes for coding tools and SDKs, filled in with this server's URL."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from oasis_route import integrations
from oasis_route.api.deps import Principal, get_ctx, require_scope
from oasis_route.context import AppContext
from oasis_route.domain.enums import ApiKeyScope
from oasis_route.domain.errors import NotFound

router = APIRouter(prefix="/api/v1", tags=["integrations"])
READ = Depends(require_scope(ApiKeyScope.READ))


def _base(request: Request, ctx: AppContext) -> str:
    return (ctx.settings.public_url or str(request.base_url)).rstrip("/")


@router.get("/integrations")
async def list_integrations(
    request: Request, p: Principal = READ, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    """Paste-ready settings for Claude Code, Codex, Cursor, Cline, Roo Code, Aider, OpenCode and SDKs."""
    return {"base_url": _base(request, ctx), "integrations": integrations.recipes(_base(request, ctx))}


@router.get("/integrations/{tool}")
async def get_integration(
    tool: str, request: Request, p: Principal = READ, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    r = integrations.recipe(tool, _base(request, ctx))
    if r is None:
        raise NotFound(f"unknown tool '{tool}'")
    r["text"] = integrations.render(r)
    return r
