"""Capacity analytics: advertised, verified, authorized, usable, consumed, and forecast.

Rules (ROUTING_AND_QUOTAS section 9):
- Values are reported per *unit* and never mixed across units (input/output tokens stay separate).
- Recurring limits are normalised to a sustained per-day figure using the *binding* period of each
  allocation (e.g. 30 RPM and 1,000 RPD sustain 1,000 requests/day), so minute, day and month limits are
  never naively summed.
- One-time credits are reported separately and never presented as recurring.
- Pools are the unit of accounting: connections sharing one upstream account share one pool, which is
  counted once no matter how many connections or keys point at it.
- Unknown limits are listed as unknown and excluded from totals, never treated as unlimited.
- Usage is split into provider-confirmed and estimated.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oasis_route.connections.service import CONNECTABLE_OFFER_STATES
from oasis_route.db.models import Connection, Offer, Project, Provider, QuotaPool, UsageRecord, utcnow
from oasis_route.domain.enums import ConnectionStatus, OfferState, QuotaPeriod
from oasis_route.health import breaker
from oasis_route.quota import ledger
from oasis_route.quota.windows import next_reset, period_seconds

DAY = 86_400.0
VERIFIED_STATES = {OfferState.VERIFIED_ELIGIBLE.value, *CONNECTABLE_OFFER_STATES}
ADVERTISED_EXCLUDED = {OfferState.REJECTED.value, OfferState.INELIGIBLE.value, OfferState.REVOKED.value}


def per_day(limit: int, period: str) -> float | None:
    """Sustained per-day equivalent of a recurring limit; ``None`` for one-time/instantaneous limits."""
    secs = period_seconds(QuotaPeriod(period))
    if secs is None:
        return None
    return limit * (DAY / secs)


def binding_per_day(limits: list[tuple[int, str]]) -> float | None:
    """The tightest sustained per-day figure across one allocation's recurring limits for one unit."""
    vals = [v for lim, per in limits if (v := per_day(lim, per)) is not None]
    return min(vals) if vals else None


@dataclass
class _Alloc:
    provider_id: str
    account_scope: str
    model_scope: str


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 2)


def _claims_by_unit(claims: list[dict[str, Any]]) -> tuple[dict[str, list[tuple[int, str]]], dict[str, int]]:
    recurring: dict[str, list[tuple[int, str]]] = defaultdict(list)
    one_time: dict[str, int] = defaultdict(int)
    for c in claims:
        try:
            lim, unit, per = int(c["limit"]), str(c["unit"]), str(c["period"])
        except (KeyError, TypeError, ValueError):
            continue
        if per == QuotaPeriod.ONE_TIME.value:
            one_time[unit] += lim
        elif per != QuotaPeriod.NONE.value:
            recurring[unit].append((lim, per))
    return recurring, one_time


async def _offer_layer(session: AsyncSession, states: set[str] | None, excluded: set[str]) -> dict[str, Any]:
    q = select(Offer).where(Offer.state.notin_(list(excluded)))
    if states is not None:
        q = q.where(Offer.state.in_(list(states)))
    offers = (await session.execute(q.order_by(Offer.provider_id))).scalars().all()
    totals: dict[str, float] = defaultdict(float)
    one_time: dict[str, int] = defaultdict(int)
    items = []
    for o in offers:
        rec, ot = _claims_by_unit(list(o.quota_claims or []))
        per_unit = {u: _round(binding_per_day(v)) for u, v in rec.items()}
        for u, v in per_unit.items():
            if v is not None:
                totals[u] += v
        for u, v in ot.items():
            one_time[u] += v
        items.append(
            {
                "offer_id": o.id,
                "provider_id": o.provider_id,
                "title": o.title,
                "state": o.state,
                "credit_type": o.credit_type,
                "confidence": o.confidence,
                "expires_at": o.expires_at,
                "per_day": per_unit,
                "one_time": dict(ot),
            }
        )
    return {
        "per_day": {u: _round(v) for u, v in sorted(totals.items())},
        "one_time": dict(sorted(one_time.items())),
        "offers": items,
    }


