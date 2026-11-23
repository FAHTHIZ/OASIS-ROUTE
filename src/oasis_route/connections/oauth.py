"""Official provider OAuth connections (authorization code + PKCE).

Two documented flow kinds are supported:

- ``key_exchange`` (OpenRouter, https://openrouter.ai/docs/guides/overview/auth/oauth): the code is exchanged
  for a user-scoped API key. No client registration is needed.
- ``oauth2_pkce`` (Hugging Face, https://huggingface.co/docs/hub/oauth): standard OAuth 2.0 authorization
  code with PKCE for a *public* client. The access token is scoped to ``inference-api`` (Inference Providers
  on the user's behalf) and refreshed by the worker with the documented ``refresh_token`` grant. The client
  id is either an operator-registered app (``OASIS_ROUTE_HF_OAUTH_CLIENT_ID``) or, when ``public_url`` is
  https, this deployment's own Client ID Metadata Document served at ``/.well-known/oauth-cimd``.

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
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select

from oasis_route.connections import service as conns
from oasis_route.context import AppContext
from oasis_route.db.models import Credential, OAuthFlow, Provider, User, utcnow
from oasis_route.domain.enums import AuthMethod
from oasis_route.domain.errors import AppError, Forbidden, NotFound, PolicyViolation, ValidationFailed
from oasis_route.identity import audit
from oasis_route.security.vault import SealedSecret

FLOW_TTL = timedelta(minutes=10)
DEVICE_TTL_MAX = timedelta(minutes=15)
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
CALLBACK_PATH = "/api/v1/oauth/callback"
BINDING_COOKIE = "oasis_oauth_binding"


CIMD_PATH = "/.well-known/oauth-cimd"
REFRESH_MARGIN = timedelta(minutes=30)


@dataclass(frozen=True)
class OAuthSpec:
    provider_id: str
    authorize_url: str
    exchange_url: str
    docs_url: str
    kind: str = "key_exchange"  # key_exchange | oauth2_pkce
    scope: str = ""
    client_id_setting: str | None = None  # Settings attribute holding a registered client id
    supports_cimd: bool = False  # provider accepts a Client ID Metadata Document URL as client_id
    manual_mode: bool = True  # provider can display the code for headless completion
    device_url: str | None = None  # RFC 8628 device authorization endpoint (sign in on another device)


SPECS: dict[str, OAuthSpec] = {
    "openrouter": OAuthSpec(
        provider_id="openrouter",
        authorize_url="https://openrouter.ai/auth",
        exchange_url="https://openrouter.ai/api/v1/auth/keys",
        docs_url="https://openrouter.ai/docs/guides/overview/auth/oauth",
    ),
    # https://huggingface.co/.well-known/openid-configuration (checked 2026-11-17): S256 PKCE, refresh_token
    # grant, client_id_metadata_document_supported. read-billing exposes only whether a payment method exists.
    "huggingface": OAuthSpec(
        provider_id="huggingface",
        authorize_url="https://huggingface.co/oauth/authorize",
        exchange_url="https://huggingface.co/oauth/token",
        docs_url="https://huggingface.co/docs/hub/oauth",
        kind="oauth2_pkce",
        scope="openid profile read-billing inference-api",
        client_id_setting="hf_oauth_client_id",
        supports_cimd=True,
        manual_mode=False,
        device_url="https://huggingface.co/oauth/device",
    ),
}


def client_id_for(ctx: AppContext, spec: OAuthSpec) -> str | None:
    """The OAuth client id to use, or ``None`` when this deployment cannot use the provider's flow."""
    if spec.kind != "oauth2_pkce":
        return ""
    configured = str(getattr(ctx.settings, spec.client_id_setting or "", "") or "").strip()
    if configured:
        return configured
    public = (ctx.settings.public_url or "").rstrip("/")
    if spec.supports_cimd and public.startswith("https://"):
        return public + CIMD_PATH
    return None


