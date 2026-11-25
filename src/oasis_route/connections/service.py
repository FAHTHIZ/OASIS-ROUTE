"""Provider connections: eligibility gate, sealed credentials, validation, model sync.

Hard boundaries enforced here (AGENT_HANDOFF.md, SECURITY_AND_COMPLIANCE.md):
- A connection exists only because the account owner supplied their own credential (or an offer explicitly
  permits automated anonymous access). We never create provider accounts.
- Eligibility (age, country, offer state) is checked against the user's declared profile; unknown facts that a
  provider restricts on are treated as ineligible, not as passing.
- A user may not attach several accounts of the same provider unless the provider explicitly permits it —
  multi-account rotation is a quota-evasion pattern.
- Credentials are sealed with AES-GCM bound to ``connection:<id>`` and only opened for the duration of a call.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oasis_route.adapters.base import AccountStatus, ConnectionContext, ModelInfo
from oasis_route.connections.access import access_for
from oasis_route.context import AppContext
from oasis_route.db.models import (
    Connection,
    Credential,
    Endpoint,
    Model,
    Offer,
    Provider,
    QuotaPool,
    User,
    utcnow,
)
from oasis_route.domain.enums import (
    AuthMethod,
    ConnectionStatus,
    ErrorCategory,
    OfferState,
    QuotaPeriod,
    QuotaSource,
    QuotaUnit,
)
from oasis_route.domain.errors import Conflict, NotFound, PolicyViolation, UpstreamError, ValidationFailed
from oasis_route.identity import audit
from oasis_route.notifications.service import notify
from oasis_route.quota.ledger import FREE_MODELS_SCOPE
from oasis_route.registry.catalog import LOCAL_SERVER_IDS
from oasis_route.security.ssrf import UnsafeUrlError, assert_safe_url, resolves_private_only
from oasis_route.security.vault import SealedSecret

# Model ids that are clearly not chat-completion models; synced but marked non-routable for chat.
_NON_CHAT = re.compile(
    r"(embed|whisper|tts|dall-e|moderation|image|audio|transcri|rerank|speech|guard|realtime)", re.I
)
CONNECTABLE_OFFER_STATES = {OfferState.CONNECTABLE.value, OfferState.ACTIVE.value}

_LOCAL_PROVIDERS = {"custom", "mock", "mock_alt"}


def aad_for(connection_id: str) -> str:
    return f"connection:{connection_id}"


def _rule_reasons(user: User, rules: dict[str, Any], prefix: str) -> list[str]:
    reasons: list[str] = []
    min_age = rules.get("min_age")
    if min_age:
        if user.declared_age is None:
            reasons.append(f"{prefix}age_undeclared: provider requires age >= {min_age}")
        elif user.declared_age < int(min_age):
            reasons.append(f"{prefix}under_min_age: provider requires age >= {min_age}")
    allowed = [c.upper() for c in rules.get("allowed_countries") or []]
    blocked = [c.upper() for c in rules.get("blocked_countries") or []]
    if allowed or blocked:
        country = (user.country or "").upper()
        if not country:
            reasons.append(f"{prefix}country_undeclared: provider restricts by country")
        elif allowed and country not in allowed:
            reasons.append(f"{prefix}country_not_supported")
        elif country in blocked:
            reasons.append(f"{prefix}country_blocked")
    return reasons


def eligibility_reasons(user: User, provider: Provider, offer: Offer | None, auth_method: str) -> list[str]:
    """Reasons this user may not connect. Empty list == eligible."""
    reasons: list[str] = []
    if provider.status != "active":
        reasons.append(f"provider_inactive: {provider.status}")
    reasons += _rule_reasons(
        user,
        {
            "min_age": provider.min_age,
            "allowed_countries": provider.allowed_countries,
            "blocked_countries": provider.blocked_countries,
        },
        "",
    )
    if offer is not None:
        if offer.provider_id != provider.id:
            reasons.append("offer_provider_mismatch")
        if offer.state not in CONNECTABLE_OFFER_STATES:
            reasons.append(f"offer_not_connectable: {offer.state}")
        if offer.expires_at is not None and offer.expires_at <= utcnow():
            reasons.append("offer_expired")
        reasons += _rule_reasons(user, offer.eligibility or {}, "offer_")
    if auth_method == AuthMethod.NONE.value:
        if not provider.automated_access_permitted:
            reasons.append("automated_access_not_permitted")
        if offer is None or offer.state != OfferState.ACTIVE.value:
            reasons.append("anonymous_access_requires_active_offer")
        elif not (offer.eligibility or {}).get("automated_access"):
            reasons.append("offer_does_not_permit_automated_anonymous_access")
    return reasons


@dataclass
class ConnectionInput:
    provider_id: str
    secret: dict[str, Any] | None
    display_name: str = ""
    upstream_account_ref: str = "default"
    auth_method: str = AuthMethod.API_KEY.value
    base_url: str | None = None
    offer_id: str | None = None
    priority: int = 0
    allowed_models: list[str] | None = None
    accept_terms: bool = False
    billing_mode: str | None = None  # free_tier | paid | unknown; None == provider default (see access.py)


async def _validate_local_server_url(ctx: AppContext, base_url: str) -> None:
    """Local presets are priced at zero, so they must really be local: a public URL is refused."""
    if await resolves_private_only(base_url, resolver=ctx.resolver):
        return
    raise PolicyViolation(
        "local server presets must point at a private or loopback address; use the 'custom' provider "
        "for remote servers",
        details={"base_url": base_url},
    )


def _validate_base_url_scope(provider: Provider, base_url: str) -> None:
    """Only user-defined providers may point anywhere; known providers must stay on official domains."""
    if provider.id in _LOCAL_PROVIDERS or provider.id in LOCAL_SERVER_IDS:
        return
    host = (urlsplit(base_url).hostname or "").lower()
    domains = [d.lower() for d in provider.official_domains or []]
    if not any(host == d or host.endswith("." + d) for d in domains):
        raise PolicyViolation(
            "base_url must be on the provider's official domains",
            details={"official_domains": domains},
        )


async def check_url(ctx: AppContext, url: str) -> None:
    try:
        await assert_safe_url(url, allow_private=ctx.settings.allow_private_upstreams, resolver=ctx.resolver)
    except UnsafeUrlError as exc:
        raise PolicyViolation(f"upstream URL rejected: {exc}") from exc


async def create_connection(
    ctx: AppContext,
    session: AsyncSession,
    *,
    project_id: str,
    user: User,
    data: ConnectionInput,
    oauth_flow: bool = False,
) -> Connection:
    provider = await session.get(Provider, data.provider_id)
    if provider is None:
        raise NotFound("provider not found")
    offer = await session.get(Offer, data.offer_id) if data.offer_id else None
    if data.offer_id and offer is None:
        raise NotFound("offer not found")
    fresh_user = await session.get(User, user.id)
    assert fresh_user is not None
    reasons = eligibility_reasons(fresh_user, provider, offer, data.auth_method)
    if reasons:
        raise PolicyViolation("not eligible to connect this provider", details={"reasons": reasons})
    if provider.id in LOCAL_SERVER_IDS and data.auth_method == AuthMethod.API_KEY.value:
        # Local servers ignore the key; store an empty one so no Authorization header is sent.
        data.secret = data.secret or {"api_key": ""}
        if data.base_url:
            await _validate_local_server_url(ctx, data.base_url)
    if data.auth_method == AuthMethod.API_KEY.value and provider.id not in LOCAL_SERVER_IDS:
        if not data.secret or not str(data.secret.get("api_key") or "").strip():
            raise ValidationFailed("api_key is required for auth_method=api_key")
        if not data.accept_terms:
            raise ValidationFailed(
                "you must confirm you hold this account yourself and accept the provider's terms",
                details={"terms_url": provider.terms_url},
            )
    elif data.auth_method == AuthMethod.OAUTH.value:
        if not oauth_flow:
            raise ValidationFailed(
                "OAuth connections are created through the provider OAuth flow",
                details={"start": f"/api/v1/projects/{project_id}/connections/oauth/start"},
            )
        if not data.secret:
            raise ValidationFailed("OAuth flow did not produce a credential")

    # Multi-account guard: one upstream account per provider per user unless the provider allows more.
    existing = (
        (
            await session.execute(
                select(Connection).where(
                    Connection.provider_id == provider.id,
                    Connection.status != ConnectionStatus.REVOKED.value,
                    (Connection.project_id == project_id) | (Connection.created_by == fresh_user.id),
                )
            )
        )
        .scalars()
        .all()
    )
    for c in existing:
        if c.project_id == project_id and c.upstream_account_ref == data.upstream_account_ref:
            raise Conflict("a connection for this provider account already exists in this project")
        if c.upstream_account_ref != data.upstream_account_ref and not provider.multi_account_permitted:
            raise PolicyViolation(
                "this provider does not permit multiple accounts per user; connecting another account "
                "to extend free quota is not allowed",
                details={"provider": provider.id},
            )

    endpoint_id: str | None = None
    if data.base_url:
        _validate_base_url_scope(provider, data.base_url)
        await check_url(ctx, data.base_url)
        ep = Endpoint(
            provider_id=provider.id, base_url=data.base_url, protocol="openai_chat", is_default=False
        )
        if provider.adapter_key == "anthropic":
            ep.protocol = "anthropic_messages"
        session.add(ep)
        await session.flush()
        endpoint_id = ep.id
    else:
        default = (
            await session.execute(
                select(Endpoint).where(Endpoint.provider_id == provider.id, Endpoint.is_default.is_(True))
            )
        ).scalar_one_or_none()
        if default is None:
            raise ValidationFailed("base_url is required for this provider")
        endpoint_id = default.id

    conn_id = str(uuid.uuid4())
    cred_id: str | None = None
    if data.secret:
        sealed = ctx.vault_.seal(data.secret, aad_for(conn_id))
        cred = Credential(
            kek_id=sealed.kek_id, wrapped_dek=sealed.wrapped_dek, ciphertext=sealed.ciphertext, aad=sealed.aad
        )
        session.add(cred)
        await session.flush()
        cred_id = cred.id
    conn = Connection(
        id=conn_id,
        project_id=project_id,
        provider_id=provider.id,
        endpoint_id=endpoint_id,
        offer_id=offer.id if offer else None,
        display_name=data.display_name or provider.name,
        upstream_account_ref=data.upstream_account_ref,
        auth_method=data.auth_method,
        status=ConnectionStatus.PENDING_VALIDATION.value,
        credential_id=cred_id,
        priority=data.priority,
        allowed_models=list(data.allowed_models or []),
        billing_mode=data.billing_mode or access_for(provider.id).default_billing_mode,
        eligibility_ack={
            "accepted_terms": data.accept_terms,
            "terms_url": provider.terms_url,
            "at": utcnow().isoformat(),
            "declared_age": fresh_user.declared_age,
            "country": fresh_user.country,
        },
        created_by=fresh_user.id,
    )
    session.add(conn)
    await session.flush()
    await audit.record(
        session,
        "connection.created",
        actor_user_id=fresh_user.id,
        project_id=project_id,
        target_type="connection",
        target_id=conn.id,
        details={"provider": provider.id, "auth_method": data.auth_method},
    )
    return conn


async def rotate_credential(
    ctx: AppContext, session: AsyncSession, conn: Connection, secret: dict[str, Any], actor_id: str
) -> None:
    if not str(secret.get("api_key") or "").strip():
        raise ValidationFailed("api_key is required")
    sealed = ctx.vault_.seal(secret, aad_for(conn.id))
    old_id = conn.credential_id
    cred = Credential(
        kek_id=sealed.kek_id,
        wrapped_dek=sealed.wrapped_dek,
        ciphertext=sealed.ciphertext,
        aad=sealed.aad,
        rotated_at=utcnow(),
    )
    session.add(cred)
    await session.flush()
    conn.credential_id = cred.id
    conn.status = ConnectionStatus.PENDING_VALIDATION.value
    conn.last_error = None
    await session.flush()
    if old_id:
        old = await session.get(Credential, old_id)
        if old is not None:
            await session.delete(old)  # crypto-shred the previous secret
    await audit.record(
        session,
        "connection.credential_rotated",
        actor_user_id=actor_id,
        project_id=conn.project_id,
        target_type="connection",
        target_id=conn.id,
    )


async def revoke(session: AsyncSession, conn: Connection, actor_id: str) -> None:
    conn.status = ConnectionStatus.REVOKED.value
    cred_id = conn.credential_id
    conn.credential_id = None
    await session.flush()
    if cred_id:
        cred = await session.get(Credential, cred_id)
        if cred is not None:
            await session.delete(cred)
    await audit.record(
        session,
        "connection.revoked",
        actor_user_id=actor_id,
        project_id=conn.project_id,
        target_type="connection",
        target_id=conn.id,
    )


def open_secret(ctx: AppContext, conn: Connection, cred: Credential | None) -> dict[str, Any]:
    if cred is None:
        return {}
    return ctx.vault_.open(
        SealedSecret(
            kek_id=cred.kek_id, wrapped_dek=cred.wrapped_dek, ciphertext=cred.ciphertext, aad=cred.aad
        ),
        aad_for(conn.id),
    )


@dataclass
class CallTarget:
    """Everything needed to call one connection, resolved in a short DB session."""

    connection_id: str
    project_id: str
    provider_id: str
    adapter_key: str
    account_scope: str
    base_url: str
    secret: dict[str, Any]


async def load_target(ctx: AppContext, session: AsyncSession, conn: Connection) -> CallTarget:
    provider = await session.get(Provider, conn.provider_id)
    ep = await session.get(Endpoint, conn.endpoint_id) if conn.endpoint_id else None
    if provider is None or ep is None:
        raise NotFound("connection endpoint not found")
    cred = await session.get(Credential, conn.credential_id) if conn.credential_id else None
    return CallTarget(
        connection_id=conn.id,
        project_id=conn.project_id,
        provider_id=provider.id,
        adapter_key=provider.adapter_key,
        account_scope=conn.upstream_account_ref,
        base_url=ep.base_url,
        secret=open_secret(ctx, conn, cred),
    )


async def connection_context(ctx: AppContext, t: CallTarget) -> ConnectionContext:
    await check_url(ctx, t.base_url)  # re-checked at call time: DNS may have changed since creation
    return ConnectionContext(
        provider_id=t.provider_id,
        base_url=t.base_url,
        secret=t.secret,
        client=ctx.upstream_client,
        connect_timeout_s=ctx.settings.upstream_connect_timeout_s,
        read_timeout_s=ctx.settings.upstream_read_timeout_s,
        max_response_bytes=ctx.settings.upstream_max_response_bytes,
    )


async def upsert_models(
    session: AsyncSession, provider_id: str, infos: list[ModelInfo], default_caps: dict[str, Any]
) -> int:
    added = 0
    for info in infos:
        m = (
            await session.execute(
                select(Model).where(Model.provider_id == provider_id, Model.upstream_id == info.upstream_id)
            )
        ).scalar_one_or_none()
        if m is not None:
            if info.context_window and not m.context_window:
                m.context_window = info.context_window
            if info.max_output_tokens and not m.max_output_tokens:
                m.max_output_tokens = info.max_output_tokens
            # What the provider's listing states now wins over adapter defaults
            # (a model gaining tool support shows up on the next sync). Edits made
            # by an admin (any other source) are left alone.
            current = dict(m.capabilities or {})
            if current.get("source") == "adapter_default":
                # Rebuilt from today's defaults and listing, so a default guess that was later
                # corrected (an unknown recorded as "no") does not stick to existing rows.
                fresh = {**default_caps, **info.capabilities, "source": "adapter_default"}
                if current.get("chat") is False:
                    fresh["chat"] = False
                if fresh != current:
                    m.capabilities = fresh
            elif info.capabilities and current.get("source") == "omniroute_catalog":
                merged = {**current, **info.capabilities}
                if merged != current:
                    m.capabilities = merged
            if info.free is not None:
                _set_reported_price(m, info.free)
            if m.lifecycle in ("removed", "catalog"):
                # Listed by the provider itself: confirmed (imported catalogue entries become routable here).
                m.lifecycle = "active"
            continue
        caps = {**default_caps, **info.capabilities, "source": "adapter_default"}
        if _NON_CHAT.search(info.upstream_id):
            caps["chat"] = False
        session.add(
            Model(
                pricing=_reported_pricing(info.free) if info.free is not None else {},
                provider_id=provider_id,
                upstream_id=info.upstream_id,
                display_name=info.display_name or info.upstream_id,
                aliases=[],
                capabilities=caps,
                context_window=info.context_window,
                max_output_tokens=info.max_output_tokens,
                lifecycle="active",
                quality={},
            )
        )
        added += 1
    await session.flush()
    return added


def _reported_pricing(free: bool) -> dict[str, Any]:
    return {"free": free, "source": "provider_model_list", "observed_at": utcnow().isoformat()}


def _set_reported_price(m: Model, free: bool) -> None:
    current = m.pricing or {}
    if current.get("free") is not free or current.get("source") != "provider_model_list":
        m.pricing = {**current, **_reported_pricing(free)}


async def retire_unlisted_models(
    session: AsyncSession, provider_id: str, infos: list[ModelInfo]
) -> list[str]:
    """Mark discovered (non-curated) models the provider stopped listing as ``removed``.

    Curated catalog entries are left alone: their lifecycle is owned by reviewed catalog data, and some
    providers list only a subset of models per account. An empty listing is never treated as removal.
    """
    if not infos:
        return []
    listed = {i.upstream_id for i in infos}
    rows = (
        await session.execute(
            select(Model).where(Model.provider_id == provider_id, Model.lifecycle != "removed")
        )
    ).scalars()
    removed = []
    for m in rows:
        if m.upstream_id not in listed and (m.capabilities or {}).get("source") == "adapter_default":
            m.lifecycle = "removed"
            removed.append(m.upstream_id)
    return sorted(removed)


@dataclass
class ValidationResult:
    status: str
    models_seen: int
    models_added: int
    error: str | None = None
    category: str | None = None


async def validate(ctx: AppContext, connection_id: str, actor_id: str | None = None) -> ValidationResult:
    """Call the provider with the stored credential and sync its model list.

    The network call happens outside any database transaction.
    """
    async with ctx.db.session() as s:
        conn = await s.get(Connection, connection_id)
        if conn is None:
            raise NotFound("connection not found")
        if conn.status == ConnectionStatus.REVOKED.value:
            raise Conflict("connection is revoked")
        target = await load_target(ctx, s, conn)
    adapter = ctx.adapters.for_provider(target.provider_id, target.adapter_key)
    infos: list[ModelInfo] = []
    error: UpstreamError | None = None
    account: AccountStatus | None = None
    try:
        cctx = await connection_context(ctx, target)
        infos = await adapter.validate_connection(cctx)
        status_fn = getattr(adapter, "account_status", None)
        if status_fn is not None:
            account = await status_fn(cctx)
    except UpstreamError as exc:
        error = exc
    async with ctx.db.transaction() as s:
        conn = await s.get(Connection, connection_id)
        assert conn is not None
        added = 0
        if error is None:
            conn.status = ConnectionStatus.ACTIVE.value
            conn.last_validated_at = utcnow()
            conn.last_error = None
            added = await upsert_models(s, target.provider_id, infos, adapter.get_capabilities(""))
            removed = await retire_unlisted_models(s, target.provider_id, infos)
            if account is not None:
                await _apply_account_status(s, conn, account)
            if removed:
                await notify(
                    s,
                    kind="models.removed",
                    severity="warning",
                    project_id=conn.project_id,
                    title=f"{len(removed)} model(s) no longer offered by {target.provider_id}",
                    body="Routing will skip these models. Pinned requests for them will fail over or "
                    "return model_not_found.",
                    facts={"provider": target.provider_id, "models": removed[:50]},
                    dedupe_key=f"models_removed:{target.provider_id}",
                )
            if conn.offer_id:
                from oasis_route.discovery.review import on_connection_validated

                await on_connection_validated(s, conn.offer_id)
        elif error.category in (ErrorCategory.AUTH_INVALID, ErrorCategory.AUTH_EXPIRED):
            was_active = conn.status == ConnectionStatus.ACTIVE.value
            conn.status = ConnectionStatus.SUSPENDED.value
            conn.last_error = f"{error.category.value}: {error.message}"
            if was_active:
                await notify(
                    s,
                    kind="connection.suspended",
                    severity="warning",
                    project_id=conn.project_id,
                    title=f"Connection '{conn.display_name}' needs attention",
                    body="The provider rejected the stored credential during revalidation. Rotate the "
                    "credential or reconnect the account.",
                    facts={
                        "connection_id": conn.id,
                        "provider": conn.provider_id,
                        "category": error.category.value,
                    },
                    dedupe_key=f"suspended:{conn.id}",
                )
        else:
            conn.last_error = f"{error.category.value}: {error.message}"
        await audit.record(
            s,
            "connection.validated" if error is None else "connection.validation_failed",
            actor_user_id=actor_id,
            project_id=conn.project_id,
            target_type="connection",
            target_id=conn.id,
            details={"models": len(infos), "category": error.category.value if error else None},
        )
        return ValidationResult(
            status=conn.status,
            models_seen=len(infos),
            models_added=added,
            error=error.message if error else None,
            category=error.category.value if error else None,
        )


async def _apply_account_status(s: AsyncSession, conn: Connection, account: AccountStatus) -> None:
    """Record provider-reported account facts; may only *tighten* or fill an unknown billing mode.

    An account the provider reports as free tier (no purchased credits) cannot be billed, so an ``unknown``
    billing mode becomes ``free_tier``. A user-declared ``paid`` mode is never relaxed, and an account
    that has purchased credits flips a stale ``free_tier`` back to ``unknown`` so free-only projects stop
    routing its paid models. The provider's free-model daily ceiling is mirrored into the quota ledger.
    """
    conn.account_status = {**asdict(account), "observed_at": utcnow().isoformat()}
    if account.free_tier is True and conn.billing_mode == "unknown":
        conn.billing_mode = "free_tier"
    elif account.free_tier is False and conn.billing_mode == "free_tier":
        conn.billing_mode = "unknown"
    if account.free_model_daily_limit:
        pool = (
            await s.execute(
                select(QuotaPool).where(
                    QuotaPool.project_id == conn.project_id,
                    QuotaPool.provider_id == conn.provider_id,
                    QuotaPool.account_scope == conn.upstream_account_ref,
                    QuotaPool.model_scope == FREE_MODELS_SCOPE,
                    QuotaPool.unit == QuotaUnit.REQUESTS.value,
                    QuotaPool.period == QuotaPeriod.DAY.value,
                )
            )
        ).scalar_one_or_none()
        if pool is None:
            pool = QuotaPool(
                project_id=conn.project_id,
                provider_id=conn.provider_id,
                account_scope=conn.upstream_account_ref,
                model_scope=FREE_MODELS_SCOPE,
                unit=QuotaUnit.REQUESTS.value,
                period=QuotaPeriod.DAY.value,
                rolling=False,
                used=0,
                reserved=0,
                window_start=utcnow(),
                confidence=0.9,
            )
            s.add(pool)
        pool.limit_value = account.free_model_daily_limit
        pool.source = QuotaSource.PROVIDER_API.value
        pool.last_verified_at = utcnow()
        if account.free_model_daily_used is not None:
            pool.observed_remaining = max(0, account.free_model_daily_limit - account.free_model_daily_used)
            pool.observed_at = utcnow()
            pool.used = max(pool.used or 0, account.free_model_daily_used)


async def suspend_for_auth(
    ctx: AppContext, connection_id: str, category: ErrorCategory, message: str
) -> None:
    """Upstream rejected the credential mid-traffic: stop using it and tell the owner."""
    async with ctx.db.transaction() as s:
        conn = await s.get(Connection, connection_id)
        if conn is None or conn.status != ConnectionStatus.ACTIVE.value:
            return
        conn.status = ConnectionStatus.SUSPENDED.value
        conn.last_error = f"{category.value}: {message}"
        await notify(
            s,
            kind="connection.suspended",
            severity="warning",
            project_id=conn.project_id,
            title=f"Connection '{conn.display_name}' needs attention",
            body="The provider rejected the stored credential. Rotate the credential or reconnect "
            "the account; Oasis Route will not try other accounts on your behalf.",
            facts={"connection_id": conn.id, "provider": conn.provider_id, "category": category.value},
            dedupe_key=f"suspended:{conn.id}",
        )
        await audit.record(
            s,
            "connection.suspended",
            project_id=conn.project_id,
            target_type="connection",
            target_id=conn.id,
            details={"category": category.value},
        )
