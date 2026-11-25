"""One-step onboarding: paste every provider key you hold, get a working, free-first gateway.

The user pastes keys (bare, or a whole ``.env``) and confirms once that the accounts are theirs. For each
key Oasis Route then, without further input:

1. Recognises the provider locally (``key_detect``); the key is only ever sent to that provider.
2. Seals it in the vault and creates the connection through the normal path (eligibility checks,
   one-account-per-provider guard, audit).
3. Validates it against the provider's official endpoint and syncs the account's models, free-model
   prices and, where the provider exposes it, the account's own free-tier and quota status.
4. Schedules an immediate refresh of that provider's approved discovery sources, so published free-tier
   limits are re-read now rather than at the next daily scan.

Nothing here widens what a connection may do. Keys of unknown format are not guessed at, a second account
for the same provider is refused unless the provider permits it, and a ``free_tier`` declaration is only
accepted for providers that have a free plan (billed-only providers stay ``paid``).
"""

from __future__ import annotations

import asyncio
import hmac
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from oasis_route.connections import service as conns
from oasis_route.connections.access import access_for
from oasis_route.connections.key_detect import MAX_KEYS, DetectedKey
from oasis_route.context import AppContext
from oasis_route.db.models import Connection, Credential, Model, Provider, Source, User, utcnow
from oasis_route.domain.enums import ConnectionStatus, ErrorCategory, SourceReviewStatus
from oasis_route.domain.errors import AppError, ValidationFailed
from oasis_route.identity import audit
from oasis_route.registry.imported import UNROUTABLE_LIFECYCLES

VALIDATE_CONCURRENCY = 4


@dataclass
class QuickResult:
    key_hint: str
    provider_id: str | None
    # connected | already_connected | needs_provider | rejected | invalid | error
    outcome: str
    detected_by: str
    message: str = ""
    connection_id: str | None = None
    status: str | None = None
    billing_mode: str | None = None
    models: int = 0
    free_models: int = 0
    candidates: list[str] = field(default_factory=list)
    env_name: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class QuickSummary:
    results: list[QuickResult]
    ignored_lines: int = 0

    @property
    def connected(self) -> int:
        return sum(1 for r in self.results if r.outcome == "connected")

    def as_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "already_connected": sum(1 for r in self.results if r.outcome == "already_connected"),
            "needs_attention": sum(
                1 for r in self.results if r.outcome not in ("connected", "already_connected")
            ),
            "free_models_available": sum(r.free_models for r in self.results),
            "ignored_lines": self.ignored_lines,
            "results": [r.as_dict() for r in self.results],
        }


def billing_mode_for(provider_id: str, free_tier_declared: bool) -> str | None:
    """The billing mode to record. ``None`` keeps the provider default (see ``access.py``)."""
    default = access_for(provider_id).default_billing_mode
    if default == "paid":
        return None  # billed-only provider: a free-tier declaration cannot apply
    return "free_tier" if free_tier_declared else None


async def _same_key_connected(ctx: AppContext, project_id: str, provider_id: str, api_key: str) -> str | None:
    """Id of a live connection in this project already holding exactly this key, if any."""
    async with ctx.db.session() as s:
        rows = (
            await s.execute(
                select(Connection).where(
                    Connection.project_id == project_id,
                    Connection.provider_id == provider_id,
                    Connection.status != ConnectionStatus.REVOKED.value,
                )
            )
        ).scalars()
        for c in rows:
            cred = await s.get(Credential, c.credential_id) if c.credential_id else None
            try:
                stored = str(conns.open_secret(ctx, c, cred).get("api_key") or "")
            except Exception:  # noqa: S112 - an unreadable credential cannot be a duplicate
                continue
            if stored and hmac.compare_digest(stored, api_key):
                return c.id
    return None


async def _free_model_count(ctx: AppContext, provider_id: str) -> tuple[int, int]:
    async with ctx.db.session() as s:
        models = list(
            (
                await s.execute(
                    select(Model).where(
                        Model.provider_id == provider_id, Model.lifecycle.notin_(UNROUTABLE_LIFECYCLES)
                    )
                )
            ).scalars()
        )
    chat = [m for m in models if (m.capabilities or {}).get("chat", True) is not False]
    free = [m for m in chat if (m.pricing or {}).get("free") is True]
    return len(chat), len(free)


async def refresh_provider_sources(ctx: AppContext, provider_ids: set[str]) -> int:
    """Make the approved discovery sources of these providers due now. Returns how many were scheduled."""
    if not provider_ids:
        return 0
    now = utcnow()
    n = 0
    async with ctx.db.transaction() as s:
        rows = (
            await s.execute(
                select(Source).where(
                    Source.provider_id.in_(sorted(provider_ids)),
                    Source.review_status == SourceReviewStatus.APPROVED.value,
                )
            )
        ).scalars()
        for src in rows:
            if src.next_fetch_at is None or src.next_fetch_at > now:
                src.next_fetch_at = now
                n += 1
    return n


