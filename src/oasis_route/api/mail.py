"""Mail API: forwarded provider notices and the optional, compliance-gated Gmail integration."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oasis_route.api.deps import Principal, get_ctx, get_session, require_scope
from oasis_route.context import AppContext
from oasis_route.db.models import MailGrant
from oasis_route.domain.enums import ApiKeyScope
from oasis_route.domain.errors import ValidationFailed
from oasis_route.notifications import mail

router = APIRouter(prefix="/api/v1/mail", tags=["mail"])
READ = Depends(require_scope(ApiKeyScope.READ))
MANAGE = Depends(require_scope(ApiKeyScope.MANAGE))


@router.get("/status")
async def status(
    p: Principal = READ, ctx: AppContext = Depends(get_ctx), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    ok, reason = mail.gmail_gate(ctx.settings)
    grants = (await session.execute(select(MailGrant).where(MailGrant.user_id == p.user.id))).scalars()
    return {
        "forwarding_enabled": ctx.settings.mail_forwarding_enabled,
        "gmail": {
            "available": ok,
            "reason": reason,
            "scopes": list(mail.SCOPES),
            "consent_version": mail.CONSENT_VERSION,
            "consent_text": mail.CONSENT_TEXT,
        },
        "retention_days": ctx.settings.mail_facts_retention_days,
        "grants": [
            {
                "id": g.id,
                "status": g.status,
                "scopes": g.scopes,
                "consent_version": g.consent_version,
                "consented_at": g.consented_at,
                "last_sync_at": g.last_sync_at,
                "revoked_at": g.revoked_at,
            }
            for g in grants
        ],
    }


class ForwardedIn(BaseModel):
    message: str = Field(min_length=1, description="Raw RFC 822 message or a pasted forward. Never stored.")


@router.post("/forwarded")
async def forwarded(
    body: ForwardedIn,
    p: Principal = READ,
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await mail.ingest_forwarded(ctx, session, p.user, body.message)


@router.delete("/data")
async def purge(p: Principal = MANAGE, session: AsyncSession = Depends(get_session)) -> dict[str, int]:
    """Delete every mail-derived notice for the caller."""
    return {"deleted": await mail.purge_mail_data(session, p.user.id)}


class ConsentIn(BaseModel):
    accept: bool
    consent_version: str


@router.post("/gmail/consent", status_code=201)
async def gmail_consent(
    body: ConsentIn,
    p: Principal = MANAGE,
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if not body.accept:
        raise ValidationFailed("explicit consent is required")
    return await mail.start_consent(ctx, session, p.user, body.consent_version)


@router.get("/gmail/callback", response_class=HTMLResponse, include_in_schema=False)
async def gmail_callback(
    code: str = Query(min_length=1, max_length=2048),
    state: str = Query(min_length=1, max_length=2048),
    ctx: AppContext = Depends(get_ctx),
) -> HTMLResponse:
    await mail.complete_oauth(ctx, code, state)
    return HTMLResponse(
        "<!doctype html><title>Oasis Route</title><p>Gmail connected with metadata-only access. "
        "You can close this window.</p>"
    )


@router.post("/gmail/grants/{grant_id}/sync")
async def gmail_sync(
    grant_id: str,
    p: Principal = MANAGE,
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    g = await session.get(MailGrant, grant_id)
    if g is None or g.user_id != p.user.id:
        from oasis_route.domain.errors import NotFound

        raise NotFound("grant not found")
    await session.commit()
    return await mail.sync_grant(ctx, grant_id)


@router.delete("/gmail/grants/{grant_id}")
async def gmail_revoke(
    grant_id: str,
    purge: bool = Query(default=False),
    p: Principal = MANAGE,
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    return await mail.revoke_grant(ctx, p.user, grant_id, purge=purge)