def available(ctx: AppContext, provider_id: str) -> bool:
    spec = SPECS.get(provider_id)
    return spec is not None and client_id_for(ctx, spec) is not None


def cimd_document(ctx: AppContext) -> dict[str, Any] | None:
    """Client ID Metadata Document for this deployment (public client, no secret)."""
    public = (ctx.settings.public_url or "").rstrip("/")
    if not public.startswith("https://"):
        return None
    return {
        "client_id": public + CIMD_PATH,
        "client_name": "Oasis Route",
        "client_uri": public + "/console/",
        "redirect_uris": [public + CALLBACK_PATH],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": SPECS["huggingface"].scope,
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
    device: bool = False,
) -> dict[str, Any]:
    spec = SPECS.get(provider_id)
    if spec is None:
        raise ValidationFailed(
            "this provider does not offer an official OAuth flow; connect it with an API key",
            details={"oauth_providers": sorted(k for k in SPECS if available(ctx, k))},
        )
    client_id = client_id_for(ctx, spec)
    if client_id is None:
        raise ValidationFailed(
            f"{provider_id} sign-in needs a registered OAuth app or an https public_url on this server; "
            "connect it with an API key instead",
            details={
                "setting": f"OASIS_ROUTE_{(spec.client_id_setting or '').upper()}",
                "docs": spec.docs_url,
            },
        )
    if device and not spec.device_url:
        raise ValidationFailed(f"{provider_id} does not offer device-code sign-in")
    if device:
        redirect = False
    elif not redirect and not spec.manual_mode:
        raise ValidationFailed(f"{provider_id} sign-in only supports the browser redirect mode")
    if not accept_terms:
        raise ValidationFailed(
            "you must confirm you hold this account yourself and accept the provider's terms"
        )
    async with ctx.db.session() as session:
        provider = await session.get(Provider, provider_id)
        fresh = await session.get(User, user.id)
        if provider is None or fresh is None:
            raise NotFound("provider not found")
        reasons = conns.eligibility_reasons(fresh, provider, None, AuthMethod.OAUTH.value)
        if reasons:
            raise PolicyViolation("not eligible to connect this provider", details={"reasons": reasons})
    provider_name = provider.name
    if device:
        return await _start_device(
            ctx,
            spec,
            client_id=str(client_id),
            project_id=project_id,
            user=user,
            display_name=display_name or provider_name,
            priority=priority,
        )
    async with ctx.db.transaction() as session:
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        binding = secrets.token_urlsafe(32) if redirect else None
        flow = OAuthFlow(
            state_hash=_hash_state(state),
            browser_binding_hash=_hash_state(binding) if binding else None,
            project_id=project_id,
            user_id=user.id,
            provider_id=provider_id,
            display_name=display_name or provider_name,
            priority=priority,
            expires_at=utcnow() + FLOW_TTL,
        )
        session.add(flow)
        await session.flush()
        redirect_uri = callback_url(ctx, request_base_url)
        sealed = ctx.vault_.seal(
            {"code_verifier": verifier, "redirect_uri": redirect_uri, "client_id": client_id}, _aad(flow.id)
        )
        cred = Credential(
            kek_id=sealed.kek_id, wrapped_dek=sealed.wrapped_dek, ciphertext=sealed.ciphertext, aad=sealed.aad
        )
        session.add(cred)
        await session.flush()
        flow.verifier_credential_id = cred.id
        params: dict[str, str]
        if spec.kind == "oauth2_pkce":
            params = {
                "response_type": "code",
                "client_id": str(client_id),
                "redirect_uri": redirect_uri,
                "scope": spec.scope,
                "state": state,
                "code_challenge": _challenge(verifier),
                "code_challenge_method": "S256",
            }
        else:
            params = {
                "code_challenge": _challenge(verifier),
                "code_challenge_method": "S256",
                "key_label": f"Oasis Route ({flow.display_name})"[:60],
            }
            if redirect:
                params["callback_url"] = redirect_uri
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
            "_browser_binding": binding,  # set as an HttpOnly cookie by the API layer, never in the body
        }


