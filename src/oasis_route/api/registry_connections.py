"""Admin API — provider registry, model catalogue, connections, quota pools."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oasis_route.api.deps import Principal, get_ctx, get_session, project_access, require_admin, require_scope
from oasis_route.connections import key_detect
from oasis_route.connections import oauth as oauth_flows
from oasis_route.connections import quick_connect as quick
from oasis_route.connections import service as conns
from oasis_route.context import AppContext
from oasis_route.db.models import Connection, Endpoint, HealthRecord, Model, Provider, QuotaPool
from oasis_route.domain.enums import ApiKeyScope, AuthMethod, QuotaPeriod, QuotaSource, QuotaUnit, Task
from oasis_route.domain.errors import Conflict, NotFound, ValidationFailed
from oasis_route.identity import audit
from oasis_route.quota import ledger

router = APIRouter(prefix="/api/v1", tags=["registry"])


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------------------------------------- providers
class EndpointOut(ORM):
    id: str
    base_url: str
    protocol: str
    region: str | None
    is_default: bool


class ProviderOut(ORM):
    id: str
    name: str
    adapter_key: str
    official_domains: list[str]
    terms_url: str | None
    policy_url: str | None
    status: str
    data_policy: dict[str, Any]
    regions: list[str]
    min_age: int | None
    allowed_countries: list[str]
    blocked_countries: list[str]
    multi_account_permitted: bool
    automated_access_permitted: bool
    notes: str
    endpoints: list[EndpointOut] = []
    oauth_available: bool = False
    oauth_device: bool = False  # sign in by entering a short code on any device (RFC 8628)


@router.get("/providers", response_model=list[ProviderOut])
async def list_providers(
    _: Principal = Depends(require_scope(ApiKeyScope.READ)),
    session: AsyncSession = Depends(get_session),
    ctx: AppContext = Depends(get_ctx),
) -> list[ProviderOut]:
    providers = (await session.execute(select(Provider).order_by(Provider.id))).scalars().all()
    eps = (await session.execute(select(Endpoint).where(Endpoint.is_default.is_(True)))).scalars().all()
    by: dict[str, list[EndpointOut]] = {}
    for e in eps:
        by.setdefault(e.provider_id, []).append(EndpointOut.model_validate(e))
    out = []
    for p in providers:
        po = ProviderOut.model_validate(p)
        po.endpoints = by.get(p.id, [])
        po.oauth_available = oauth_flows.available(ctx, p.id)
        spec = oauth_flows.SPECS.get(p.id)
        po.oauth_device = po.oauth_available and spec is not None and spec.device_url is not None
        out.append(po)
    return out


class DataPolicyFacts(BaseModel):
    trains_on_inputs: bool | None = None
    zero_retention: bool | None = None
    source: str | None = Field(default=None, description="Evidence URL for the stated policy.")
    as_of: str | None = None


class ProviderPatch(BaseModel):
    status: str | None = Field(default=None, pattern="^(active|suspended|retired)$")
    data_policy: DataPolicyFacts | None = None
    regions: list[str] | None = None
    min_age: int | None = Field(default=None, ge=0, le=99)
    allowed_countries: list[str] | None = None
    blocked_countries: list[str] | None = None
    multi_account_permitted: bool | None = None
    automated_access_permitted: bool | None = None
    terms_url: str | None = None
    policy_url: str | None = None
    notes: str | None = None

    @field_validator("data_policy")
    @classmethod
    def _evidence(cls, v: DataPolicyFacts | None) -> DataPolicyFacts | None:
        if (
            v is not None
            and (v.trains_on_inputs is not None or v.zero_retention is not None)
            and not v.source
        ):
            raise ValueError("data policy facts require a source (evidence URL)")
        return v


@router.patch("/admin/providers/{provider_id}", response_model=ProviderOut)
async def patch_provider(
    provider_id: str,
    body: ProviderPatch,
    p: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ProviderOut:
    prov = await session.get(Provider, provider_id)
    if prov is None:
        raise NotFound("provider not found")
    changes = body.model_dump(exclude_unset=True, mode="json")
    for k, v in changes.items():
        if k in ("allowed_countries", "blocked_countries") and v is not None:
            v = [c.upper() for c in v]
        setattr(prov, k, v)
    await audit.record(
        session,
        "registry.provider_updated",
        actor_user_id=p.user.id,
        target_type="provider",
        target_id=provider_id,
        details=changes,
    )
    await session.flush()
    return ProviderOut.model_validate(prov)


# ------------------------------------------------------------------------------------------- models
class ModelOut(ORM):
    id: str
    provider_id: str
    upstream_id: str
    display_name: str
    family: str | None
    aliases: list[str]
    capabilities: dict[str, Any]
    context_window: int | None
    max_output_tokens: int | None
    lifecycle: str
    quality: dict[str, Any]


@router.get("/models", response_model=list[ModelOut])
async def list_models(
    provider: str | None = Query(default=None),
    _: Principal = Depends(require_scope(ApiKeyScope.READ)),
    session: AsyncSession = Depends(get_session),
) -> list[ModelOut]:
    q = select(Model).order_by(Model.provider_id, Model.upstream_id)
    if provider:
        q = q.where(Model.provider_id == provider)
    return [ModelOut.model_validate(m) for m in (await session.execute(q)).scalars().all()]


class QualityEvidence(BaseModel):
    score: float = Field(ge=0, le=100)
    source: str = Field(min_length=3, description="Benchmark/report URL or citation.")
    as_of: str = Field(min_length=4, description="Date of the evidence (YYYY-MM-DD).")
    methodology: str = Field(min_length=3)
    model_version: str | None = None
    confidence: float = Field(ge=0, le=1)


class ModelPatch(BaseModel):
    quality: dict[Task, QualityEvidence] | None = None
    capabilities: dict[str, Any] | None = None
    aliases: list[str] | None = None
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    lifecycle: str | None = Field(default=None, pattern="^(active|deprecated|removed)$")
    display_name: str | None = None
    family: str | None = None


@router.patch("/admin/models/{model_id}", response_model=ModelOut)
async def patch_model(
    model_id: str,
    body: ModelPatch,
    p: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ModelOut:
    m = await session.get(Model, model_id)
    if m is None:
        raise NotFound("model not found")
    changes = body.model_dump(exclude_unset=True, mode="json")
    for k, v in changes.items():
        if k == "capabilities":
            # Marked as an operator's edit, so a later model sync does not
            # overwrite it with what the provider's listing says.
            v = {**m.capabilities, **v}
            if "source" not in changes["capabilities"]:
                v["source"] = "admin"
        setattr(m, k, v)
    await audit.record(
        session,
        "registry.model_updated",
        actor_user_id=p.user.id,
        target_type="model",
        target_id=model_id,
        details=changes,
    )
    await session.flush()
    return ModelOut.model_validate(m)


# ------------------------------------------------------------------------------------------- connections
class ConnectionCreate(BaseModel):
    provider_id: str
    api_key: SecretStr | None = Field(
        default=None, description="Your own provider API key. Stored encrypted."
    )
    display_name: str = ""
    upstream_account_ref: str = Field(
        default="default",
        max_length=200,
        description="Non-secret label of the upstream account/project; connections sharing it share quota.",
    )
    auth_method: AuthMethod = AuthMethod.API_KEY
    base_url: str | None = None
    offer_id: str | None = None
    priority: int = Field(default=0, ge=-100, le=100)
    allowed_models: list[str] = Field(default_factory=list)
    accept_terms: bool = Field(
        default=False,
        description="Confirms you hold this provider account yourself and accept the provider's terms.",
    )
    billing_mode: Literal["free_tier", "paid", "unknown"] | None = Field(
        default=None,
        description="free_tier: the upstream account has no billing / is on a free plan, so it can never be "
        "charged. Under a free_only project only free_tier connections or zero-priced models are routed. "
        "Defaults to the provider's documented mode (paid for billed-only providers, otherwise unknown).",
    )
    validate_now: bool = True


class HealthOut(ORM):
    model_scope: str
    state: str
    consecutive_failures: int
    open_until: datetime | None
    last_error_category: str | None
    success_count: int
    failure_count: int
    ewma_latency_ms: float | None


class ConnectionOut(ORM):
    id: str
    project_id: str
    provider_id: str
    endpoint_id: str | None
    offer_id: str | None
    display_name: str
    upstream_account_ref: str
    auth_method: str
    billing_mode: str
    account_status: dict[str, Any] = {}
    status: str
    priority: int
    allowed_models: list[str]
    last_validated_at: datetime | None
    last_error: str | None
    created_at: datetime
    has_credential: bool = False
    health: list[HealthOut] = []
    validation: dict[str, Any] | None = None


def _conn_out(c: Connection, health: list[HealthRecord] | None = None) -> ConnectionOut:
    out = ConnectionOut.model_validate(c)
    out.has_credential = c.credential_id is not None
    out.health = [HealthOut.model_validate(h) for h in health or []]
    return out


async def _get_conn(session: AsyncSession, project_id: str, connection_id: str) -> Connection:
    c = await session.get(Connection, connection_id)
    if c is None or c.project_id != project_id:
        raise NotFound("connection not found")
    return c


@router.post("/projects/{project_id}/connections", response_model=ConnectionOut, status_code=201)
async def create_connection(
    project_id: str,
    body: ConnectionCreate,
    p: Principal = Depends(require_scope(ApiKeyScope.MANAGE)),
    ctx: AppContext = Depends(get_ctx),
) -> ConnectionOut:
    async with ctx.db.transaction() as session:
        await project_access(project_id, p, session, write=True)
        conn = await conns.create_connection(
            ctx,
            session,
            project_id=project_id,
            user=p.user,
            data=conns.ConnectionInput(
                provider_id=body.provider_id,
                secret={"api_key": body.api_key.get_secret_value()} if body.api_key else None,
                display_name=body.display_name,
                upstream_account_ref=body.upstream_account_ref,
                auth_method=body.auth_method.value,
                base_url=body.base_url,
                offer_id=body.offer_id,
                priority=body.priority,
                allowed_models=body.allowed_models,
                accept_terms=body.accept_terms,
                billing_mode=body.billing_mode,
            ),
        )
        cid = conn.id
    validation = None
    if body.validate_now:
        validation = (await conns.validate(ctx, cid, p.user.id)).__dict__
    async with ctx.db.session() as session:
        c = await session.get(Connection, cid)
        assert c is not None
        out = _conn_out(c)
    out.validation = validation
    return out


class QuickKey(BaseModel):
    api_key: SecretStr
    provider_id: str | None = Field(
        default=None, description="Provider the key was issued by. Omit to detect it from the key format."
    )


class QuickConnectIn(BaseModel):
    text: SecretStr | None = Field(
        default=None,
        description="Pasted keys: one per line, or a whole .env file. Only provider keys are read; other "
        "variables are ignored and never stored or echoed.",
    )
    keys: list[QuickKey] = Field(default_factory=list, max_length=key_detect.MAX_KEYS)
    providers_by_line: dict[int, str] = Field(
        default_factory=dict,
        max_length=key_detect.MAX_KEYS,
        description="Provider for keys whose format several providers share: {line number in text: "
        "provider_id}, using the `line` reported by a dry run.",
    )
    accept_terms: bool = Field(
        default=False,
        description="Confirms you hold every one of these provider accounts yourself and accept each "
        "provider's terms.",
    )
    free_tier: bool = Field(
        default=False,
        description="Declares these accounts have no billing set up (free plan or trial key). Ignored for "
        "providers that bill every request.",
    )
    dry_run: bool = Field(default=False, description="Only report what was detected; store nothing.")


def _detected(body: QuickConnectIn) -> tuple[list[key_detect.DetectedKey], int]:
    found: list[key_detect.DetectedKey] = []
    ignored = 0
    if body.text is not None:
        raw = body.text.get_secret_value()
        if len(raw.encode()) > key_detect.MAX_TEXT_BYTES:
            raise ValidationFailed(f"pasted text is limited to {key_detect.MAX_TEXT_BYTES} bytes")
        parsed = key_detect.parse_text(raw)
        for d in parsed.keys:
            chosen = body.providers_by_line.get(d.line or 0)
            if chosen:
                line = d.line
                d = key_detect.detect(d.api_key, provider_id=chosen, env_name=d.env_name)
                d.line = line
            found.append(d)
        ignored = parsed.ignored_lines
    seen = {d.api_key for d in found}
    for k in body.keys:
        value = k.api_key.get_secret_value().strip()
        if value and value not in seen:
            seen.add(value)
            found.append(key_detect.detect(value, provider_id=k.provider_id))
    return found, ignored


@router.post("/projects/{project_id}/connections/quick")
async def quick_connect_keys(
    project_id: str,
    body: QuickConnectIn,
    p: Principal = Depends(require_scope(ApiKeyScope.MANAGE)),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Connect many provider keys at once. Each key's provider is detected locally; nothing is guessed."""
    async with ctx.db.session() as session:
        await project_access(project_id, p, session, write=True)
    found, ignored = _detected(body)
    if len(found) > key_detect.MAX_KEYS:
        raise ValidationFailed(f"at most {key_detect.MAX_KEYS} keys per request")
    if body.dry_run:
        return {
            "dry_run": True,
            "ignored_lines": ignored,
            "results": [
                {
                    "key_hint": d.hint,
                    "provider_id": d.provider_id,
                    "detected_by": d.method,
                    "candidates": d.candidates,
                    "env_name": d.env_name,
                    "line": d.line,
                    "message": d.note,
                }
                for d in found
            ],
        }
    summary = await quick.quick_connect(
        ctx,
        project_id=project_id,
        user=p.user,
        keys=found,
        accept_terms=body.accept_terms,
        free_tier_declared=body.free_tier,
        ignored_lines=ignored,
    )
    return summary.as_dict()