async def project_report(
    session: AsyncSession, project: Project, now: datetime | None = None
) -> dict[str, Any]:
    now = now or utcnow()
    pools = list(
        (await session.execute(select(QuotaPool).where(QuotaPool.project_id == project.id))).scalars().all()
    )
    await ledger.roll_windows(session, pools, now)  # report against the current window
    conns = (
        (await session.execute(select(Connection).where(Connection.project_id == project.id))).scalars().all()
    )
    providers = {p.id: p for p in (await session.execute(select(Provider))).scalars()}
    offer_ids = [c.offer_id for c in conns if c.offer_id]
    offers = {
        o.id: o
        for o in (await session.execute(select(Offer).where(Offer.id.in_(offer_ids or [""])))).scalars()
    }
    health = await breaker.load(session, [c.id for c in conns])

    # Which (provider, account_scope) allocations have at least one authorized / routable connection.
    authorized: dict[tuple[str, str], list[str]] = defaultdict(list)
    routable: set[tuple[str, str]] = set()
    for c in conns:
        prov = providers.get(c.provider_id)
        if c.status != ConnectionStatus.ACTIVE.value or prov is None or prov.status != "active":
            continue
        offer = offers.get(c.offer_id) if c.offer_id else None
        if c.offer_id and (
            offer is None
            or offer.state not in CONNECTABLE_OFFER_STATES
            or (offer.expires_at is not None and offer.expires_at <= now)
        ):
            continue
        key = (c.provider_id, c.upstream_account_ref)
        authorized[key].append(c.id)
        av = breaker.evaluate(breaker.records_for(health, c.id, breaker.CONNECTION_SCOPE), now)
        if av.routable:
            routable.add(key)

    alloc_rows = []
    auth_per_day: dict[str, float] = defaultdict(float)
    auth_one_time: dict[str, int] = defaultdict(int)
    usable_now: dict[str, float] = defaultdict(float)
    usable_per_day: dict[str, float] = defaultdict(float)
    unknown: list[dict[str, Any]] = []
    concurrency: dict[str, int] = defaultdict(int)
    by_alloc: dict[tuple[str, str, str], list[QuotaPool]] = defaultdict(list)
    # (provider, account, unit) -> [(is_account_wide, sustained/day, headroom, one_time_left, routable)]
    contrib: dict[tuple[str, str, str], list[tuple[bool, float | None, int | None, int, bool]]] = defaultdict(
        list
    )
    for p in pools:
        by_alloc[(p.provider_id, p.account_scope, p.model_scope)].append(p)

    for (prov_id, scope, model_scope), ps in sorted(by_alloc.items()):
        key = (prov_id, scope)
        is_auth = key in authorized
        is_routable = key in routable
        units: dict[str, list[QuotaPool]] = defaultdict(list)
        for p in ps:
            units[p.unit].append(p)
        dims = []
        for unit, ups in sorted(units.items()):
            rec_limits = [
                (int(p.limit_value), p.period)
                for p in ups
                if p.limit_value is not None
                and p.period not in (QuotaPeriod.ONE_TIME.value, QuotaPeriod.NONE.value)
            ]
            sustained = binding_per_day(rec_limits)
            headroom: int | None = None
            unknown_here = False
            for p in ups:
                amt = ledger.available(p, now)
                if amt is None:
                    unknown_here = True
                    continue
                headroom = amt if headroom is None else min(headroom, amt)
            one_time_left = sum(
                max(0, int(p.limit_value) - p.used - p.reserved)
                for p in ups
                if p.period == QuotaPeriod.ONE_TIME.value and p.limit_value is not None
            )
            pools_out = [
                {
                    "pool_id": p.id,
                    "period": p.period,
                    "limit": p.limit_value,
                    "used": p.used,
                    "estimated_used": p.estimated_used,
                    "reserved": p.reserved,
                    "available": ledger.available(p, now),
                    "source": p.source,
                    "confidence": p.confidence,
                    "next_reset": next_reset(
                        QuotaPeriod(p.period),
                        now,
                        p.reset_tz,
                        rolling_start=p.window_start if p.rolling else None,
                    ),
                    "exhausted_until": p.exhausted_until
                    if p.exhausted_until and p.exhausted_until > now
                    else None,
                    "needs_recheck": p.needs_recheck,
                    "expires_at": p.expires_at,
                }
                for p in sorted(ups, key=lambda x: period_seconds(QuotaPeriod(x.period)) or 1e12)
            ]
            if unknown_here:
                unknown.append({"provider_id": prov_id, "account_scope": scope, "unit": unit})
            if is_auth:
                contrib[(prov_id, scope, unit)].append(
                    (model_scope == "*", sustained, headroom, one_time_left, is_routable)
                )
            dims.append(
                {
                    "unit": unit,
                    "sustained_per_day": _round(sustained),
                    "headroom_now": headroom,
                    "one_time_remaining": one_time_left or None,
                    "limit_unknown": unknown_here,
                    "pools": pools_out,
                }
            )
        alloc_rows.append(
            {
                "provider_id": prov_id,
                "account_scope": scope,
                "model_scope": model_scope,
                "connections": authorized.get(key, []),
                "authorized": is_auth,
                "routable_now": is_routable,
                "dimensions": dims,
            }
        )

    # Within one upstream account, per-model limits add up across models but an account-wide ("*") pool caps
    # them all, so the account contributes min(account-wide, sum of per-model). Accounts then add up.
    for (_prov, _scope, unit), parts in contrib.items():
        wide = [x for x in parts if x[0]]
        narrow = [x for x in parts if not x[0]]

        def _combine(vals_w: list[float | None], vals_n: list[float | None]) -> float | None:
            w = [v for v in vals_w if v is not None]
            n = [v for v in vals_n if v is not None]
            cap_w = min(w) if w else None
            sum_n = sum(n) if n else None
            if cap_w is None:
                return sum_n
            return cap_w if sum_n is None else min(cap_w, sum_n)

        if unit == "concurrent_requests":
            conc = _combine(
                [None if x[2] is None else float(x[2]) for x in wide],
                [None if x[2] is None else float(x[2]) for x in narrow],
            )
            concurrency[unit] += int(conc or 0)
            continue
        sus = _combine([x[1] for x in wide], [x[1] for x in narrow])
        if sus is not None:
            auth_per_day[unit] += sus
        auth_one_time[unit] += sum(x[3] for x in parts)
        r_wide = [x for x in wide if x[4]]
        r_narrow = [x for x in narrow if x[4]]
        if parts[0][4]:  # routability is a property of the account, shared by all its parts
            head = _combine(
                [None if x[2] is None else float(x[2]) for x in r_wide],
                [None if x[2] is None else float(x[2]) for x in r_narrow],
            )
            if head is not None:
                usable_now[unit] += head
                u_sus = _combine(
                    [x[1] if (x[2] or 0) > 0 else 0.0 for x in r_wide],
                    [x[1] if (x[2] or 0) > 0 else 0.0 for x in r_narrow],
                )
                if u_sus is not None:
                    usable_per_day[unit] += u_sus

    # Unmetered (no pools at all) authorized allocations are surfaced explicitly, not summed.
    metered = {(p.provider_id, p.account_scope) for p in pools}
    unmetered = [
        {"provider_id": k[0], "account_scope": k[1], "connections": v}
        for k, v in sorted(authorized.items())
        if k not in metered
    ]

    consumed = await consumed_summary(session, project.id, now)
    return {
        "project_id": project.id,
        "as_of": now,
        "notes": [
            "per_day values are sustained daily equivalents of each allocation's binding recurring limit",
            "one_time credits are never included in recurring figures",
            "unknown limits are excluded from totals; unmetered connections are listed separately",
        ],
        "advertised": await _offer_layer(session, None, ADVERTISED_EXCLUDED),
        "verified": await _offer_layer(session, VERIFIED_STATES, set()),
        "authorized": {
            "per_day": {u: _round(v) for u, v in sorted(auth_per_day.items())},
            "one_time_remaining": {u: v for u, v in sorted(auth_one_time.items()) if v},
            "concurrency": dict(concurrency),
        },
        "usable": {
            "headroom_now": {u: _round(v) for u, v in sorted(usable_now.items())},
            "per_day": {u: _round(v) for u, v in sorted(usable_per_day.items())},
        },
        "unknown_limits": unknown,
        "unmetered_connections": unmetered,
        "allocations": alloc_rows,
        "consumed": consumed,
        "forecast": forecast(alloc_rows, consumed, now),
    }


