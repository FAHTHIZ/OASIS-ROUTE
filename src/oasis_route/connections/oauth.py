"""Official provider OAuth connections (authorization code + PKCE).

Only providers that publish a first-party OAuth flow for third-party apps are listed in ``SPECS``. The user
signs in on the provider's own site and approves; the provider returns a single-use code that Oasis Route
exchanges for a credential scoped to that user's account. Oasis Route never sees the user's password and never
creates accounts.

Security properties:
- ``state`` is 32 random bytes, stored only as a SHA-256 hash, single use, and expires after ``FLOW_TTL``.
- The PKCE ``code_verifier`` never leaves the server and is sealed in the vault until the exchange.
- The exchange endpoint is a constant on the provider's official domain (no user-controlled URL).
- The resulting credential goes through the normal connection path: eligibility, multi-account guard,
  vault sealing, validation and model sync.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select

from oasis_route.connections import service as conns
from oasis_route.context import AppContext
from oasis_route.db.models import Credential, OAuthFlow, Provider, User, utcnow
from oasis_route.domain.enums import AuthMethod
from oasis_route.domain.errors import AppError, NotFound, PolicyViolation, ValidationFailed
from oasis_route.identity import audit
from oasis_route.security.vault import SealedSecret

FLOW_TTL = timedelta(minutes=10)
CALLBACK_PATH = "/api/v1/oauth/callback"


@dataclass(frozen=True)
class OAuthSpec:
    provider_id: str
    authorize_url: str
    exchange_url: str
    docs_url: str


SPECS: dict[str, OAuthSpec] = {
    "openrouter": OAuthSpec(
        provider_id="openrouter",
        authorize_url="https://openrouter.ai/auth",
        exchange_url="https://openrouter.ai/api/v1/auth/keys",
        docs_url="https://openrouter.ai/docs/guides/overview/auth/oauth",
    ),
}


class OAuthFailed(AppError):
    status_code = 502
    code = "oauth_failed"


def _hash_state(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _aad(flow_id: str) -> str:
    return f"oauth_flow:{flow_id}"


def callback_url(ctx: AppContext, request_base_url: str) -> str:
    base = (ctx.settings.public_url or request_base_url).rstrip("/")
    return base + CALLBACK_PATH


async def start(
    ctx: AppContext,
    *,
    project_id: str,
    user: User,
    provider_id: str,
    accept_terms: bool,
    display_name: str,
    priority: int,
    redirect: bool,
    request_base_url: str,
) -> dict[str, Any]:
    spec = SPECS.get(provider_id)
    if spec is None:
        raise ValidationFailed(
            "this provider does not offer an official OAuth flow; connect it with an API key",
            details={"oauth_providers": sorted(SPECS)},
        )
    if not accept_terms:
        raise ValidationFailed(
            "you must confirm you hold this account yourself and accept the provider's terms"
        )
    async with ctx.db.transaction() as session:
        provider = await session.get(Provider, provider_id)
        fresh = await session.get(User, user.id)
        if provider is None or fresh is None:
            raise NotFound("provider not found")
        reasons = conns.eligibility_reasons(fresh, provider, None, AuthMethod.OAUTH.value)
        if reasons:
            raise PolicyViolation("not eligible to connect this provider", details={"reasons": reasons})
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        flow = OAuthFlow(
            state_hash=_hash_state(state),
            project_id=project_id,
            user_id=user.id,
            provider_id=provider_id,
            display_name=display_name or provider.name,
            priority=priority,
            expires_at=utcnow() + FLOW_TTL,
        )
        session.add(flow)
        await session.flush()
        sealed = ctx.vault_.seal({"code_verifier": verifier}, _aad(flow.id))
        cred = Credential(
            kek_id=sealed.kek_id, wrapped_dek=sealed.wrapped_dek, ciphertext=sealed.ciphertext, aad=sealed.aad
        )
        session.add(cred)
        await session.flush()
        flow.verifier_credential_id = cred.id
        params: dict[str, str] = {
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
            "key_label": f"Oasis Route ({flow.display_name})"[:60],
        }
        if redirect:
            params["callback_url"] = callback_url(ctx, request_base_url)
            params["state"] = state
        await audit.record(
            session,
            "connection.oauth_started",
            actor_user_id=user.id,
            project_id=project_id,
            target_type="oauth_flow",
            target_id=flow.id,
            details={"provider": provider_id, "mode": "redirect" if redirect else "manual"},
        )
        return {
            "flow_id": flow.id,
            "provider_id": provider_id,
            "authorization_url": f"{spec.authorize_url}?{urlencode(params)}",
            "mode": "redirect" if redirect else "manual",
            "expires_at": flow.expires_at,
        }


async def _exchange(ctx: AppContext, spec: OAuthSpec, code: str, verifier: str) -> str:
    try:
        resp = await ctx.upstream_client.post(
            spec.exchange_url,
            json={"code": code, "code_verifier": verifier, "code_challenge_method": "S256"},
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        raise OAuthFailed(f"could not reach the provider: {type(exc).__name__}") from exc
    if resp.status_code >= 400:
        raise OAuthFailed(
            "the provider rejected the authorization code (it may have expired or already been used)",
            details={"upstream_status": resp.status_code},
        )
    try:
        key = resp.json().get("key")
    except ValueError:
        key = None
    if not isinstance(key, str) or not key.strip():
        raise OAuthFailed("the provider did not return a credential")
    return key.strip()


async def _finish(ctx: AppContext, flow_id: str, code: str) -> dict[str, Any]:
    """Exchange the code, create the connection, validate it. Each flow can complete once."""
    expired = False
    async with ctx.db.transaction() as session:
        flow = await session.get(OAuthFlow, flow_id, with_for_update=True)
        if flow is None:
            raise NotFound("authorization request not found")
        if flow.status != "pending":
            raise ValidationFailed(f"this authorization request is already {flow.status}")
        if flow.expires_at <= utcnow():
            flow.status, expired = "expired", True
            await _shred(session, flow)
    if expired:  # raised after commit so the verifier is destroyed
        raise ValidationFailed("this authorization request expired; start again")
    async with ctx.db.transaction() as session:
        flow = await session.get(OAuthFlow, flow_id, with_for_update=True)
        if flow is None or flow.status != "pending":
            raise ValidationFailed("this authorization request is no longer usable; start again")
        cred = (
            await session.get(Credential, flow.verifier_credential_id)
            if flow.verifier_credential_id
            else None
        )
        if cred is None:
            raise ValidationFailed("this authorization request is no longer usable; start again")
        verifier = str(
            ctx.vault_.open(
                SealedSecret(
                    kek_id=cred.kek_id, wrapped_dek=cred.wrapped_dek, ciphertext=cred.ciphertext, aad=cred.aad
                ),
                _aad(flow.id),
            )["code_verifier"]
        )
        flow.status = "exchanging"  # claims the flow: a concurrent callback now sees it as used
        await _shred(session, flow)
        snapshot = (flow.project_id, flow.user_id, flow.provider_id, flow.display_name, flow.priority)
    project_id, user_id, provider_id, display_name, priority = snapshot
    spec = SPECS[provider_id]
    try:
        key = await _exchange(ctx, spec, code, verifier)
        async with ctx.db.transaction() as session:
            user = await session.get(User, user_id)
            if user is None or not user.is_active:
                raise PolicyViolation("the user who started this connection is no longer active")
            conn = await conns.create_connection(
                ctx,
                session,
                project_id=project_id,
                user=user,
                data=conns.ConnectionInput(
                    provider_id=provider_id,
                    secret={"api_key": key},
                    display_name=display_name,
                    auth_method=AuthMethod.OAUTH.value,
                    priority=priority,
                    accept_terms=True,
                ),
                oauth_flow=True,
            )
            cid = conn.id
            flow = await session.get(OAuthFlow, flow_id)
            assert flow is not None
            flow.status, flow.connection_id, flow.completed_at = "completed", cid, utcnow()
    except AppError as exc:
        async with ctx.db.transaction() as session:
            flow = await session.get(OAuthFlow, flow_id)
            if flow is not None:
                flow.status, flow.error, flow.completed_at = "failed", exc.message[:300], utcnow()
        raise
    result = await conns.validate(ctx, cid, user_id)
    return {"connection_id": cid, "project_id": project_id, "validation": result.__dict__}


async def _shred(session: Any, flow: OAuthFlow) -> None:
    if flow.verifier_credential_id:
        cred = await session.get(Credential, flow.verifier_credential_id)
        flow.verifier_credential_id = None
        await session.flush()
        if cred is not None:
            await session.delete(cred)


async def complete_redirect(ctx: AppContext, code: str, state: str) -> dict[str, Any]:
    """Browser callback: authenticated by the unguessable, single-use, expiring ``state``."""
    async with ctx.db.session() as session:
        flow = (
            await session.execute(select(OAuthFlow).where(OAuthFlow.state_hash == _hash_state(state)))
        ).scalar_one_or_none()
        if flow is None:
            raise NotFound("authorization request not found")
        flow_id = flow.id
    return await _finish(ctx, flow_id, code)


async def complete_manual(ctx: AppContext, *, project_id: str, flow_id: str, code: str) -> dict[str, Any]:
    """Headless flow: the user pastes the code shown by the provider. Caller is already authorized."""
    async with ctx.db.session() as session:
        flow = await session.get(OAuthFlow, flow_id)
        if flow is None or flow.project_id != project_id:
            raise NotFound("authorization request not found")
    return await _finish(ctx, flow_id, code)


async def get_flow(ctx: AppContext, *, project_id: str, flow_id: str) -> dict[str, Any]:
    async with ctx.db.session() as session:
        flow = await session.get(OAuthFlow, flow_id)
        if flow is None or flow.project_id != project_id:
            raise NotFound("authorization request not found")
        status = flow.status
        if status == "pending" and flow.expires_at <= utcnow():
            status = "expired"
        return {
            "flow_id": flow.id,
            "provider_id": flow.provider_id,
            "status": status,
            "connection_id": flow.connection_id,
            "error": flow.error,
            "expires_at": flow.expires_at,
        }


async def purge_expired(ctx: AppContext) -> int:
    """Worker hook: expire stale flows and destroy their sealed verifiers."""
    n = 0
    async with ctx.db.transaction() as session:
        rows = (
            await session.execute(
                select(OAuthFlow).where(OAuthFlow.status == "pending", OAuthFlow.expires_at <= utcnow())
            )
        ).scalars()
        for flow in rows:
            flow.status = "expired"
            await _shred(session, flow)
            n += 1
    return n