@router.get("/projects/{project_id}/connections", response_model=list[ConnectionOut])
async def list_connections(
    project_id: str,
    p: Principal = Depends(require_scope(ApiKeyScope.READ)),
    session: AsyncSession = Depends(get_session),
) -> list[ConnectionOut]:
    await project_access(project_id, p, session)
    rows = (
        (
            await session.execute(
                select(Connection).where(Connection.project_id == project_id).order_by(Connection.created_at)
            )
        )
        .scalars()
        .all()
    )
    health = (
        (
            await session.execute(
                select(HealthRecord).where(HealthRecord.connection_id.in_([c.id for c in rows] or [""]))
            )
        )
        .scalars()
        .all()
    )
    return [_conn_out(c, [h for h in health if h.connection_id == c.id]) for c in rows]


@router.get("/projects/{project_id}/connections/{connection_id}", response_model=ConnectionOut)
async def get_connection(
    project_id: str,
    connection_id: str,
    p: Principal = Depends(require_scope(ApiKeyScope.READ)),
    session: AsyncSession = Depends(get_session),
) -> ConnectionOut:
    await project_access(project_id, p, session)
    c = await _get_conn(session, project_id, connection_id)
    health = (
        (await session.execute(select(HealthRecord).where(HealthRecord.connection_id == c.id)))
        .scalars()
        .all()
    )
    return _conn_out(c, list(health))


