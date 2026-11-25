"""Route explanations: why this route, what else was considered, what was rejected and why.

Every route preview and every persisted route decision carries one. Fields:

- ``selected``: the route that served (or would serve) the request, with ``reason``;
- ``strategy``: the ranking strategy (``RoutingMode``) applied after the policy gates;
- ``alternatives``: other eligible candidates in fallback order, with the factors that ranked them;
- ``rejected``: routes removed by a gate, grouped by reason (authentication/authorization, verification,
  eligibility, budget, quota, health, capability, privacy);
- ``verification``: how the selected route is known to be free (or not) and whether its price is verified;
- ``quota``: remaining fraction and time to reset for the selected route, when known;
- ``estimated_cost`` / ``actual_cost``: worst-case estimate before dispatch and the settled cost after;
- ``fallback_chain``: the planned order and the attempts actually made.
"""

from __future__ import annotations

from typing import Any

from oasis_route.routing.cost import CostEstimate

FREE_EVIDENCE = {
    "local": "runs on a local server",
    "free_model": "priced at zero in the provider's own model list or by an operator",
    "free_tier_account": "account declared free-tier (no billing) by its owner",
}

GATES = {
    "connection_": "authentication",
    "provider_": "authorization",
    "offer_not_usable": "verification",
    "ineligible": "eligibility",
    "not_in_requested_providers": "authorization",
    "demo_provider_superseded": "authorization",
    "data_policy": "privacy",
    "paid_route_blocked": "budget",
    "cost_unknown": "budget",
    "approval_required": "budget",
    "budget_": "budget",
    "quota": "quota",
    "health": "health",
    "excluded_after_failure": "health",
    "missing_capability": "capability",
    "context_window_exceeded": "capability",
    "max_output_tokens_exceeded": "capability",
    "not_a_chat_model": "capability",
    "model_removed": "capability",
    "catalog_model_unconfirmed": "verification",
    "below_min_quality": "capability",
    "downgrade_not_allowed": "capability",
    "model_not_found": "capability",
}


def gate_of(reason: str) -> str:
    for prefix, gate in GATES.items():
        if reason.startswith(prefix):
            return gate
    return "other"


def _route(c: Any) -> str:
    return f"{c['provider_id']}/{c['upstream_model']}"


def _cost(c: dict[str, Any]) -> dict[str, Any]:
    micros = c.get("est_cost_usd_micros")
    return {
        "status": c.get("cost_status", "unknown"),
        "usd": None if micros is None else micros / 1_000_000,
        "usd_micros": micros,
        "price_verified": bool(c.get("price_verified")),
        "cost_basis": c.get("cost_basis"),
    }


def _factors(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "quality": c.get("quality"),
        "quality_source": c.get("quality_source"),
        "prior": c.get("prior"),
        "reliability": c.get("reliability"),
        "latency_ms": c.get("latency_ms"),
        "quota_fraction": c.get("quota_fraction"),
        "reset_in_s": c.get("reset_in_s"),
        "context_window": c.get("context_window"),
        "priority": c.get("priority"),
        "cost": _cost(c),
    }


def _reason(strategy: str, c: dict[str, Any], pinned: bool) -> str:
    if pinned:
        lead = "the requested model"
    else:
        lead = {
            "strict_quality": "highest quality evidence"
            if c.get("quality") is not None
            else "best unrated candidate (name-based prior)",
            "balanced": "best quality tier with the healthiest headroom",
            "lowest_cost": "lowest cost"
            + (
                " (free)"
                if c.get("cost_status") == "free"
                else " (verified price)"
                if c.get("price_verified")
                else ""
            ),
            "most_quota": "most remaining quota",
            "lowest_latency": "lowest observed latency",
            "reliability": "highest observed reliability",
            "context_fit": "smallest context window that fits the request",
            "reset_aware": "quota that resets soonest",
            "diversity": "top of a provider-diverse fallback chain",
        }.get(strategy, strategy)
    basis = c.get("cost_basis")
    free = FREE_EVIDENCE.get(str(basis))
    tail = f"; free because it {free}" if free else f"; cost basis {basis}"
    return f"{lead}{tail}"


def explain(
    plan_desc: dict[str, Any],
    *,
    pinned: bool = False,
    attempts: list[dict[str, Any]] | None = None,
    served: dict[str, Any] | None = None,
    actual_cost: CostEstimate | None = None,
    spend: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cands: list[dict[str, Any]] = list(plan_desc.get("candidates") or [])
    strategy = str(plan_desc.get("mode") or "strict_quality")
    selected = served or (cands[0] if cands else None)
    rejected: dict[str, dict[str, Any]] = {}
    for r in plan_desc.get("rejected") or []:
        g = rejected.setdefault(
            r["reason"], {"reason": r["reason"], "gate": gate_of(r["reason"]), "count": 0, "routes": []}
        )
        g["count"] += 1
        if len(g["routes"]) < 10:
            g["routes"].append(
                {
                    "route": _route(r),
                    "connection_id": r.get("connection_id"),
                    "retry_in_s": r.get("retry_in_s"),
                }
            )
    out: dict[str, Any] = {
        "strategy": strategy,
        "free_only": bool(plan_desc.get("free_only")),
        "selected": None,
        "alternatives": [],
        "rejected": sorted(rejected.values(), key=lambda g: (-g["count"], g["reason"])),
        "fallback_chain": {
            "planned": [_route(c) for c in cands][:20],
            "attempts": attempts or [],
        },
    }
    if spend:
        out["spend"] = spend
    if selected is None:
        out["reason"] = "no route passed every gate" if not cands else None
        return out
    out["selected"] = {
        "route": _route(selected),
        "connection_id": selected.get("connection_id"),
        "reason": _reason(strategy, selected, pinned),
        **_factors(selected),
    }
    out["verification"] = {
        "cost_basis": selected.get("cost_basis"),
        "free": selected.get("cost_status") == "free",
        "free_evidence": FREE_EVIDENCE.get(str(selected.get("cost_basis"))),
        "price_verified": bool(selected.get("price_verified")),
    }
    out["quota"] = {
        "metered": selected.get("quota_metered"),
        "remaining_fraction": selected.get("quota_fraction"),
        "reset_in_s": selected.get("reset_in_s"),
    }
    out["estimated_cost"] = _cost(selected)
    out["actual_cost"] = actual_cost.as_dict() if actual_cost is not None else None
    sel_key = (selected.get("connection_id"), selected.get("upstream_model"))
    out["alternatives"] = [
        {"route": _route(c), "connection_id": c.get("connection_id"), **_factors(c)}
        for c in cands
        if (c.get("connection_id"), c.get("upstream_model")) != sel_key
    ][:10]
    return out
