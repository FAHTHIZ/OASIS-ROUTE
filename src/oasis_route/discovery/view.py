"""The Discover view: every provider, what it actually takes to get access, and what to do next.

This is a read model assembled from existing state only (providers, offers, evidence, models, connections,
sources, worker leases). It never fetches anything and never changes state. Facts that are not verified are
reported as ``unknown`` / ``unverified`` rather than guessed:

- Offers are shown to non-admins only once a reviewer made them ``connectable`` or ``active``; unreviewed
  offers are counted (``pending_review``) so users know more may be coming, but their claims are not shown.
- ``card_required`` comes from extracted evidence when present, otherwise from the documented access hint, and
  is labelled with which of the two it is.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oasis_route.connections import oauth
from oasis_route.connections.access import access_for, access_path_for
from oasis_route.context import AppContext
from oasis_route.db.models import (
    Connection,
    Evidence,
    Model,
    Offer,
    Project,
    Provider,
    ProviderCandidate,
    Source,
    WorkerLease,
    utcnow,
)
from oasis_route.domain.enums import ConnectionStatus, OfferState, SourceReviewStatus

PUBLIC_STATES = {OfferState.CONNECTABLE.value, OfferState.ACTIVE.value}
REVIEW_STATES = {
    OfferState.DISCOVERED.value,
    OfferState.NEEDS_REVIEW.value,
    OfferState.VERIFIED_ELIGIBLE.value,
}
DISCOVERY_JOB = "discovery.sources"


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def _quota_summary(claims: list[Any]) -> list[dict[str, Any]]:
    """Account-wide quotas first, then the most generous per-model ones; at most six for a card."""
    rows = [q for q in claims or [] if isinstance(q, dict) and q.get("limit") is not None]
    rows.sort(key=lambda q: (q.get("model_scope", "*") != "*", -(q.get("limit") or 0)))
    out = []
    for q in rows[:6]:
        out.append(
            {
                "limit": q.get("limit"),
                "unit": q.get("unit"),
                "period": q.get("period"),
                "model_scope": q.get("model_scope", "*"),
                "tier": q.get("tier"),
            }
        )
    return out


def _offer_card(o: Offer, evidence: list[Evidence], *, admin: bool) -> dict[str, Any]:
    urls = sorted({e.url for e in evidence})[:5]
    models = [m for m in o.models or [] if isinstance(m, dict)]
    return {
        "id": o.id,
        "title": o.title,
        "state": o.state,
        "offer_class": o.offer_class,
        "access_mode": o.access_mode,
        "access_path": o.access_path,
        "credit_type": o.credit_type,
        "quotas": _quota_summary(o.quota_claims),
        "models": {
            "count": len(models),
            "free": sorted({str(m.get("model")) for m in models if m.get("free")})[:12],
            "sample": sorted({str(m.get("model")) for m in models})[:12],
        },
        "billing": o.billing or {},
        "eligibility": o.eligibility or {},
        "regions": o.regions or [],
        "expires_at": _iso(o.expires_at),
        "first_observed_at": _iso(o.first_observed_at),
        "last_observed_at": _iso(o.last_observed_at),
        "confidence": o.confidence,
        "evidence": {"count": len(evidence), "urls": urls},
        "verified": o.state in PUBLIC_STATES,
        **({"admin_review_needed": o.state in REVIEW_STATES} if admin else {}),
    }


def _next_action(
    provider: Provider,
    *,
    connection: Connection | None,
    oauth_ok: bool,
    links: dict[str, Any],
    verified_offers: int,
) -> dict[str, Any]:
    if provider.status != "active":
        return {"kind": "unavailable", "label": f"Provider is {provider.status}"}
    if connection is not None:
        if connection.status == ConnectionStatus.ACTIVE.value:
            if connection.billing_mode in ("unknown", "paid"):
                return {
                    "kind": "confirm_billing",
                    "label": "Connected. Confirm the account is free-tier so free-only routing can use it.",
                    "connection_id": connection.id,
                }
            return {"kind": "connected", "label": "Connected", "connection_id": connection.id}
        return {
            "kind": "fix_connection",
            "label": "Connection needs attention: reconnect or rotate the key.",
            "connection_id": connection.id,
        }
    if oauth_ok:
        return {"kind": "connect_oauth", "label": f"Sign in with {provider.name}"}
    if links.get("api_key"):
        return {
            "kind": "connect_api_key",
            "label": "Create a key in the provider's console, then paste it here.",
            "url": links["api_key"],
        }
    if verified_offers:
        return {"kind": "connect_api_key", "label": "Paste an API key from this provider."}
    return {"kind": "learn_more", "label": "No verified free access yet.", "url": links.get("docs")}


async def discovery_status(session: AsyncSession) -> dict[str, Any]:
    now = utcnow()
    lease = await session.get(WorkerLease, DISCOVERY_JOB)
    srcs = list((await session.execute(select(Source))).scalars())
    approved = [s for s in srcs if s.review_status == SourceReviewStatus.APPROVED.value]
    failing = [s for s in approved if (s.failure_count or 0) > 0]
    offers = list((await session.execute(select(Offer))).scalars())
    day = now - timedelta(days=1)
    next_due = min((s.next_fetch_at for s in approved if s.next_fetch_at), default=None)
    candidates = (
        await session.execute(
            select(func.count()).select_from(ProviderCandidate).where(ProviderCandidate.status == "candidate")
        )
    ).scalar_one()
    return {
        # The scheduled job's last success, or the latest fetch when sources were run by hand.
        "last_run_at": _iso(
            (lease.last_success_at if lease else None)
            or max((x.last_fetched_at for x in srcs if x.last_fetched_at), default=None)
        ),
        "last_error": lease.last_error if lease else None,
        "next_scan_at": _iso(next_due),
        "sources": {
            "total": len(srcs),
            "approved": len(approved),
            "pending": sum(1 for s in srcs if s.review_status == SourceReviewStatus.PENDING.value),
            "failing": len(failing),
            "seeded": sum(1 for s in srcs if s.origin == "seed"),
            "frontier": sum(1 for s in srcs if s.origin == "frontier"),
        },
        "offers": {
            "verified": sum(1 for o in offers if o.state in PUBLIC_STATES),
            "pending_review": sum(1 for o in offers if o.state in REVIEW_STATES),
            "expired": sum(1 for o in offers if o.state == OfferState.EXPIRED.value),
            "updated_24h": sum(1 for o in offers if o.last_observed_at and o.last_observed_at >= day),
        },
        "provider_candidates": int(candidates),
    }


async def discover(
    ctx: AppContext, session: AsyncSession, *, project: Project | None, admin: bool
) -> dict[str, Any]:
    providers = list((await session.execute(select(Provider).order_by(Provider.name))).scalars())
    offers = list((await session.execute(select(Offer))).scalars())
    ev_rows = list(
        (
            await session.execute(
                select(Evidence).where(Evidence.offer_id.in_([o.id for o in offers] or [""]))
            )
        ).scalars()
    )
    evidence: dict[str, list[Evidence]] = {}
    for e in ev_rows:
        evidence.setdefault(e.offer_id or "", []).append(e)
    model_rows = (
        await session.execute(select(Model.provider_id, Model.pricing).where(Model.lifecycle != "removed"))
    ).all()
    total_models: dict[str, int] = {}
    free_models: dict[str, int] = {}
    for pid, pricing in model_rows:
        total_models[pid] = total_models.get(pid, 0) + 1
        if (pricing or {}).get("free") is True:
            free_models[pid] = free_models.get(pid, 0) + 1
    conns: dict[str, Connection] = {}
    if project is not None:
        for c in (
            await session.execute(
                select(Connection)
                .where(
                    Connection.project_id == project.id, Connection.status != ConnectionStatus.REVOKED.value
                )
                .order_by(Connection.created_at)
            )
        ).scalars():
            conns.setdefault(c.provider_id, c)

    cards: list[dict[str, Any]] = []
    for p in providers:
        if p.id.startswith("mock") and not ctx.settings.mock_providers_enabled:
            continue
        info = access_for(p.id)
        mine = [o for o in offers if o.provider_id == p.id]
        visible = [o for o in mine if admin or o.state in PUBLIC_STATES]
        oauth_ok = oauth.available(ctx, p.id)
        links = {
            k: v
            for k, v in {
                "signup": info.signup,
                "console": info.console,
                "api_key": info.api_key,
                "docs": info.docs,
                "pricing": info.pricing,
                "rate_limits": info.rate_limits,
                "terms": p.terms_url,
            }.items()
            if v
        }
        for o in visible:  # reviewed offer links override documented defaults
            for k, v in (o.links or {}).items():
                if o.state in PUBLIC_STATES and isinstance(v, str) and v.startswith("https://"):
                    links[k] = v
        card_from_evidence = next(
            (
                o.billing.get("card_required")
                for o in visible
                if isinstance(o.billing, dict) and isinstance(o.billing.get("card_required"), bool)
            ),
            None,
        )
        conn = conns.get(p.id)
        verified = sum(1 for o in mine if o.state in PUBLIC_STATES)
        cards.append(
            {
                "id": p.id,
                "name": p.name,
                "status": p.status,
                "access_path": access_path_for(p.id, p.adapter_key, oauth_available=oauth_ok),
                "oauth_available": oauth_ok,
                "free_tier_hint": info.free_tier_hint or None,
                "card_required": card_from_evidence if card_from_evidence is not None else info.card_required,
                "card_required_source": (
                    "evidence"
                    if card_from_evidence is not None
                    else ("documented" if info.card_required is not None else "unknown")
                ),
                "default_billing_mode": info.default_billing_mode,
                "regions": p.regions or [],
                "min_age": p.min_age,
                "links": links,
                "models": {"total": total_models.get(p.id, 0), "free": free_models.get(p.id, 0)},
                "offers": [_offer_card(o, evidence.get(o.id, []), admin=admin) for o in visible],
                "pending_review": sum(1 for o in mine if o.state in REVIEW_STATES),
                "connection": (
                    {
                        "id": conn.id,
                        "status": conn.status,
                        "billing_mode": conn.billing_mode,
                        "auth_method": conn.auth_method,
                    }
                    if conn
                    else None
                ),
                "next_action": _next_action(
                    p, connection=conn, oauth_ok=oauth_ok, links=links, verified_offers=verified
                ),
            }
        )
    # Most useful first: free capacity you can get now, then by name.
    cards.sort(
        key=lambda c: (
            c["connection"] is None and c["status"] != "active",
            -(1 if c["models"]["free"] or any(o["verified"] for o in c["offers"]) else 0),
            c["default_billing_mode"] == "paid",
            c["name"].lower(),
        )
    )
    status = await discovery_status(session)
    free_connected = sum(
        1
        for c in conns.values()
        if c.status == ConnectionStatus.ACTIVE.value and c.billing_mode == "free_tier"
    )
    status["project"] = (
        {
            "id": project.id,
            "spend_policy": project.spend_policy,
            "connected": sum(1 for c in conns.values() if c.status == ConnectionStatus.ACTIVE.value),
            "free_tier_connections": free_connected,
            "action_required": [
                c["id"] for c in cards if c["next_action"]["kind"] in ("fix_connection", "confirm_billing")
            ],
        }
        if project is not None
        else None
    )
    return {"generated_at": utcnow().isoformat(), "status": status, "providers": cards}