async def _start_device(
    ctx: AppContext,
    spec: OAuthSpec,
    *,
    client_id: str,
    project_id: str,
    user: User,
    display_name: str,
    priority: int,
) -> dict[str, Any]:
    """RFC 8628: ask the provider for a user code the person enters on any device they are signed in on."""
    assert spec.device_url
    try:
        resp = await ctx.upstream_client.post(
            spec.device_url,
            data={"client_id": client_id, "scope": spec.scope},
            headers={"accept": "application/json"},
            timeout=20.0,
        )
        body = resp.json() if resp.status_code < 400 else None
    except (httpx.HTTPError, ValueError) as exc:
        raise OAuthFailed(f"could not reach the provider: {type(exc).__name__}") from exc
    if not isinstance(body, dict) or not all(
        isinstance(body.get(k), str) and body.get(k) for k in ("device_code", "user_code", "verification_uri")
    ):
        raise OAuthFailed(
            "the provider did not start device sign-in", details={"upstream_status": resp.status_code}
        )
    verification_uri = str(body["verification_uri"])
    complete_uri = body.get("verification_uri_complete")
    official = spec.device_url.split("/", 3)[:3]
    for uri in (verification_uri, complete_uri):  # never send a user to a host the provider did not own
        if isinstance(uri, str) and uri.split("/", 3)[:3] != official:
            raise OAuthFailed("the provider returned an unexpected sign-in address")
    interval = _bounded_int(body.get("interval"), 5, 1, 60)
    ttl = min(timedelta(seconds=_bounded_int(body.get("expires_in"), 900, 60, 3600)), DEVICE_TTL_MAX)
    async with ctx.db.transaction() as session:
        flow = OAuthFlow(
            state_hash=_hash_state(secrets.token_urlsafe(32)),  # unused: device flows have no callback
            project_id=project_id,
            user_id=user.id,
            provider_id=spec.provider_id,
            display_name=display_name,
            priority=priority,
            expires_at=utcnow() + ttl,
        )
        session.add(flow)
        await session.flush()
        sealed = ctx.vault_.seal(
            {"device_code": body["device_code"], "client_id": client_id, "interval": interval}, _aad(flow.id)
        )
        cred = Credential(
            kek_id=sealed.kek_id, wrapped_dek=sealed.wrapped_dek, ciphertext=sealed.ciphertext, aad=sealed.aad
        )
        session.add(cred)
        await session.flush()
        flow.verifier_credential_id = cred.id
        await audit.record(
            session,
            "connection.oauth_started",
            actor_user_id=user.id,
            project_id=project_id,
            target_type="oauth_flow",
            target_id=flow.id,
            details={"provider": spec.provider_id, "mode": "device"},
        )
        return {
            "flow_id": flow.id,
            "provider_id": spec.provider_id,
            "mode": "device",
            "user_code": str(body["user_code"]),
            "verification_uri": verification_uri,
            "verification_uri_complete": complete_uri if isinstance(complete_uri, str) else None,
            "authorization_url": complete_uri if isinstance(complete_uri, str) else verification_uri,
            "interval": interval,
            "expires_at": flow.expires_at,
            "_browser_binding": None,
        }


