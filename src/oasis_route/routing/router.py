"""Strongest-suitable-first router (docs/ROUTING_AND_QUOTAS.md §2-3).

1. Build every (connection, model) pair the project is authorized to use.
2. Apply hard filters. Every rejected pair carries a machine-readable reason, surfaced in route previews and
   persisted route decisions.
3. Rank survivors lexicographically:
   explicit preference → task quality → reliability → latency → quota scarcity / reset horizon → cost.

Modes:
- ``strict_quality``: exact task score; all capacity of the strongest model is tried before descending.
- ``balanced``: scores are compared in 10-point bands, so reliability/latency can break near-ties.

Unrated models (no quality evidence) rank after every rated model; they are never assumed strong. Among
themselves they are ordered by a labelled name-based prior (``routing.priors``) so ``auto`` still tries a
70B model before a 1B one without operator input. Priors never satisfy ``min_quality``.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oasis_route.connections.service import CONNECTABLE_OFFER_STATES, eligibility_reasons
from oasis_route.db.models import Connection, Model, Offer, Project, Provider, QuotaPool, User, utcnow
from oasis_route.domain.enums import ConnectionStatus, QuotaPeriod, RoutingMode, Task
from oasis_route.health import breaker
from oasis_route.quota import ledger
from oasis_route.quota.windows import next_reset
from oasis_route.registry.catalog import DEMO_PROVIDER_IDS
from oasis_route.routing.cost import CostEstimate, estimate_cost
from oasis_route.routing.priors import PRIOR_SOURCE, model_prior
from oasis_route.spend import budgets

QUALITY_EPSILON = 0.5  # score difference treated as "same quality" for downgrade detection


@dataclass
class RouteOptions:
    requested_model: str = "auto"
    task: Task = Task.GENERAL
    mode: RoutingMode = RoutingMode.STRICT_QUALITY
    min_quality: float | None = None
    allow_fallback: bool = True
    allow_downgrade: bool = True
    providers: list[str] = field(default_factory=list)  # restrict to + prefer in this order
    require: set[str] = field(default_factory=set)  # required capabilities
    input_tokens: int = 0
    max_output_tokens: int = 0
    free_only: bool = False  # per-request tightening; a free_only project is always free-only
    # bounded_autonomy: the (connection_id, upstream_model) a single-use paid approval admits.
    approved_paid: tuple[str, str] | None = None


@dataclass
class Candidate:
    connection_id: str
    provider_id: str
    adapter_key: str
    account_scope: str
    multi_account_permitted: bool
    model_id: str
    upstream_model: str
    quality: float | None
    quality_source: str | None
    reliability: float
    latency_ms: float | None
    quota_fraction: float | None  # min remaining fraction over known pools (None == unmetered/unknown)
    reset_in_s: float | None
    quota_metered: bool
    needs_probe: bool
    priority: int
    preference_rank: int
    prior: float | None = None  # heuristic ordering hint for unrated models; never quality evidence
    cost_basis: str = "unknown"  # free_model | free_tier_account | local | paid | unknown
    # Worst-case cost of this request on this route (routing/cost.py): status free | priced | unknown.
    cost_status: str = "unknown"
    est_cost_usd_micros: int | None = None
    price_verified: bool = False
    context_window: int | None = None
    pricing: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("adapter_key")
        d.pop("multi_account_permitted")
        d.pop("pricing")
        d["prior_source"] = PRIOR_SOURCE if self.prior is not None else None
        return d


@dataclass
class Rejection:
    provider_id: str
    upstream_model: str
    connection_id: str | None
    reason: str
    retry_in_s: float | None = None  # when this candidate may become routable again, if known

    def describe(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RoutePlan:
    options: RouteOptions
    candidates: list[Candidate]
    rejected: list[Rejection]
    best_quality: float | None
    pinned: bool
    free_only: bool = False
    spend_mode: str = "free_only"
    # bounded_autonomy: paid routes that passed every other gate and need explicit approval.
    approval_candidates: list[Candidate] = field(default_factory=list)

    def is_downgrade(self, c: Candidate) -> bool:
        if self.best_quality is None:
            return False
        return c.quality is None or c.quality < self.best_quality - QUALITY_EPSILON

    def describe(self) -> dict[str, Any]:
        return {
            "requested_model": self.options.requested_model,
            "task": self.options.task.value,
            "mode": self.options.mode.value,
            "best_quality": self.best_quality,
            "free_only": self.free_only,
            "spend_mode": self.spend_mode,
            "approval_candidates": [c.describe() for c in self.approval_candidates],
            "candidates": [c.describe() for c in self.candidates],
            "rejected": [r.describe() for r in self.rejected],
        }


def quality_of(model: Model, task: Task) -> tuple[float | None, str | None]:
    q = model.quality or {}
    entry = q.get(task.value) or q.get(Task.GENERAL.value)
    if isinstance(entry, dict) and isinstance(entry.get("score"), int | float):
        return float(entry["score"]), str(entry.get("source") or "")
    return None, None


SPEND_FREE_ONLY = "free_only"  # zero-budget: verified free routes only; fail clearly when none remain
SPEND_BOUNDED = "bounded_autonomy"  # free first; a paid route needs a per-request single-use approval
SPEND_ALLOW_PAID = "allow_paid"  # continuity: free first; authorised paid fallback, always labelled
SPEND_ALIASES = {"zero_budget": SPEND_FREE_ONLY, "continuity": SPEND_ALLOW_PAID}
SPEND_MODES = (SPEND_FREE_ONLY, SPEND_BOUNDED, SPEND_ALLOW_PAID)
SPEND_LABELS = {
    SPEND_FREE_ONLY: "zero_budget",
    SPEND_BOUNDED: "bounded_autonomy",
    SPEND_ALLOW_PAID: "continuity",
}


def spend_mode(project: Project) -> str:
    v = project.spend_policy or SPEND_FREE_ONLY
    v = SPEND_ALIASES.get(v, v)
    return v if v in SPEND_MODES else SPEND_FREE_ONLY  # unknown values fail closed


def cost_basis(conn: Connection, provider: Provider, model: Model) -> str:
    """Why a (connection, model) pair costs nothing, or ``paid`` / ``unknown``.

    A pair is provably free when the model is published at zero price in the provider's own catalogue
    (``Model.pricing.free``), the user declared the upstream account a free-tier account with no billing
    (``Connection.billing_mode == "free_tier"``), or the provider is a local development mock.
    Everything else is ``paid`` (explicitly billed) or ``unknown``; neither is routable under ``free_only``.
    """
    if "local" in (provider.regions or []):
        return "local"
    if (model.pricing or {}).get("free") is True:
        return "free_model"
    if conn.billing_mode == "free_tier":
        return "free_tier_account"
    if conn.billing_mode == "paid":
        return "paid"
    return "unknown"


def _model_matches(m: Model, name: str) -> bool:
    return name == m.upstream_id or name in (m.aliases or [])


def _policy_reason(project: Project, provider: Provider) -> str | None:
    pol = project.data_policy or {}
    allow = pol.get("provider_allowlist") or []
    deny = pol.get("provider_denylist") or []
    if allow and provider.id not in allow:
        return "data_policy:provider_not_allowlisted"
    if provider.id in deny:
        return "data_policy:provider_denylisted"
    facts = provider.data_policy or {}
    if pol.get("require_no_training") and facts.get("trains_on_inputs") is not False:
        return "data_policy:training_policy_unknown_or_trains"
    if pol.get("require_zero_retention") and facts.get("zero_retention") is not True:
        return "data_policy:retention_unknown_or_retains"
    regions = [r.lower() for r in pol.get("allowed_regions") or []]
    if regions:
        prov_regions = [r.lower() for r in provider.regions or []]
        if not prov_regions:
            return "data_policy:processing_region_unknown"
        if not set(prov_regions) <= set(regions):
            return "data_policy:region_not_allowed"
    return None


def _capability_reason(m: Model, opts: RouteOptions, explicit: bool = False) -> str | None:
    caps = m.capabilities or {}
    if caps.get("chat") is False:
        return "not_a_chat_model"
    if m.lifecycle == "removed":
        return "model_removed"
    if m.lifecycle == "catalog":
        # Listed by the imported catalogue only; the provider's own model list has not confirmed it.
        return "catalog_model_unconfirmed"
    for feat in sorted(opts.require):
        # A model the caller named explicitly is tried when a capability is
        # merely unknown (many listings, e.g. custom OpenAI-compatible servers,
        # say nothing about tools); only a stated lack rules it out. "auto"
        # stays strict and picks models known to have the capability.
        if explicit and caps.get(feat) is None:
            continue
        if caps.get(feat) is not True:
            return f"missing_capability:{feat}"
    if m.context_window and opts.input_tokens + opts.max_output_tokens > m.context_window:
        return "context_window_exceeded"
    if m.max_output_tokens and opts.max_output_tokens > m.max_output_tokens:
        return "max_output_tokens_exceeded"
    return None


def _quota_view(
    pools: list[QuotaPool], now: datetime, allow_unknown: bool
) -> tuple[str | None, float | None, float | None, bool]:
    """Return (rejection reason, min remaining fraction, seconds to reset of scarcest pool, metered)."""
    frac: float | None = None
    reset_in: float | None = None
    for p in pools:
        avail = ledger.available(p, now)
        if avail is None:
            if not allow_unknown:
                return "quota_limit_unknown", None, None, True
            continue
        if avail <= 0:
            if p.exhausted_until is not None and p.exhausted_until > now:
                until: datetime | None = p.exhausted_until
            else:
                until = next_reset(
                    QuotaPeriod(p.period),
                    now,
                    p.reset_tz,
                    rolling_start=p.window_start if p.rolling else None,
                )
            return "quota_exhausted", 0.0, (until - now).total_seconds() if until else None, True
        limit = p.limit_value or 1
        f = avail / limit
        if frac is None or f < frac:
            frac = f
            nr = next_reset(
                QuotaPeriod(p.period), now, p.reset_tz, rolling_start=p.window_start if p.rolling else None
            )
            reset_in = (nr - now).total_seconds() if nr else None
    return None, frac, reset_in, bool(pools)


async def build_plan(
    session: AsyncSession,
    project: Project,
    opts: RouteOptions,
    *,
    exclude: set[tuple[str, str]] | None = None,
    now: datetime | None = None,
) -> RoutePlan:
    now = now or utcnow()
    exclude = exclude or set()
    rejected: list[Rejection] = []

    conns = (
        (
            await session.execute(
                select(Connection)
                .where(
                    Connection.project_id == project.id, Connection.status != ConnectionStatus.REVOKED.value
                )
                # Deterministic tie-break between equally ranked routes: the older connection first, on
                # every database (row order is unspecified without ORDER BY, e.g. on PostgreSQL).
                .order_by(Connection.created_at, Connection.id)
            )
        )
        .scalars()
        .all()
    )
    provider_ids = sorted({c.provider_id for c in conns})
    providers = {
        p.id: p
        for p in (
            await session.execute(select(Provider).where(Provider.id.in_(provider_ids or [""])))
        ).scalars()
    }
    models = list(
        (
            await session.execute(
                select(Model).where(Model.provider_id.in_(provider_ids or [""])).order_by(Model.upstream_id)
            )
        ).scalars()
    )
    offer_ids = [c.offer_id for c in conns if c.offer_id]
    offers = {
        o.id: o
        for o in (await session.execute(select(Offer).where(Offer.id.in_(offer_ids or [""])))).scalars()
    }
    user_ids = [c.created_by for c in conns if c.created_by]
    users = {
        u.id: u for u in (await session.execute(select(User).where(User.id.in_(user_ids or [""])))).scalars()
    }
    health = await breaker.load(session, [c.id for c in conns])
    pools_all = list(
        (await session.execute(select(QuotaPool).where(QuotaPool.project_id == project.id))).scalars()
    )

    # Requested model resolution: "auto", "provider/model" (pinned), or an upstream id / alias.
    name = (opts.requested_model or "auto").strip()
    pinned_provider: str | None = None
    if "/" in name and name.split("/", 1)[0] in providers:
        pinned_provider, name = name.split("/", 1)
    auto = name in ("", "auto")

    def wanted(m: Model) -> bool:
        if pinned_provider is not None and m.provider_id != pinned_provider:
            return False
        return auto or _model_matches(m, name)

    if not auto and not any(wanted(m) for m in models):
        rejected.append(
            Rejection(pinned_provider or "*", name, None, "model_not_found_in_authorized_connections")
        )

    spend = SPEND_FREE_ONLY if opts.free_only else spend_mode(project)
    free_only = spend == SPEND_FREE_ONLY
    # Demo providers (`oasis-route dev` mocks) answer with canned echoes. Once a real provider is active,
    # ``auto`` must never pick them, on any path a connection was created by; naming a mock model still works.
    real_active = any(
        c.status == ConnectionStatus.ACTIVE.value and c.provider_id not in DEMO_PROVIDER_IDS for c in conns
    )
    survivors: list[Candidate] = []
    pending: list[Candidate] = []
    headroom = budgets.tightest(await budgets.for_project(session, project.id), now)
    static_ok_quality: list[float | None] = []
    for c in conns:
        prov = providers.get(c.provider_id)
        if prov is None:
            continue
        conn_reason: str | None = None
        if c.status != ConnectionStatus.ACTIVE.value:
            conn_reason = f"connection_{c.status}"
        elif prov.status != "active":
            conn_reason = f"provider_{prov.status}"
        elif opts.providers and prov.id not in opts.providers:
            conn_reason = "not_in_requested_providers"
        elif auto and pinned_provider is None and real_active and prov.id in DEMO_PROVIDER_IDS:
            conn_reason = "demo_provider_superseded"
        else:
            offer = offers.get(c.offer_id) if c.offer_id else None
            if c.offer_id and (
                offer is None
                or offer.state not in CONNECTABLE_OFFER_STATES
                or (offer.expires_at is not None and offer.expires_at <= now)
            ):
                conn_reason = f"offer_not_usable:{offer.state if offer else 'missing'}"
            else:
                owner = users.get(c.created_by or "")
                if owner is not None:
                    reasons = eligibility_reasons(owner, prov, offer, c.auth_method)
                    if reasons:
                        conn_reason = f"ineligible:{reasons[0]}"
                if conn_reason is None:
                    conn_reason = _policy_reason(project, prov)
        for m in models:
            if m.provider_id != c.provider_id or not wanted(m):
                continue
            if c.allowed_models and m.upstream_id not in c.allowed_models:
                continue
            if conn_reason:
                rejected.append(Rejection(prov.id, m.upstream_id, c.id, conn_reason))
                continue
            basis = cost_basis(c, prov, m)
            needs_approval = (
                spend == SPEND_BOUNDED
                and basis in ("paid", "unknown")
                and opts.approved_paid != (c.id, m.upstream_id)
            )
            if free_only and basis in ("paid", "unknown"):
                rejected.append(
                    Rejection(
                        prov.id,
                        m.upstream_id,
                        c.id,
                        "paid_route_blocked" if basis == "paid" else "cost_unknown",
                    )
                )
                continue
            reason = _capability_reason(m, opts, explicit=not auto)
            q, q_src = quality_of(m, opts.task)
            if reason is None and opts.min_quality is not None and (q is None or q < opts.min_quality):
                reason = "below_min_quality"
            if reason:
                rejected.append(Rejection(prov.id, m.upstream_id, c.id, reason))
                continue
            static_ok_quality.append(q)
            if (c.id, m.upstream_id) in exclude or (c.id, "*") in exclude:
                rejected.append(Rejection(prov.id, m.upstream_id, c.id, "excluded_after_failure"))
                continue
            av = breaker.evaluate(breaker.records_for(health, c.id, m.upstream_id), now)
            if not av.routable:
                retry_in = (av.retry_at - now).total_seconds() if av.retry_at else None
                rejected.append(Rejection(prov.id, m.upstream_id, c.id, f"health:{av.reason}", retry_in))
                continue
            pools = [
                p
                for p in pools_all
                if p.provider_id == prov.id
                and p.account_scope == c.upstream_account_ref
                and p.model_scope in ledger.scopes_for(m.upstream_id)
            ]
            q_reason, frac, reset_in, metered = _quota_view(pools, now, project.allow_unknown_quota)
            if q_reason:
                rejected.append(Rejection(prov.id, m.upstream_id, c.id, q_reason, reset_in))
                continue
            rec = breaker.records_for(health, c.id, breaker.CONNECTION_SCOPE)
            est = route_cost(basis, m.pricing, opts.input_tokens, opts.max_output_tokens)
            if headroom is not None and est.status != "free":
                if est.usd_micros is None:
                    rejected.append(Rejection(prov.id, m.upstream_id, c.id, "budget_cost_unknown"))
                    continue
                if est.usd_micros > headroom.remaining_usd_micros:
                    rejected.append(
                        Rejection(prov.id, m.upstream_id, c.id, "budget_exceeded", headroom.resets_in_s)
                    )
                    continue
            (pending if needs_approval else survivors).append(
                Candidate(
                    connection_id=c.id,
                    provider_id=prov.id,
                    adapter_key=prov.adapter_key,
                    account_scope=c.upstream_account_ref,
                    multi_account_permitted=prov.multi_account_permitted,
                    model_id=m.id,
                    upstream_model=m.upstream_id,
                    quality=q,
                    quality_source=q_src,
                    reliability=av.reliability,
                    latency_ms=rec[0].ewma_latency_ms if rec else None,
                    quota_fraction=frac,
                    reset_in_s=reset_in,
                    quota_metered=metered,
                    needs_probe=av.needs_probe,
                    priority=c.priority,
                    preference_rank=opts.providers.index(prov.id) if prov.id in opts.providers else 0,
                    prior=model_prior(m.upstream_id, opts.task) if q is None else None,
                    cost_basis=basis,
                    cost_status=est.status,
                    est_cost_usd_micros=est.usd_micros,
                    price_verified=est.verified,
                    context_window=m.context_window,
                    pricing=dict(m.pricing or {}),
                )
            )

    rated = [q for q in static_ok_quality if q is not None]
    best = max(rated) if rated else None
    plan = RoutePlan(opts, [], rejected, best, pinned_provider is not None or not auto, free_only, spend)
    for cand in pending:
        rejected.append(
            Rejection(cand.provider_id, cand.upstream_model, cand.connection_id, "approval_required")
        )
    pending.sort(key=lambda c: (_cost_key(c), rank_key(c, opts.mode, opts)))
    plan.approval_candidates = pending[:3]
    final: list[Candidate] = []
    for cand in survivors:
        if not opts.allow_downgrade and plan.is_downgrade(cand):
            rejected.append(
                Rejection(cand.provider_id, cand.upstream_model, cand.connection_id, "downgrade_not_allowed")
            )
            continue
        final.append(cand)
    if spend == SPEND_FREE_ONLY:
        final.sort(key=lambda c: rank_key(c, opts.mode, opts))
    else:  # bounded_autonomy and continuity always prefer free routes; paid ones are fallback only
        final.sort(key=lambda c: (c.cost_status != "free", rank_key(c, opts.mode, opts)))
    if opts.mode is RoutingMode.DIVERSITY:  # diversify within free, then within paid: never paid before free
        final = diversify([c for c in final if c.cost_status == "free"]) + diversify(
            [c for c in final if c.cost_status != "free"]
        )
    if not opts.allow_fallback:
        final = final[:1]
    plan.candidates = final
    return plan


def route_cost(
    basis: str, pricing: dict[str, Any] | None, input_tokens: int, output_tokens: int
) -> CostEstimate:
    """Cost of a request on a route given its cost basis (a declared free-tier account costs nothing)."""
    if basis == "free_tier_account":
        return CostEstimate("free", 0, False, "account_declared_free_tier")
    return estimate_cost(pricing, input_tokens, output_tokens, local=basis == "local")


def diversify(cands: list[Candidate]) -> list[Candidate]:
    """Keep the ranked order but take one candidate per provider per round, so fallback crosses providers."""
    buckets: dict[str, list[Candidate]] = {}
    for c in cands:
        buckets.setdefault(c.provider_id, []).append(c)
    out: list[Candidate] = []
    while any(buckets.values()):
        for pid in list(buckets):
            if buckets[pid]:
                out.append(buckets[pid].pop(0))
    return out


_UNKNOWN_COST = 1e18


def _cost_key(c: Candidate) -> float:
    if c.cost_status == "free":
        return 0.0
    if c.est_cost_usd_micros is None:
        return _UNKNOWN_COST
    # An unverified (estimated) price ranks after an equal verified one.
    return float(c.est_cost_usd_micros) + (0.0 if c.price_verified else 0.5)


def rank_key(c: Candidate, mode: RoutingMode, opts: RouteOptions | None = None) -> tuple[float, ...]:
    # Rated candidates (tier 0) always precede unrated ones (tier 1); unrated are ordered by heuristic prior.
    if c.quality is None:
        q_tier, q_val = 1.0, c.prior if c.prior is not None else 0.0
    else:
        q_tier, q_val = 0.0, c.quality
    q_key = -math.floor(q_val / 10.0) if mode is RoutingMode.BALANCED else -q_val
    rel_key = -round(c.reliability, 1 if mode is RoutingMode.STRICT_QUALITY else 2)
    lat_key = round((c.latency_ms if c.latency_ms is not None else 5_000.0) / 250.0)
    # Use-it-or-lose-it: prefer capacity that resets sooner, then the less scarce pool.
    reset_key = round((c.reset_in_s if c.reset_in_s is not None else 86_400.0 * 31) / 3600.0)
    frac_key = -(c.quota_fraction if c.quota_fraction is not None else 0.5)
    pref = (float(c.preference_rank), float(-c.priority))
    quality = (q_tier, q_key)
    if mode is RoutingMode.LOWEST_COST:
        return (*pref, _cost_key(c), *quality, rel_key, lat_key)
    if mode is RoutingMode.MOST_QUOTA:
        # Unmetered (no known pool) sorts after metered capacity with headroom: its limit is unknown.
        frac = c.quota_fraction if c.quota_fraction is not None else 0.0
        return (*pref, -round(frac, 2), *quality, rel_key, lat_key)
    if mode is RoutingMode.LOWEST_LATENCY:
        lat = c.latency_ms if c.latency_ms is not None else 5_000.0
        return (*pref, round(lat / 50.0), *quality, rel_key)
    if mode is RoutingMode.RELIABILITY:
        return (*pref, -round(c.reliability, 2), *quality, lat_key)
    if mode is RoutingMode.CONTEXT_FIT:
        need = (opts.input_tokens + opts.max_output_tokens) if opts else 0
        # Tightest fit first keeps large-context routes for large requests; unknown windows last.
        headroom = (c.context_window - need) if c.context_window else 1e12
        return (*pref, float(headroom), *quality, rel_key)
    if mode is RoutingMode.RESET_AWARE:
        return (*pref, reset_key, frac_key, *quality, rel_key)
    return (*pref, q_tier, q_key, rel_key, lat_key, reset_key, frac_key)