async def _connect_one(
    ctx: AppContext,
    *,
    project_id: str,
    user: User,
    d: DetectedKey,
    accept_terms: bool,
    free_tier_declared: bool,
) -> QuickResult:
    res = QuickResult(
        key_hint=d.hint,
        provider_id=d.provider_id,
        outcome="needs_provider",
        detected_by=d.method,
        message=d.note,
        candidates=list(d.candidates),
        env_name=d.env_name,
    )
    if d.provider_id is None:
        return res
    async with ctx.db.session() as s:
        provider = await s.get(Provider, d.provider_id)
    if provider is None or provider.id in ("custom", "mock", "mock_alt"):
        res.outcome = "rejected"
        res.message = "unknown provider; use the full connection form for custom endpoints"
        return res
    existing = await _same_key_connected(ctx, project_id, provider.id, d.api_key)
    if existing:
        res.outcome, res.connection_id = "already_connected", existing
        res.message = "this key is already connected"
        return res
    try:
        async with ctx.db.transaction() as s:
            conn = await conns.create_connection(
                ctx,
                s,
                project_id=project_id,
                user=user,
                data=conns.ConnectionInput(
                    provider_id=provider.id,
                    secret={"api_key": d.api_key},
                    display_name=provider.name,
                    accept_terms=accept_terms,
                    billing_mode=billing_mode_for(provider.id, free_tier_declared),
                ),
            )
            cid = conn.id
    except AppError as exc:
        res.outcome = "rejected"
        reasons = (exc.details or {}).get("reasons") if isinstance(exc.details, dict) else None
        res.message = exc.message + (f" ({'; '.join(map(str, reasons))})" if reasons else "")
        return res
    res.connection_id = cid
    try:
        v = await conns.validate(ctx, cid, user.id)
    except AppError as exc:
        res.outcome, res.message = "error", exc.message
        return res
    res.status, res.models = v.status, v.models_seen
    if v.status == ConnectionStatus.ACTIVE.value:
        res.outcome = "connected"
        _, res.free_models = await _free_model_count(ctx, provider.id)
    elif v.category in (ErrorCategory.AUTH_INVALID.value, ErrorCategory.AUTH_EXPIRED.value):
        res.outcome = "invalid"
        res.message = "the provider rejected this key; check it was copied completely and is still active"
    else:
        res.outcome = "error"
        res.message = v.error or "validation did not complete; it will be retried in the background"
    async with ctx.db.session() as s:
        c = await s.get(Connection, cid)
        res.billing_mode = c.billing_mode if c else None
    if res.outcome == "connected" and res.billing_mode == "paid":
        res.message = (
            "connected; this provider bills every request, so free-only projects will not route to it"
        )
    return res


async def quick_connect(
    ctx: AppContext,
    *,
    project_id: str,
    user: User,
    keys: list[DetectedKey],
    accept_terms: bool,
    free_tier_declared: bool = False,
    ignored_lines: int = 0,
) -> QuickSummary:
    if not keys:
        raise ValidationFailed("no API keys found in the submitted text")
    if len(keys) > MAX_KEYS:
        raise ValidationFailed(f"at most {MAX_KEYS} keys per request")
    if not accept_terms:
        raise ValidationFailed(
            "confirm that you hold these provider accounts yourself and accept each provider's terms"
        )
    # Keys for the same provider are connected one after another so the one-account guard sees each.
    by_provider: dict[str, list[int]] = {}
    for i, d in enumerate(keys):
        by_provider.setdefault(d.provider_id or f"?{i}", []).append(i)
    sem = asyncio.Semaphore(VALIDATE_CONCURRENCY)
    results: list[QuickResult | None] = [None] * len(keys)

    async def run_group(indexes: list[int]) -> None:
        for i in indexes:
            async with sem:
                results[i] = await _connect_one(
                    ctx,
                    project_id=project_id,
                    user=user,
                    d=keys[i],
                    accept_terms=accept_terms,
                    free_tier_declared=free_tier_declared,
                )

    await asyncio.gather(*(run_group(g) for g in by_provider.values()))
    flat = [r for r in results if r is not None]
    connected = {r.provider_id for r in flat if r.outcome == "connected" and r.provider_id}
    await refresh_provider_sources(ctx, connected)
    async with ctx.db.transaction() as s:
        await audit.record(
            s,
            "connection.quick_connect",
            actor_user_id=user.id,
            project_id=project_id,
            details={
                "submitted": len(keys),
                "outcomes": {o: sum(1 for r in flat if r.outcome == o) for o in {r.outcome for r in flat}},
                "providers": sorted(connected),
            },
        )
    return QuickSummary(flat, ignored_lines)