def _bounded_int(v: Any, default: int, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return default
    return max(lo, min(hi, int(v)))


async def poll_device(ctx: AppContext, *, project_id: str, flow_id: str) -> dict[str, Any]:
    """Ask the provider once whether the user approved a device flow; connect on approval.

    Callers poll no faster than the returned ``interval``; ``slow_down`` from the provider raises it.
    """
    async with ctx.db.session() as session:
        flow = await session.get(OAuthFlow, flow_id)
        if flow is None or flow.project_id != project_id:
            raise NotFound("authorization request not found")
        settled = flow.status != "pending"
        cred = (
            await session.get(Credential, flow.verifier_credential_id)
            if flow.verifier_credential_id
            else None
        )
        expired = flow.expires_at <= utcnow()
        sealed = (
            dict(
                ctx.vault_.open(
                    SealedSecret(
                        kek_id=cred.kek_id,
                        wrapped_dek=cred.wrapped_dek,
                        ciphertext=cred.ciphertext,
                        aad=cred.aad,
                    ),
                    _aad(flow.id),
                )
            )
            if cred is not None and not expired and not settled
            else None
        )
        spec = SPECS[flow.provider_id]
    if settled:
        return await get_flow(ctx, project_id=project_id, flow_id=flow_id)
    if sealed is None or "device_code" not in sealed:
        if expired:
            await _fail(ctx, flow_id, "expired", None)
            return await get_flow(ctx, project_id=project_id, flow_id=flow_id)
        raise ValidationFailed("this authorization request is not a device sign-in")
    interval = int(sealed.get("interval") or 5)
    try:
        resp = await ctx.upstream_client.post(
            spec.exchange_url,
            data={
                "grant_type": DEVICE_GRANT,
                "device_code": str(sealed["device_code"]),
                "client_id": str(sealed.get("client_id") or ""),
            },
            headers={"accept": "application/json"},
            timeout=20.0,
        )
    except httpx.HTTPError:
        return {"flow_id": flow_id, "status": "pending", "interval": interval}  # transient (RFC 8628 3.5)
    if resp.status_code >= 400:
        try:
            err = str((resp.json() or {}).get("error") or "")
        except (ValueError, AttributeError):
            err = ""
        if err == "slow_down":
            interval = min(interval + 5, 60)
            await _store_interval(ctx, flow_id, sealed, interval)
            return {"flow_id": flow_id, "status": "pending", "interval": interval}
        if err == "authorization_pending" or resp.status_code >= 500 or resp.status_code == 429:
            return {"flow_id": flow_id, "status": "pending", "interval": interval}
        reason = {
            "access_denied": "sign-in was declined on the provider's site",
            "expired_token": "the code expired before it was approved; start again",
        }.get(err, "the provider refused the sign-in")
        await _fail(ctx, flow_id, "expired" if err == "expired_token" else "failed", reason)
        return await get_flow(ctx, project_id=project_id, flow_id=flow_id)
    secret = _token_secret(spec, resp, client_id=str(sealed.get("client_id") or ""))
    if secret is None:
        await _fail(ctx, flow_id, "failed", "the provider did not return a credential")
        return await get_flow(ctx, project_id=project_id, flow_id=flow_id)
    snapshot = await _claim(ctx, flow_id)
    out = await _create(ctx, flow_id, snapshot, secret)
    return {**await get_flow(ctx, project_id=project_id, flow_id=flow_id), "validation": out["validation"]}


async def _store_interval(ctx: AppContext, flow_id: str, sealed: dict[str, Any], interval: int) -> None:
    async with ctx.db.transaction() as session:
        flow = await session.get(OAuthFlow, flow_id)
        if flow is None or not flow.verifier_credential_id:
            return
        cred = await session.get(Credential, flow.verifier_credential_id)
        if cred is None:
            return
        new = ctx.vault_.seal({**sealed, "interval": interval}, _aad(flow.id))
        cred.kek_id, cred.wrapped_dek, cred.ciphertext, cred.aad = (
            new.kek_id,
            new.wrapped_dek,
            new.ciphertext,
            new.aad,
        )


async def _fail(ctx: AppContext, flow_id: str, status: str, error: str | None) -> None:
    async with ctx.db.transaction() as session:
        flow = await session.get(OAuthFlow, flow_id, with_for_update=True)
        if flow is None or flow.status != "pending":
            return
        flow.status, flow.error, flow.completed_at = status, (error or None), utcnow()
        await _shred(session, flow)


async def _exchange(ctx: AppContext, spec: OAuthSpec, code: str, sealed: dict[str, Any]) -> dict[str, Any]:
    """Exchange the single-use code for the secret stored in the vault (``api_key`` + optional refresh)."""
    verifier = str(sealed["code_verifier"])
    try:
        if spec.kind == "oauth2_pkce":
            resp = await ctx.upstream_client.post(
                spec.exchange_url,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": str(sealed.get("redirect_uri") or ""),
                    "client_id": str(sealed.get("client_id") or ""),
                    "code_verifier": verifier,
                },
                headers={"accept": "application/json"},
                timeout=20.0,
            )
        else:
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
    secret = _token_secret(spec, resp, client_id=str(sealed.get("client_id") or ""))
    if secret is None:
        raise OAuthFailed("the provider did not return a credential")
    return secret