async def consumed_summary(session: AsyncSession, project_id: str, now: datetime) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for label, since in (
        ("last_hour", now - timedelta(hours=1)),
        ("last_24h", now - timedelta(days=1)),
        ("last_30d", now - timedelta(days=30)),
    ):
        rows = (
            await session.execute(
                select(
                    UsageRecord.provider_reported,
                    func.count(),
                    func.coalesce(func.sum(UsageRecord.input_tokens), 0),
                    func.coalesce(func.sum(UsageRecord.output_tokens), 0),
                )
                .where(
                    UsageRecord.project_id == project_id,
                    UsageRecord.created_at >= since,
                    UsageRecord.outcome.in_(["success", "cancelled"]),
                )
                .group_by(UsageRecord.provider_reported)
            )
        ).all()
        bucket: dict[str, Any] = {
            "confirmed": {"requests": 0, "input_tokens": 0, "output_tokens": 0},
            "estimated": {"requests": 0, "input_tokens": 0, "output_tokens": 0},
        }
        for reported, n, inp, outp in rows:
            b = bucket["confirmed" if reported else "estimated"]
            b["requests"] += int(n)
            b["input_tokens"] += int(inp)
            b["output_tokens"] += int(outp)
        errors = (
            await session.execute(
                select(func.count()).where(
                    UsageRecord.project_id == project_id,
                    UsageRecord.created_at >= since,
                    UsageRecord.outcome == "error",
                )
            )
        ).scalar_one()
        bucket["failed_attempts"] = int(errors)
        out[label] = bucket
    return out


