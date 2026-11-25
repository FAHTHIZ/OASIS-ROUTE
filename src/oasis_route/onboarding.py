"""Turnkey onboarding: detect local servers, show connectable free providers, classify routes, set defaults.

The flow (docs/ONBOARDING.md):

1. start Oasis Route (``oasis-route dev`` or ``serve``);
2. detect local OpenAI-compatible servers (Ollama, llama.cpp, vLLM, LM Studio, ...) on their default ports;
3. list providers with a **verified** free offer that the user can connect, plus advertised (unreviewed)
   leads clearly labelled as such;
4. connect an account or key (existing connection / quick-connect APIs);
5. credentials are validated and models discovered by the provider's own model list;
6. classify every route as ``free`` / ``paid`` / ``unknown`` and ``eligible`` / ``blocked`` / ``exhausted``;
7. apply a sensible default route set: zero-budget stays on, local servers are last-resort fallbacks;
8. one OpenAI-compatible endpoint: ``<base>/v1`` with model ``auto``.

Nothing here ever enables a paid route: setup never changes ``spend_policy`` or a connection's
``billing_mode``.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oasis_route.context import AppContext
from oasis_route.db.models import Connection, Model, Offer, Project, Provider, User
from oasis_route.domain.enums import ConnectionStatus
from oasis_route.registry.catalog import DEMO_PROVIDER_IDS, LOCAL_SERVER_IDS, LOCAL_SERVERS
from oasis_route.registry.imported import UNROUTABLE_LIFECYCLES
from oasis_route.routing.router import RouteOptions, build_plan, cost_basis

LOCAL_PRIORITY = -10  # local servers rank after hosted free tiers: an uncapped last resort
DETECT_TIMEOUT_S = 1.0
VERIFIED_OFFER_STATES = ("verified_eligible", "connectable", "active")
FREE_BASES = {"local": "local", "free_model": "provider_price_list", "free_tier_account": "account_declared"}
EXHAUSTED_PREFIXES = ("quota_exhausted", "health:")
# Hosted providers whose own documentation describes a no-card free plan or trial key, in the order a new
# user gets the most from them. Display-only guidance (links from connections/access.py); a route only
# becomes free through the provider's price list or the owner declaring the account free-tier.
FREE_KEY_ORDER = (
    "groq",
    "gemini",
    "openrouter",
    "cerebras",
    "mistral",
    "sambanova",
    "nvidia",
    "cohere",
    "zai",
    "huggingface",
)


# ------------------------------------------------------------------------------------------ local servers
async def _probe(client: httpx.AsyncClient, pid: str, name: str, base: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "provider_id": pid,
        "name": name,
        "base_url": base,
        "reachable": False,
        "models": [],
    }
    try:
        r = await client.get(base.rstrip("/") + "/models", timeout=DETECT_TIMEOUT_S)
    except httpx.HTTPError as exc:
        out["error"] = type(exc).__name__
        return out
    if r.status_code != 200:
        out["error"] = f"HTTP {r.status_code}"
        return out
    try:
        data = json.loads(r.content[:2_000_000])
        items = data.get("data") or data.get("models") or []
        models = sorted(
            {str(m.get("id") or m.get("name") or m.get("model")) for m in items if isinstance(m, dict)}
            - {"None"}
        )
    except (ValueError, AttributeError):
        out["error"] = "not an OpenAI-compatible model list"
        return out
    out.update(reachable=True, models=models[:200])
    return out


async def detect_local_servers(ctx: AppContext) -> dict[str, Any]:
    """Probe each local preset's default ``/v1/models`` (loopback only, 1 s timeout, no credentials sent).

    Only runs when private upstreams are allowed: on a shared host the server must not probe its own
    loopback interface on a user's behalf.
    """
    if not ctx.settings.allow_private_upstreams:
        return {
            "enabled": False,
            "reason": "set OASIS_ROUTE_ALLOW_PRIVATE_UPSTREAMS=true on a single-user host "
            "to use local servers",
            "servers": [],
        }
    own = {f"127.0.0.1:{ctx.settings.port}", f"localhost:{ctx.settings.port}"}
    targets = [(pid, name, url) for pid, name, url, _ in LOCAL_SERVERS if url.split("/")[2] not in own]
    results = await asyncio.gather(*(_probe(ctx.upstream_client, *t) for t in targets))
    return {"enabled": True, "servers": list(results)}


# --------------------------------------------------------------------------------------- classification
def _state(reason: str | None) -> str:
    if reason is None:
        return "eligible"
    if reason.startswith(EXHAUSTED_PREFIXES):
        return "exhausted"
    return "blocked"


async def classify_routes(session: AsyncSession, project: Project) -> dict[str, Any]:
    """Every (connection, model) route of the project with cost class and state, and the reason."""
    plan = await build_plan(session, project, RouteOptions())
    conns = {
        c.id: c
        for c in (
            await session.execute(select(Connection).where(Connection.project_id == project.id))
        ).scalars()
    }
    pids = sorted({c.provider_id for c in conns.values()}) or [""]
    providers = {
        p.id: p for p in (await session.execute(select(Provider).where(Provider.id.in_(pids)))).scalars()
    }
    models = {
        (m.provider_id, m.upstream_id): m
        for m in (await session.execute(select(Model).where(Model.provider_id.in_(pids)))).scalars()
    }

    def row(
        conn_id: str | None, provider_id: str, upstream: str, reason: str | None, **extra: Any
    ) -> dict[str, Any]:
        c = conns.get(conn_id or "")
        prov = providers.get(provider_id)
        m = models.get((provider_id, upstream))
        basis = cost_basis(c, prov, m) if c and prov and m else "unknown"
        cost_class = "free" if basis in FREE_BASES else basis
        return {
            "connection_id": conn_id,
            "demo": provider_id in DEMO_PROVIDER_IDS,
            "provider_id": provider_id,
            "model": upstream,
            "route": f"{provider_id}/{upstream}",
            "cost_class": cost_class,
            "cost_basis": basis,
            "free_evidence": FREE_BASES.get(basis),
            "state": _state(reason),
            "reason": reason,
            "lifecycle": m.lifecycle if m else None,
            **extra,
        }

    routes = [
        row(c.connection_id, c.provider_id, c.upstream_model, None, rank=i)
        for i, c in enumerate(plan.candidates)
    ]
    unconfirmed = 0
    for r in plan.rejected:
        if r.connection_id is None:
            continue
        m = models.get((r.provider_id, r.upstream_model))
        if m is not None and m.lifecycle in UNROUTABLE_LIFECYCLES:
            # Retired, or only listed by the imported catalogue: not a route the provider offers this account.
            unconfirmed += m.lifecycle == "catalog"
            continue
        routes.append(
            row(r.connection_id, r.provider_id, r.upstream_model, r.reason, retry_in_s=r.retry_in_s)
        )
    summary = {
        "total": len(routes),
        "by_cost_class": dict(Counter(x["cost_class"] for x in routes)),
        "by_state": dict(Counter(x["state"] for x in routes)),
        # Demo (mock) routes answer with canned echoes: counted apart so "ready" means real models.
        "eligible_free": sum(
            1 for x in routes if x["state"] == "eligible" and x["cost_class"] == "free" and not x["demo"]
        ),
        "eligible_demo": sum(1 for x in routes if x["state"] == "eligible" and x["demo"]),
        "spend_policy": project.spend_policy,
        "free_only": plan.free_only,
        "unconfirmed_catalog_models": unconfirmed,
    }
    return {"summary": summary, "routes": routes}


# ----------------------------------------------------------------------------------- connectable providers
async def connectable_providers(session: AsyncSession, project_id: str) -> dict[str, list[dict[str, Any]]]:
    """Providers with a reviewed free offer (``verified``) and imported, unreviewed leads (``advertised``)."""
    connected = set(
        (
            await session.execute(
                select(Connection.provider_id).where(
                    Connection.project_id == project_id, Connection.status != ConnectionStatus.REVOKED.value
                )
            )
        ).scalars()
    )
    offers = list((await session.execute(select(Offer).where(Offer.access_mode == "free_tier"))).scalars())
    providers = {p.id: p for p in (await session.execute(select(Provider))).scalars()}
    verified: dict[str, dict[str, Any]] = {}
    advertised: dict[str, dict[str, Any]] = {}
    for o in offers:
        prov = providers.get(o.provider_id)
        if prov is None or prov.status != "active" or prov.id in LOCAL_SERVER_IDS:
            continue
        bucket = (
            verified if o.state in VERIFIED_OFFER_STATES else advertised if o.state == "discovered" else None
        )
        if bucket is None:
            continue
        entry = bucket.setdefault(
            prov.id,
            {
                "provider_id": prov.id,
                "name": prov.name,
                "connected": prov.id in connected,
                "verification": "verified" if bucket is verified else "advertised",
                "offers": [],
                "website": (prov.catalog or {}).get("website") or o.onboarding_url,
                "auth": (prov.catalog or {}).get("auth") or {},
            },
        )
        entry["offers"].append(
            {
                "id": o.id,
                "title": o.title,
                "state": o.state,
                "quota_claims": o.quota_claims,
                "confidence": o.confidence,
            }
        )
    for pid in verified:
        advertised.pop(pid, None)
    order = sorted(verified.values(), key=lambda e: (e["connected"], e["name"].lower()))
    leads = sorted(advertised.values(), key=lambda e: (e["connected"], e["name"].lower()))
    return {"verified": order, "advertised": leads}


def free_key_providers(providers: dict[str, Provider], connected: set[str]) -> list[dict[str, Any]]:
    """Where to get a free key for each reviewed hosted provider, most useful first (display-only)."""
    from oasis_route.connections.access import access_for
    from oasis_route.connections.key_detect import ENV_NAMES

    env_for: dict[str, str] = {}
    for name, pid in ENV_NAMES.items():
        env_for.setdefault(pid, name)
    out = []
    for pid in FREE_KEY_ORDER:
        prov = providers.get(pid)
        info = access_for(pid)
        if prov is None or prov.status != "active" or not info.api_key:
            continue
        out.append(
            {
                "provider_id": pid,
                "name": prov.name,
                "connected": pid in connected,
                "get_key_url": info.api_key,
                "env_var": env_for.get(pid),
                "free_tier_hint": info.free_tier_hint,
                "card_required": info.card_required,
            }
        )
    return out


async def billing_attention(
    session: AsyncSession, project_id: str, routes: dict[str, Any]
) -> list[dict[str, Any]]:
    """Active hosted connections with no free route because nobody has said whether billing is set up."""
    conns = list(
        (
            await session.execute(
                select(Connection).where(
                    Connection.project_id == project_id, Connection.status == ConnectionStatus.ACTIVE.value
                )
            )
        ).scalars()
    )
    free_by_conn = Counter(
        r["connection_id"] for r in routes["routes"] if r["state"] == "eligible" and r["cost_class"] == "free"
    )
    unknown_by_conn = Counter(r["connection_id"] for r in routes["routes"] if r["reason"] == "cost_unknown")
    return [
        {
            "connection_id": c.id,
            "provider_id": c.provider_id,
            "name": c.display_name,
            "blocked_routes": unknown_by_conn[c.id],
            "fix": "declare the account free-tier if no billing is set up on it "
            f"(PATCH /api/v1/projects/{project_id}/connections/{c.id} with billing_mode=free_tier, or the "
            "console's Get started page)",
        }
        for c in conns
        if c.billing_mode == "unknown" and unknown_by_conn[c.id] and not free_by_conn[c.id]
    ]


# ------------------------------------------------------------------------------------------------ setup
def endpoint_info(base_url: str) -> dict[str, Any]:
    base = base_url.rstrip("/")
    return {
        "base_url": f"{base}/v1",
        "model": "auto",
        "api_key": "your Oasis Key (OASIS_API_KEY)",
        "chat_completions": f"{base}/v1/chat/completions",
        "models": f"{base}/v1/models",
        "anthropic_messages": f"{base}/v1/messages",
    }


async def setup_plan(ctx: AppContext, project: Project, base_url: str) -> dict[str, Any]:
    local = await detect_local_servers(ctx)
    async with ctx.db.session() as session:
        fresh = await session.get(Project, project.id)
        assert fresh is not None
        connectable = await connectable_providers(session, project.id)
        routes = await classify_routes(session, fresh)
        attention = await billing_attention(session, project.id, routes)
        all_providers = {p.id: p for p in (await session.execute(select(Provider))).scalars()}
        connected_ids = set(
            (
                await session.execute(
                    select(Connection.provider_id).where(
                        Connection.project_id == project.id,
                        Connection.status != ConnectionStatus.REVOKED.value,
                    )
                )
            ).scalars()
        )
        local_conns = {
            c.provider_id
            for c in (
                await session.execute(
                    select(Connection).where(
                        Connection.project_id == project.id,
                        Connection.provider_id.in_(sorted(LOCAL_SERVER_IDS)),
                        Connection.status != ConnectionStatus.REVOKED.value,
                    )
                )
            ).scalars()
        }
    for s in local["servers"]:
        s["connected"] = s["provider_id"] in local_conns
    steps = [
        {"step": "start", "done": True},
        {
            "step": "detect_local_servers",
            "done": local["enabled"],
            "found": [s["provider_id"] for s in local["servers"] if s["reachable"]],
        },
        {"step": "connect_providers", "done": routes["summary"]["total"] > 0},
        {"step": "routing_ready", "done": routes["summary"]["eligible_free"] > 0},
    ]
    return {
        "steps": steps,
        "local_servers": local,
        "connectable_providers": connectable,
        "free_keys": free_key_providers(all_providers, connected_ids),
        "billing_attention": attention,
        "routes": routes,
        "endpoint": endpoint_info(base_url),
        "spend_policy": project.spend_policy,
        "paid_routes_enabled": False if project.spend_policy == "free_only" else None,
    }


async def apply_setup(
    ctx: AppContext, project_id: str, user: User, *, connect_local: bool, base_url: str
) -> dict[str, Any]:
    """Connect reachable local servers (last-resort priority) and return the resulting plan.

    Never changes the spend policy or any billing mode, so no paid route can become routable here.
    """
    from oasis_route.connections import service as conns

    created: list[dict[str, Any]] = []
    if connect_local:
        local = await detect_local_servers(ctx)
        for s in local["servers"]:
            if not s["reachable"]:
                continue
            async with ctx.db.transaction() as session:
                exists = (
                    await session.execute(
                        select(Connection.id).where(
                            Connection.project_id == project_id,
                            Connection.provider_id == s["provider_id"],
                            Connection.status != ConnectionStatus.REVOKED.value,
                        )
                    )
                ).first()
                if exists is not None:
                    continue
                c = await conns.create_connection(
                    ctx,
                    session,
                    project_id=project_id,
                    user=user,
                    data=conns.ConnectionInput(
                        provider_id=s["provider_id"],
                        secret=None,
                        base_url=s["base_url"],
                        display_name=f"{s['name']} (detected)",
                        priority=LOCAL_PRIORITY,
                        accept_terms=True,
                    ),
                )
                cid = c.id
            result = await conns.validate(ctx, cid, user.id)
            created.append(
                {"provider_id": s["provider_id"], "connection_id": cid, "validation": result.__dict__}
            )
    async with ctx.db.session() as session:
        project = await session.get(Project, project_id)
        assert project is not None
    plan = await setup_plan(ctx, project, base_url)
    plan["connected_local"] = created
    return plan