def _token_secret(spec: OAuthSpec, resp: httpx.Response, *, client_id: str) -> dict[str, Any] | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    if spec.kind != "oauth2_pkce":
        key = body.get("key")
        return {"api_key": key.strip()} if isinstance(key, str) and key.strip() else None
    token = body.get("access_token")
    if not isinstance(token, str) or not token.strip():
        return None
    out: dict[str, Any] = {"api_key": token.strip(), "oauth": spec.provider_id, "client_id": client_id}
    refresh = body.get("refresh_token")
    if isinstance(refresh, str) and refresh:
        out["refresh_token"] = refresh
    expires_in = body.get("expires_in")
    if isinstance(expires_in, int | float) and not isinstance(expires_in, bool) and expires_in > 0:
        out["expires_at"] = (utcnow() + timedelta(seconds=float(expires_in))).isoformat()
    return out


async def _finish(ctx: AppContext, flow_id: str, code: str) -> dict[str, Any]:
    """Exchange the code, create the connection, validate it. Each flow can complete once."""
    sealed_flow = await _open_for_claim(ctx, flow_id)
    if "device_code" in sealed_flow:
        raise ValidationFailed("this is a device sign-in; it completes by polling, not with a code")
    snapshot = await _claim(ctx, flow_id)
    spec = SPECS[snapshot[2]]
    try:
        secret = await _exchange(ctx, spec, code, sealed_flow)
    except AppError as exc:
        await _mark_failed(ctx, flow_id, exc.message)
        raise
    return await _create(ctx, flow_id, snapshot, secret)


async def _open_for_claim(ctx: AppContext, flow_id: str) -> dict[str, Any]:
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
        else:
            cred = (
                await session.get(Credential, flow.verifier_credential_id)
                if flow.verifier_credential_id
                else None
            )
            if cred is None:
                raise ValidationFailed("this authorization request is no longer usable; start again")
            return dict(
                ctx.vault_.open(
                    SealedSecret(
                        kek_id=cred.kek_id,
                        wrapped_dek=cred.wrapped_dek,
                        ciphertext=cred.ciphertext,
                        aad=cred.aad,
                    ),
                    _aad(flow.id),
                )
            )
    assert expired  # raised after commit so the verifier is destroyed
    raise ValidationFailed("this authorization request expired; start again")


async def _claim(ctx: AppContext, flow_id: str) -> tuple[str, str, str, str, int]:
    """Mark the flow as used and destroy its sealed verifier; a concurrent completion now fails."""
    async with ctx.db.transaction() as session:
        flow = await session.get(OAuthFlow, flow_id, with_for_update=True)
        if flow is None or flow.status != "pending":
            raise ValidationFailed("this authorization request is no longer usable; start again")
        flow.status = "exchanging"
        await _shred(session, flow)
        return (flow.project_id, flow.user_id, flow.provider_id, flow.display_name, flow.priority)


async def _mark_failed(ctx: AppContext, flow_id: str, message: str) -> None:
    async with ctx.db.transaction() as session:
        flow = await session.get(OAuthFlow, flow_id)
        if flow is not None:
            flow.status, flow.error, flow.completed_at = "failed", message[:300], utcnow()


