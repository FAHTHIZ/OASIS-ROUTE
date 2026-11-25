"""Hard filters and ranking: strongest suitable eligible model first, every rejection explained."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select, update

from oasis_route.context import AppContext
from oasis_route.db.models import Connection, HealthRecord, Model, Offer, utcnow
from tests.helpers import auth, chat, gateway_setup


async def _preview(client: httpx.AsyncClient, g: dict[str, Any], **body: Any) -> dict[str, Any]:
    r = await client.post(f"/api/v1/projects/{g['pid']}/route-preview", json=body, headers=auth(g["key"]))
    assert r.status_code == 200, r.text
    data: dict[str, Any] = r.json()
    return data


def _reasons(plan: dict[str, Any]) -> dict[tuple[str, str], str]:
    return {(r["provider_id"], r["upstream_model"]): r["reason"] for r in plan["rejected"]}


async def test_auto_routes_to_strongest(client: httpx.AsyncClient, upstreams: dict[str, Any]) -> None:
    g = await gateway_setup(client)
    plan = await _preview(client, g)
    order = [c["upstream_model"] for c in plan["candidates"]]
    assert order[:2] == ["mock-large", "mock-large"]
    assert order.index("mock-medium") < order.index("mock-small")
    r = await chat(client, g["key"])
    assert r.status_code == 200, r.text
    assert r.headers["x-oasis-route-model"] == "mock-large"
    assert r.headers["x-oasis-route-downgraded"] == "false"


async def test_data_policy_unknown_never_satisfies(
    client: httpx.AsyncClient, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client)
    r = await client.patch(
        "/api/v1/admin/providers/mock_alt", json={"data_policy": {}}, headers=auth(g["admin"])
    )
    assert r.status_code == 200
    r = await client.patch(
        f"/api/v1/projects/{g['pid']}",
        json={"data_policy": {"require_no_training": True}},
        headers=auth(g["key"]),
    )
    assert r.status_code == 200, r.text
    plan = await _preview(client, g)
    assert {c["provider_id"] for c in plan["candidates"]} == {"mock"}
    assert _reasons(plan)[("mock_alt", "mock-large")] == "data_policy:training_policy_unknown_or_trains"


async def test_provider_denylist_and_region(client: httpx.AsyncClient, upstreams: dict[str, Any]) -> None:
    g = await gateway_setup(client)
    await client.patch(
        f"/api/v1/projects/{g['pid']}",
        json={"data_policy": {"provider_denylist": ["mock"]}},
        headers=auth(g["key"]),
    )
    plan = await _preview(client, g)
    assert {c["provider_id"] for c in plan["candidates"]} == {"mock_alt"}
    await client.patch(
        f"/api/v1/projects/{g['pid']}",
        json={"data_policy": {"allowed_regions": ["eu"]}},
        headers=auth(g["key"]),
    )
    plan = await _preview(client, g)
    assert plan["candidates"] == []
    assert set(_reasons(plan).values()) == {"data_policy:region_not_allowed"}
    r = await chat(client, g["key"])
    assert r.status_code == 503 and r.json()["error"]["code"] == "no_eligible_route"
    assert r.json()["oasis_route"]["rejections"]["data_policy:region_not_allowed"] == 6


async def test_capabilities_and_context_window(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client, two_providers=False)
    async with ctx.db.transaction() as s:
        m = (await s.execute(select(Model).where(Model.upstream_id == "mock-medium"))).scalars().first()
        assert m is not None
        m.capabilities = {**m.capabilities, "tools": False}
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
    plan = await _preview(client, g, tools=tools)
    assert _reasons(plan)[("mock", "mock-medium")] == "missing_capability:tools"
    plan = await _preview(client, g, max_tokens=5_000)
    reasons = _reasons(plan)
    assert reasons[("mock", "mock-small")] == "max_output_tokens_exceeded"
    plan = await _preview(client, g, messages=[{"role": "user", "content": "x" * 120_000}], max_tokens=100)
    assert _reasons(plan)[("mock", "mock-small")] == "context_window_exceeded"
    assert [c["upstream_model"] for c in plan["candidates"]] == ["mock-large"]


async def test_min_quality_pinned_and_explicit(client: httpx.AsyncClient, upstreams: dict[str, Any]) -> None:
    g = await gateway_setup(client)
    plan = await _preview(client, g, oasis_route={"min_quality": 80})
    assert {c["upstream_model"] for c in plan["candidates"]} == {"mock-large"}
    plan = await _preview(client, g, model="mock_alt/mock-medium")
    assert [(c["provider_id"], c["upstream_model"]) for c in plan["candidates"]] == [
        ("mock_alt", "mock-medium")
    ]
    plan = await _preview(client, g, model="mock-medium")
    assert {c["provider_id"] for c in plan["candidates"]} == {"mock", "mock_alt"}
    assert {c["upstream_model"] for c in plan["candidates"]} == {"mock-medium"}
    r = await chat(client, g["key"], model="nonexistent-model")
    assert r.status_code == 404 and r.json()["error"]["code"] == "model_not_found"


async def test_no_downgrade_policy(client: httpx.AsyncClient, upstreams: dict[str, Any]) -> None:
    g = await gateway_setup(client)
    await client.patch(
        f"/api/v1/projects/{g['pid']}", json={"allow_downgrade": False}, headers=auth(g["key"])
    )
    plan = await _preview(client, g)
    assert {c["upstream_model"] for c in plan["candidates"]} == {"mock-large"}
    assert "downgrade_not_allowed" in set(_reasons(plan).values())


async def test_unrated_models_rank_last(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client, two_providers=False)
    async with ctx.db.transaction() as s:
        s.add(
            Model(
                provider_id="mock",
                upstream_id="mock-mystery",
                capabilities={"streaming": True},
                quality={},
                aliases=[],
            )
        )
    plan = await _preview(client, g)
    assert plan["candidates"][-1]["upstream_model"] == "mock-mystery"
    assert plan["candidates"][-1]["quality"] is None


async def test_balanced_trades_small_quality_for_reliability(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client)

    def q(score: float) -> dict[str, Any]:
        return {
            "general": {
                "score": score,
                "source": "t",
                "as_of": "2026-01-01",
                "methodology": "t",
                "confidence": 1,
            }
        }

    async with ctx.db.transaction() as s:
        await s.execute(
            update(Model)
            .where(Model.provider_id == "mock", Model.upstream_id == "mock-large")
            .values(quality=q(88))
        )
        await s.execute(
            update(Model)
            .where(Model.provider_id == "mock_alt", Model.upstream_id == "mock-large")
            .values(quality=q(82))
        )
        s.add(
            HealthRecord(
                connection_id=g["conn_a"]["id"],
                model_scope="*",
                state="degraded",
                success_count=10,
                failure_count=10,
                consecutive_failures=1,
            )
        )
        s.add(
            HealthRecord(
                connection_id=g["conn_b"]["id"],
                model_scope="*",
                state="healthy",
                success_count=100,
                failure_count=0,
                consecutive_failures=0,
            )
        )
    strict = await _preview(client, g)
    assert strict["candidates"][0]["provider_id"] == "mock"
    balanced = await _preview(client, g, oasis_route={"mode": "balanced"})
    assert balanced["candidates"][0]["provider_id"] == "mock_alt"


async def test_kill_switch_and_offer_expiry(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client)
    async with ctx.db.transaction() as s:
        o = Offer(
            provider_id="mock_alt",
            title="free",
            dedupe_key="mock_alt:free",
            state="active",
            expires_at=utcnow() - timedelta(days=1),
        )
        s.add(o)
        await s.flush()
        await s.execute(update(Connection).where(Connection.id == g["conn_b"]["id"]).values(offer_id=o.id))
    plan = await _preview(client, g)
    assert {c["provider_id"] for c in plan["candidates"]} == {"mock"}
    assert _reasons(plan)[("mock_alt", "mock-large")].startswith("offer_not_usable")
    await client.patch(f"/api/v1/projects/{g['pid']}", json={"kill_switch": True}, headers=auth(g["key"]))
    r = await chat(client, g["key"])
    assert r.status_code == 503 and r.json()["error"]["code"] == "project_disabled"


async def test_eligibility_rechecked_at_route_time(
    client: httpx.AsyncClient, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client)
    await client.patch("/api/v1/admin/providers/mock_alt", json={"min_age": 40}, headers=auth(g["admin"]))
    plan = await _preview(client, g)
    assert _reasons(plan)[("mock_alt", "mock-large")].startswith("ineligible:under_min_age")


async def test_no_route_for_a_capability_says_which_model_and_how(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    """auto with tools and no tool-capable model: the error names the model and the way through."""
    g = await gateway_setup(client, two_providers=False)
    async with ctx.db.transaction() as s:
        for m in (await s.execute(select(Model))).scalars():
            m.capabilities = {**m.capabilities, "tools": None}
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
    r = await chat(client, g["key"], tools=tools)
    assert r.status_code == 503, r.text
    message = r.json()["error"]["message"]
    assert "not known to support tools" in message and 'request it by name (model "mock/' in message
    # Named explicitly, a model whose tool support is unknown is tried.
    named = await chat(client, g["key"], model="mock/mock-large", tools=tools)
    assert named.status_code == 200, named.text


def test_custom_endpoint_capabilities_start_unknown() -> None:
    """A custom server's listing says nothing about tools: unknown, not a stated "no"."""
    from oasis_route.adapters.registry import PROFILES

    caps = PROFILES["custom"].default_capabilities
    assert "tools" not in caps and caps.get("streaming") is True


