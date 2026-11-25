"""Routing strategies order only gated candidates; every decision is explainable."""

from __future__ import annotations

from typing import Any

import httpx

from oasis_route.domain.enums import RoutingMode
from oasis_route.routing.explain import explain, gate_of
from oasis_route.routing.router import Candidate, RouteOptions, diversify, rank_key, route_cost
from tests.helpers import auth, chat, gateway_setup


def _c(name: str, provider: str = "p", **kw: Any) -> Candidate:
    base: dict[str, Any] = dict(
        connection_id=f"conn_{name}",
        provider_id=provider,
        adapter_key="openai_compatible",
        account_scope="a",
        multi_account_permitted=False,
        model_id=name,
        upstream_model=name,
        quality=50.0,
        quality_source="test",
        reliability=0.9,
        latency_ms=500.0,
        quota_fraction=0.5,
        reset_in_s=3600.0,
        quota_metered=True,
        needs_probe=False,
        priority=0,
        preference_rank=0,
    )
    base.update(kw)
    return Candidate(**base)


def _order(cands: list[Candidate], mode: RoutingMode, opts: RouteOptions | None = None) -> list[str]:
    return [c.upstream_model for c in sorted(cands, key=lambda c: rank_key(c, mode, opts))]


def test_lowest_cost_prefers_free_then_verified_price_then_unknown() -> None:
    cands = [
        _c("unknown", quality=90.0),
        _c("est", cost_status="priced", est_cost_usd_micros=100, price_verified=False),
        _c("verified", cost_status="priced", est_cost_usd_micros=100, price_verified=True),
        _c("free", cost_status="free", est_cost_usd_micros=0, quality=10.0),
    ]
    assert _order(cands, RoutingMode.LOWEST_COST) == ["free", "verified", "est", "unknown"]
    assert _order(cands, RoutingMode.STRICT_QUALITY)[0] == "unknown"


def test_quota_latency_reliability_reset_orderings() -> None:
    a = _c("a", quota_fraction=0.9, latency_ms=900.0, reliability=0.5, reset_in_s=80_000.0)
    b = _c("b", quota_fraction=0.1, latency_ms=100.0, reliability=0.99, reset_in_s=600.0)
    assert _order([b, a], RoutingMode.MOST_QUOTA) == ["a", "b"]
    assert _order([a, b], RoutingMode.LOWEST_LATENCY) == ["b", "a"]
    assert _order([a, b], RoutingMode.RELIABILITY) == ["b", "a"]
    assert _order([a, b], RoutingMode.RESET_AWARE) == ["b", "a"]


def test_context_fit_picks_tightest_window_that_fits() -> None:
    cands = [_c("huge", context_window=1_000_000), _c("fit", context_window=16_000), _c("none")]
    opts = RouteOptions(input_tokens=8_000, max_output_tokens=1_000)
    assert _order(cands, RoutingMode.CONTEXT_FIT, opts) == ["fit", "huge", "none"]


def test_diversity_alternates_providers() -> None:
    ranked = [_c("a1", "A"), _c("a2", "A"), _c("b1", "B"), _c("a3", "A"), _c("c1", "C")]
    assert [c.upstream_model for c in diversify(ranked)] == ["a1", "b1", "c1", "a2", "a3"]


def test_route_cost_never_calls_a_paid_route_free() -> None:
    free_tier = route_cost(
        "free_tier_account", {"prompt_usd_per_mtok": 3.0, "completion_usd_per_mtok": 3.0}, 1000, 1000
    )
    assert free_tier.status == "free" and free_tier.usd_micros == 0
    paid = route_cost("paid", {"prompt_usd_per_mtok": 1.0, "completion_usd_per_mtok": 2.0}, 1_000_000, 0)
    assert paid.status == "priced" and paid.usd_micros == 1_000_000 and paid.verified
    assert route_cost("unknown", {}, 10, 10).status == "unknown"


def test_explanation_groups_rejections_by_gate() -> None:
    plan = {
        "mode": "lowest_cost",
        "candidates": [
            _c("x", cost_basis="local", cost_status="free", est_cost_usd_micros=0).describe(),
            _c("y").describe(),
        ],
        "rejected": [
            {
                "provider_id": "p",
                "upstream_model": "m",
                "connection_id": "c1",
                "reason": "paid_route_blocked",
            },
            {
                "provider_id": "q",
                "upstream_model": "m",
                "connection_id": "c2",
                "reason": "paid_route_blocked",
            },
            {"provider_id": "q", "upstream_model": "n", "connection_id": "c2", "reason": "quota_exhausted"},
        ],
    }
    ex = explain(plan)
    assert ex["selected"]["route"] == "p/x" and "free" in ex["selected"]["reason"]
    assert ex["verification"]["free"] and ex["verification"]["free_evidence"]
    assert [a["route"] for a in ex["alternatives"]] == ["p/y"]
    assert ex["rejected"][0] == {
        "reason": "paid_route_blocked",
        "gate": "budget",
        "count": 2,
        "routes": [
            {"route": "p/m", "connection_id": "c1", "retry_in_s": None},
            {"route": "q/m", "connection_id": "c2", "retry_in_s": None},
        ],
    }
    assert gate_of("quota_exhausted") == "quota" and gate_of("health:open") == "health"
    assert ex["fallback_chain"]["planned"] == ["p/x", "p/y"]


async def test_decision_explanation_is_persisted_and_served(
    client: httpx.AsyncClient, upstreams: Any
) -> None:
    g = await gateway_setup(client)
    h = auth(g["key"])
    prev = await client.post(
        f"/api/v1/projects/{g['pid']}/route-preview", json={"oasis_route": {"mode": "diversity"}}, headers=h
    )
    assert prev.status_code == 200, prev.text
    ex = prev.json()["explanation"]
    assert ex["strategy"] == "diversity" and ex["selected"]["reason"]

    r = await chat(client, g["key"], oasis_route={"mode": "lowest_cost"})
    assert r.status_code == 200, r.text
    meta = r.json()["oasis_route"]
    route = meta["route"]
    assert route["strategy"] == "lowest_cost" and route["reason"]
    assert route["fallback_chain"] and route["explain"].endswith(meta["request_id"])

    d = await client.get(f"/api/v1/projects/{g['pid']}/route-decisions/{meta['request_id']}", headers=h)
    assert d.status_code == 200, d.text
    full = d.json()["explanation"]
    assert full["selected"]["route"] == f"{meta['provider']}/{meta['model']}"
    assert full["actual_cost"] is not None and full["estimated_cost"] is not None
    assert len(full["fallback_chain"]["attempts"]) >= 1

    missing = await client.get(f"/api/v1/projects/{g['pid']}/route-decisions/req_nope", headers=h)
    assert missing.status_code == 404


async def test_project_accepts_new_routing_modes(client: httpx.AsyncClient, upstreams: Any) -> None:
    g = await gateway_setup(client, two_providers=False)
    r = await client.patch(
        f"/api/v1/projects/{g['pid']}", json={"routing_mode": "reset_aware"}, headers=auth(g["key"])
    )
    assert r.status_code == 200, r.text
    assert r.json()["routing_mode"] == "reset_aware"