async def _create(
    ctx: AppContext, flow_id: str, snapshot: tuple[str, str, str, str, int], secret: dict[str, Any]
) -> dict[str, Any]:
    project_id, user_id, provider_id, display_name, priority = snapshot
    try:
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
                    secret=secret,
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
        await _mark_failed(ctx, flow_id, exc.message)
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


async def complete_redirect(ctx: AppContext, code: str, state: str, binding: str | None) -> dict[str, Any]:
    """Browser callback: authenticated by the unguessable, single-use, expiring ``state`` plus the
    HttpOnly binding cookie of the browser that started the flow."""
    async with ctx.db.session() as session:
        flow = (
            await session.execute(select(OAuthFlow).where(OAuthFlow.state_hash == _hash_state(state)))
        ).scalar_one_or_none()
        if flow is None:
            raise NotFound("authorization request not found")
        expected = flow.browser_binding_hash
        if expected is None or not binding or not hmac.compare_digest(expected, _hash_state(binding)):
            raise Forbidden(
                "finish the connection in the same browser where you started it, or use manual mode"
            )
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


async def refresh_tokens(ctx: AppContext) -> dict[str, int]:
    """Worker hook: refresh OAuth access tokens that expire within ``REFRESH_MARGIN``.

    Uses only the provider's documented ``refresh_token`` grant. A rejected refresh suspends the connection
    and notifies the owner (they reconnect); it is never retried in a loop.
    """
    from oasis_route.db.models import Connection
    from oasis_route.domain.enums import ConnectionStatus, ErrorCategory

    due: list[tuple[str, OAuthSpec, dict[str, Any]]] = []
    async with ctx.db.session() as session:
        rows = (
            await session.execute(
                select(Connection).where(
                    Connection.auth_method == AuthMethod.OAUTH.value,
                    Connection.status.in_(
                        [ConnectionStatus.ACTIVE.value, ConnectionStatus.PENDING_VALIDATION.value]
                    ),
                    Connection.provider_id.in_([p for p, sp in SPECS.items() if sp.kind == "oauth2_pkce"]),
                )
            )
        ).scalars()
        for conn in rows:
            spec = SPECS[conn.provider_id]
            try:
                secret = conns.open_secret(ctx, conn, conn.credential)
            except Exception:  # noqa: S112 - unreadable credential is reported by validation, not here
                continue
            exp = secret.get("expires_at")
            if not secret.get("refresh_token") or not isinstance(exp, str):
                continue
            try:
                expires_at = datetime.fromisoformat(exp)
            except ValueError:
                continue
            if expires_at - utcnow() <= REFRESH_MARGIN:
                due.append((conn.id, spec, secret))
    refreshed = failed = 0
    for conn_id, spec, secret in due:
        try:
            resp = await ctx.upstream_client.post(
                spec.exchange_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": str(secret["refresh_token"]),
                    "client_id": str(secret.get("client_id") or ""),
                },
                headers={"accept": "application/json"},
                timeout=20.0,
            )
            new = (
                _token_secret(spec, resp, client_id=str(secret.get("client_id") or ""))
                if resp.status_code < 400
                else None
            )
        except httpx.HTTPError:
            continue  # transient: try again on the next run
        if new is None:
            failed += 1
            await conns.suspend_for_auth(
                ctx,
                conn_id,
                ErrorCategory.AUTH_EXPIRED,
                "the provider did not refresh the sign-in; reconnect",
            )
            continue
        new.setdefault("refresh_token", secret["refresh_token"])  # providers may not rotate refresh tokens
        async with ctx.db.transaction() as session:
            target = await session.get(Connection, conn_id)
            if target is None:
                continue
            status = target.status
            await conns.rotate_credential(ctx, session, target, new, actor_id="system:oauth_refresh")
            target.status = status  # same account and grant: keep serving without a revalidation gap
        refreshed += 1
    return {"due": len(due), "refreshed": refreshed, "failed": failed}