class ConnectionPatch(BaseModel):
    display_name: str | None = None
    priority: int | None = Field(default=None, ge=-100, le=100)
    allowed_models: list[str] | None = None
    billing_mode: Literal["free_tier", "paid", "unknown"] | None = None


@router.patch("/projects/{project_id}/connections/{connection_id}", response_model=ConnectionOut)
async def patch_connection(
    project_id: str,
    connection_id: str,
    body: ConnectionPatch,
    p: Principal = Depends(require_scope(ApiKeyScope.MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> ConnectionOut:
    await project_access(project_id, p, session, write=True)
    c = await _get_conn(session, project_id, connection_id)
    changes = body.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(c, k, v)
    await audit.record(
        session,
        "connection.updated",
        actor_user_id=p.user.id,
        project_id=project_id,
        target_type="connection",
        target_id=c.id,
        details=changes,
    )
    await session.flush()
    return _conn_out(c)


@router.post("/projects/{project_id}/connections/{connection_id}/validate")
async def validate_connection(
    project_id: str,
    connection_id: str,
    p: Principal = Depends(require_scope(ApiKeyScope.MANAGE)),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Re-validate the credential and sync the provider's model list."""
    async with ctx.db.session() as session:
        await project_access(project_id, p, session, write=True)
        await _get_conn(session, project_id, connection_id)
    return (await conns.validate(ctx, connection_id, p.user.id)).__dict__


class RotateIn(BaseModel):
    api_key: SecretStr


@router.post("/projects/{project_id}/connections/{connection_id}/rotate-credential")
async def rotate_connection_credential(
    project_id: str,
    connection_id: str,
    body: RotateIn,
    p: Principal = Depends(require_scope(ApiKeyScope.MANAGE)),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    async with ctx.db.transaction() as session:
        await project_access(project_id, p, session, write=True)
        c = await _get_conn(session, project_id, connection_id)
        if c.status == "revoked":
            raise Conflict("connection is revoked")
        await conns.rotate_credential(
            ctx, session, c, {"api_key": body.api_key.get_secret_value()}, p.user.id
        )
    return (await conns.validate(ctx, connection_id, p.user.id)).__dict__


@router.delete("/projects/{project_id}/connections/{connection_id}", status_code=204)
async def delete_connection(
    project_id: str,
    connection_id: str,
    p: Principal = Depends(require_scope(ApiKeyScope.MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Revoke a connection and destroy its stored credential."""
    await project_access(project_id, p, session, write=True)
    c = await _get_conn(session, project_id, connection_id)
    await conns.revoke(session, c, p.user.id)
    return Response(status_code=204)


# ------------------------------------------------------------------------------------------- quota pools
class PoolCreate(BaseModel):
    provider_id: str
    account_scope: str = "default"
    model_scope: str = "*"
    unit: QuotaUnit
    period: QuotaPeriod
    limit_value: int | None = Field(
        default=None, ge=0, description="Omit when unknown; unknown != unlimited."
    )
    reset_tz: str = "UTC"
    rolling: bool = False
    expires_at: datetime | None = None
    offer_id: str | None = None
    source: QuotaSource = QuotaSource.USER_DECLARED
    confidence: float = Field(default=0.5, ge=0, le=1)


class PoolPatch(BaseModel):
    limit_value: int | None = Field(default=None, ge=0)
    reset_tz: str | None = None
    rolling: bool | None = None
    expires_at: datetime | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)


class PoolOut(ORM):
    id: str
    project_id: str
    provider_id: str
    account_scope: str
    model_scope: str
    offer_id: str | None
    unit: str
    period: str
    reset_tz: str
    rolling: bool
    limit_value: int | None
    used: int
    reserved: int
    estimated_used: int
    drift: int
    window_start: datetime | None
    observed_remaining: int | None
    observed_at: datetime | None
    exhausted_until: datetime | None
    needs_recheck: bool
    expires_at: datetime | None
    source: str
    confidence: float
    last_verified_at: datetime | None
    available: int | None = None


def _pool_out(pool: QuotaPool) -> PoolOut:
    from oasis_route.db.models import utcnow

    out = PoolOut.model_validate(pool)
    out.available = ledger.available(pool, utcnow())
    return out


@router.get("/projects/{project_id}/quota-pools", response_model=list[PoolOut])
async def list_pools(
    project_id: str,
    p: Principal = Depends(require_scope(ApiKeyScope.READ)),
    session: AsyncSession = Depends(get_session),
) -> list[PoolOut]:
    await project_access(project_id, p, session)
    rows = (
        (
            await session.execute(
                select(QuotaPool)
                .where(QuotaPool.project_id == project_id)
                .order_by(QuotaPool.provider_id, QuotaPool.unit, QuotaPool.period)
            )
        )
        .scalars()
        .all()
    )
    return [_pool_out(x) for x in rows]


@router.post("/projects/{project_id}/quota-pools", response_model=PoolOut, status_code=201)
async def create_pool(
    project_id: str,
    body: PoolCreate,
    p: Principal = Depends(require_scope(ApiKeyScope.MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> PoolOut:
    await project_access(project_id, p, session, write=True)
    if await session.get(Provider, body.provider_id) is None:
        raise NotFound("provider not found")
    if body.unit is QuotaUnit.CONCURRENT and body.period is not QuotaPeriod.NONE:
        raise ValidationFailed("concurrency pools use period 'none'")
    dup = (
        await session.execute(
            select(QuotaPool.id).where(
                QuotaPool.project_id == project_id,
                QuotaPool.provider_id == body.provider_id,
                QuotaPool.account_scope == body.account_scope,
                QuotaPool.model_scope == body.model_scope,
                QuotaPool.unit == body.unit.value,
                QuotaPool.period == body.period.value,
            )
        )
    ).first()
    if dup is not None:
        raise Conflict(
            "a pool for this allocation already exists; connections sharing an upstream account "
            "share one pool",
            details={"pool_id": dup[0]},
        )
    pool = QuotaPool(
        project_id=project_id,
        used=0,
        reserved=0,
        **body.model_dump(mode="python", exclude={"unit", "period", "source"}),
        unit=body.unit.value,
        period=body.period.value,
        source=body.source.value,
    )
    session.add(pool)
    await session.flush()
    await audit.record(
        session,
        "quota_pool.created",
        actor_user_id=p.user.id,
        project_id=project_id,
        target_type="quota_pool",
        target_id=pool.id,
        details=body.model_dump(mode="json"),
    )
    return _pool_out(pool)


@router.patch("/projects/{project_id}/quota-pools/{pool_id}", response_model=PoolOut)
async def patch_pool(
    project_id: str,
    pool_id: str,
    body: PoolPatch,
    p: Principal = Depends(require_scope(ApiKeyScope.MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> PoolOut:
    await project_access(project_id, p, session, write=True)
    pool = await session.get(QuotaPool, pool_id)
    if pool is None or pool.project_id != project_id:
        raise NotFound("quota pool not found")
    changes = body.model_dump(exclude_unset=True, mode="python")
    for k, v in changes.items():
        setattr(pool, k, v)
    await audit.record(
        session,
        "quota_pool.updated",
        actor_user_id=p.user.id,
        project_id=project_id,
        target_type="quota_pool",
        target_id=pool.id,
        details=body.model_dump(exclude_unset=True, mode="json"),
    )
    await session.flush()
    return _pool_out(pool)


@router.delete("/projects/{project_id}/quota-pools/{pool_id}", status_code=204)
async def delete_pool(
    project_id: str,
    pool_id: str,
    p: Principal = Depends(require_scope(ApiKeyScope.MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await project_access(project_id, p, session, write=True)
    pool = await session.get(QuotaPool, pool_id)
    if pool is None or pool.project_id != project_id:
        raise NotFound("quota pool not found")
    if pool.reserved > 0:
        raise Conflict("pool has in-flight reservations")
    await session.delete(pool)
    await audit.record(
        session,
        "quota_pool.deleted",
        actor_user_id=p.user.id,
        project_id=project_id,
        target_type="quota_pool",
        target_id=pool_id,
    )
    return Response(status_code=204)


# ------------------------------------------------------------------------------------------- route preview
class RoutePreviewIn(BaseModel):
    model: str = "auto"
    messages: list[dict[str, Any]] = Field(default_factory=lambda: [{"role": "user", "content": "hello"}])
    max_tokens: int | None = Field(default=None, ge=1)
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    response_format: dict[str, Any] | None = None
    oasis_route: dict[str, Any] = Field(default_factory=dict)


@router.post("/projects/{project_id}/route-preview")
async def route_preview(
    project_id: str,
    body: RoutePreviewIn,
    p: Principal = Depends(require_scope(ApiKeyScope.READ)),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Explain how a request would be routed right now, without calling any provider or reserving quota."""
    from oasis_route.gateway.engine import GatewayError, parse_body
    from oasis_route.routing.router import build_plan

    async with ctx.db.session() as session:
        project = await project_access(project_id, p, session)
        try:
            parsed = parse_body(
                body.model_dump(exclude_none=True), project, ctx.settings.default_max_output_tokens
            )
        except GatewayError as err:
            raise ValidationFailed(err.message) from err
        plan = await build_plan(session, project, parsed.options)
    out = plan.describe()
    out["downgrade_flags"] = [plan.is_downgrade(c) for c in plan.candidates]
    out["max_fallback_hops"] = (
        project.max_fallback_hops if project.max_fallback_hops is not None else ctx.settings.max_fallback_hops
    )
    return out