async def test_model_sync_drops_an_old_default_guess(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    """Rows saved with an earlier default ("tools": false) follow today's defaults on the next sync."""
    from oasis_route.adapters.base import ModelInfo
    from oasis_route.connections.service import upsert_models

    await gateway_setup(client, two_providers=False)
    async with ctx.db.transaction() as s:
        m = (
            (
                await s.execute(
                    select(Model).where(Model.provider_id == "mock", Model.upstream_id == "mock-medium")
                )
            )
            .scalars()
            .one()
        )
        m.capabilities = {"streaming": True, "tools": False, "source": "adapter_default"}
        admin = (
            (
                await s.execute(
                    select(Model).where(Model.provider_id == "mock", Model.upstream_id == "mock-small")
                )
            )
            .scalars()
            .one()
        )
        admin.capabilities = {"streaming": True, "tools": False, "source": "admin"}
    async with ctx.db.transaction() as s:
        infos = [ModelInfo("mock-medium"), ModelInfo("mock-small")]
        await upsert_models(s, "mock", infos, {"streaming": True})
    async with ctx.db.session() as s:
        medium = (
            (
                await s.execute(
                    select(Model).where(Model.provider_id == "mock", Model.upstream_id == "mock-medium")
                )
            )
            .scalars()
            .one()
        )
        small = (
            (
                await s.execute(
                    select(Model).where(Model.provider_id == "mock", Model.upstream_id == "mock-small")
                )
            )
            .scalars()
            .one()
        )
        assert medium.capabilities == {"streaming": True, "source": "adapter_default"}
        assert small.capabilities["tools"] is False  # an admin's edit is kept