def forecast(allocs: list[dict[str, Any]], consumed: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Linear burn-rate projection from the last hour. Explicit assumptions and confidence."""
    hour = consumed["last_hour"]
    rate = {
        "requests": hour["confirmed"]["requests"] + hour["estimated"]["requests"],
        "input_tokens": hour["confirmed"]["input_tokens"] + hour["estimated"]["input_tokens"],
        "output_tokens": hour["confirmed"]["output_tokens"] + hour["estimated"]["output_tokens"],
    }
    rate["total_tokens"] = rate["input_tokens"] + rate["output_tokens"]
    samples = rate["requests"]
    confidence = "low" if samples < 10 else ("medium" if samples < 100 else "high")
    items = []
    for a in allocs:
        if not a["authorized"]:
            continue
        for d in a["dimensions"]:
            r = rate.get(d["unit"])
            head = d["headroom_now"]
            if not r or head is None:
                continue
            # Project-wide rate is an upper bound for any single allocation's burn rate.
            hours = head / r
            exhaust_at = now + timedelta(hours=hours)
            soonest_reset = min(
                (p["next_reset"] for p in d["pools"] if p["next_reset"] is not None), default=None
            )
            items.append(
                {
                    "provider_id": a["provider_id"],
                    "account_scope": a["account_scope"],
                    "unit": d["unit"],
                    "headroom_now": head,
                    "burn_per_hour": r,
                    "projected_exhaustion": exhaust_at,
                    "exhausts_before_reset": bool(soonest_reset and exhaust_at < soonest_reset),
                }
            )
    return {
        "method": "linear extrapolation of the project's last-hour usage applied to each allocation",
        "assumptions": [
            "traffic continues at the last hour's rate",
            "project-wide rate applied to each allocation (conservative upper bound)",
            "provider limits unchanged",
        ],
        "confidence": confidence,
        "sample_requests": samples,
        "items": items,
    }
