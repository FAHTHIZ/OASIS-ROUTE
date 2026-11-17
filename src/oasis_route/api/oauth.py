"""Official provider OAuth (PKCE) connection endpoints."""

from __future__ import annotations

import html
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from oasis_route.api.deps import Principal, get_ctx, project_access, require_scope
from oasis_route.connections import oauth
from oasis_route.context import AppContext
from oasis_route.domain.enums import ApiKeyScope
from oasis_route.domain.errors import AppError

router = APIRouter(prefix="/api/v1", tags=["oauth"])
MANAGE = Depends(require_scope(ApiKeyScope.MANAGE))

_PAGE_HEADERS = {
    "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}


class OAuthStartIn(BaseModel):
    provider_id: str = Field(min_length=1, max_length=64)
    accept_terms: bool = False
    display_name: str = Field(default="", max_length=200)
    priority: int = 0
    mode: str = Field(default="redirect", pattern="^(redirect|manual)$")


class OAuthCompleteIn(BaseModel):
    code: str = Field(min_length=1, max_length=2048)


@router.post("/projects/{project_id}/connections/oauth/start", status_code=201)
async def start(
    project_id: str,
    body: OAuthStartIn,
    request: Request,
    response: Response,
    p: Principal = MANAGE,
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Begin an official OAuth connection. Open ``authorization_url`` in the same browser.

    ``redirect`` flows are bound to the calling browser with an HttpOnly cookie, so a link started by one
    person cannot be completed by someone else. API clients without a browser should use ``manual``.
    """
    async with ctx.db.session() as session:
        await project_access(project_id, p, session, write=True)
    out = await oauth.start(
        ctx,
        project_id=project_id,
        user=p.user,
        provider_id=body.provider_id,
        accept_terms=body.accept_terms,
        display_name=body.display_name,
        priority=body.priority,
        redirect=body.mode == "redirect",
        request_base_url=str(request.base_url),
    )
    binding = out.pop("_browser_binding", None)
    if binding:
        response.set_cookie(
            oauth.BINDING_COOKIE,
            binding,
            max_age=int(oauth.FLOW_TTL.total_seconds()),
            path=oauth.CALLBACK_PATH,
            httponly=True,
            secure=oauth.callback_url(ctx, str(request.base_url)).startswith("https://"),
            samesite="lax",  # sent on the provider's top-level redirect back to us
        )
    return out


@router.get("/projects/{project_id}/connections/oauth/{flow_id}")
async def flow_status(
    project_id: str, flow_id: str, p: Principal = MANAGE, ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    async with ctx.db.session() as session:
        await project_access(project_id, p, session, write=True)
    return await oauth.get_flow(ctx, project_id=project_id, flow_id=flow_id)


@router.post("/projects/{project_id}/connections/oauth/{flow_id}/complete")
async def complete(
    project_id: str,
    flow_id: str,
    body: OAuthCompleteIn,
    p: Principal = MANAGE,
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Finish a ``manual`` flow with the code the provider displayed."""
    async with ctx.db.session() as session:
        await project_access(project_id, p, session, write=True)
    return await oauth.complete_manual(ctx, project_id=project_id, flow_id=flow_id, code=body.code)


def _page(title: str, message: str, status: int) -> HTMLResponse:
    body = (
        "<!doctype html><html lang=en><meta charset=utf-8><title>Oasis Route</title>"
        "<body style='font-family:system-ui;max-width:32rem;margin:4rem auto;line-height:1.5'>"
        f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>"
        "<p><a href='/console/'>Return to Oasis Route</a></p></body></html>"
    )
    return HTMLResponse(body, status_code=status, headers=_PAGE_HEADERS)


def _bound_page(title: str, message: str, status: int) -> HTMLResponse:
    page = _page(title, message, status)
    page.delete_cookie(oauth.BINDING_COOKIE, path=oauth.CALLBACK_PATH)
    return page


@router.get("/oauth/callback", response_class=HTMLResponse)
async def callback(
    request: Request,
    ctx: AppContext = Depends(get_ctx),
    code: str | None = Query(default=None, max_length=2048),
    state: str | None = Query(default=None, max_length=512),
    error: str | None = Query(default=None, max_length=200),
) -> HTMLResponse:
    """Provider redirect target. Authenticated by the single-use ``state``, not an Oasis Key."""
    if error or not code or not state:
        return _page("Connection not completed", "The provider did not authorize the connection.", 400)
    try:
        result = await oauth.complete_redirect(ctx, code, state, request.cookies.get(oauth.BINDING_COOKIE))
    except AppError as exc:
        return _page("Connection not completed", exc.message, exc.status_code)
    v = result.get("validation") or {}
    if v.get("status") == "active":
        return _bound_page("Connected", "Your provider is connected and its models are ready to use.", 200)
    return _bound_page(
        "Connected, checking provider",
        "The connection was created. Oasis Route will keep checking it; see the console for status.",
        200,
    )
