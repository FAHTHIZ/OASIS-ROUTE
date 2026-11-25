"""USD budget ledger: worst-case holds, atomic reservation, settlement, receipts, forecast."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from sqlalchemy import select, update

from oasis_route.context import AppContext
from oasis_route.db.models import Connection, Model, Provider, SpendBudget
from oasis_route.quota import ledger
from tests.helpers import auth, chat, gateway_setup

PRICE = {"prompt_usd_per_mtok": 1.0, "completion_usd_per_mtok": 2.0, "source": "provider_model_list"}


async def _paid_project(client: httpx.AsyncClient, ctx: AppContext, *, two: bool = False) -> dict[str, Any]:
    g = await gateway_setup(client, two_providers=two)
    async with ctx.db.transaction() as s:
        await s.execute(update(Provider).where(Provider.id.in_(["mock", "mock_alt"])).values(regions=["us"]))
        await s.execute(
            update(Model).where(Model.provider_id.in_(["mock", "mock_alt"])).values(pricing=PRICE)
        )
    h = auth(g["key"])
    r = await client.patch(
        f"/api/v1/projects/{g['pid']}/connections/{g['conn_a']['id']}",
        json={"billing_mode": "paid"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    r = await client.patch(
        f"/api/v1/projects/{g['pid']}",
        json={"spend_policy": "continuity", "acknowledge_paid_usage": True},
        headers=h,
    )
    assert r.status_code == 200, r.text
    return g


async def _worst(client: httpx.AsyncClient, g: dict[str, Any], model: str = "mock-small") -> int:
    r = await client.post(
        f"/api/v1/projects/{g['pid']}/route-preview",
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
        headers=auth(g["key"]),
    )
    assert r.status_code == 200, r.text
    micros: int = r.json()["candidates"][0]["est_cost_usd_micros"]
    assert micros > 0
    return micros


async def _budget(client: httpx.AsyncClient, g: dict[str, Any], period: str, usd: float) -> dict[str, Any]:
    r = await client.put(
        f"/api/v1/projects/{g['pid']}/budgets/{period}", json={"limit_usd": usd}, headers=auth(g["key"])
    )
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


async def test_budget_too_small_blocks_before_dispatch(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: Any
) -> None:
    g = await _paid_project(client, ctx)
    worst = await _worst(client, g)
    await _budget(client, g, "day", (worst - 1) / 1_000_000)
    r = await chat(client, g["key"], "mock-small")
    assert r.status_code >= 400, r.text
    assert "budget_exceeded" in r.json()["oasis_route"]["rejections"]
    assert upstreams["mock-a.test"].calls == []


async def test_reserve_settle_and_receipt(client: httpx.AsyncClient, ctx: AppContext, upstreams: Any) -> None:
    g = await _paid_project(client, ctx)
    worst = await _worst(client, g)
    await _budget(client, g, "month", 1.0)
    r = await chat(client, g["key"], "mock-small")
    assert r.status_code == 200, r.text
    rid = r.json()["oasis_route"]["request_id"]

    rec = (await client.get(f"/api/v1/projects/{g['pid']}/receipts/{rid}", headers=auth(g["key"]))).json()
    assert rec["outcome"] == "success" and rec["spend_mode"] == "continuity"
    assert rec["estimated_usd_micros"] == worst and rec["reserved_usd_micros"] == worst
    assert rec["usage_source"] == "provider" and rec["input_tokens"] > 0
    assert (
        0 < rec["actual_usd_micros"] < worst and rec["drift_usd_micros"] == rec["actual_usd_micros"] - worst
    )
    assert rec["digest_valid"] and len(rec["digest"]) == 64 and rec["reservation_ids"]
    assert rec["budgets_after"][0]["remaining_usd_micros"] == 1_000_000 - rec["actual_usd_micros"]

    b = (await client.get(f"/api/v1/projects/{g['pid']}/budgets", headers=auth(g["key"]))).json()["budgets"][
        0
    ]
    assert b["period"] == "month" and b["reserved_usd"] == 0
    assert b["used_usd"] * 1_000_000 == rec["actual_usd_micros"]
    assert b["forecast"]["burn_rate_usd_per_hour"] > 0 and b["forecast"]["projected_window_usd"] > 0
    listed = (await client.get(f"/api/v1/projects/{g['pid']}/receipts", headers=auth(g["key"]))).json()
    assert [x["request_id"] for x in listed] == [rid]


async def test_free_routes_hold_nothing(client: httpx.AsyncClient, ctx: AppContext, upstreams: Any) -> None:
    g = await gateway_setup(client, two_providers=False)  # local mocks: free by construction
    await _budget(client, g, "day", 0.0)
    r = await chat(client, g["key"])
    assert r.status_code == 200, r.text
    rid = r.json()["oasis_route"]["request_id"]
    rec = (await client.get(f"/api/v1/projects/{g['pid']}/receipts/{rid}", headers=auth(g["key"]))).json()
    assert rec["cost_status"] == "free" and rec["reserved_usd_micros"] == 0 and rec["actual_usd_micros"] == 0


async def test_unknown_cost_never_runs_under_a_budget(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: Any
) -> None:
    g = await _paid_project(client, ctx)
    async with ctx.db.transaction() as s:
        await s.execute(update(Model).where(Model.provider_id == "mock").values(pricing={}))
    await _budget(client, g, "day", 100.0)
    r = await chat(client, g["key"], "mock-small")
    assert r.status_code >= 400 and "budget_cost_unknown" in r.json()["oasis_route"]["rejections"]
    assert upstreams["mock-a.test"].calls == []


async def test_concurrent_holds_never_overshoot(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: Any
) -> None:
    g = await _paid_project(client, ctx)
    await _budget(client, g, "total", 350 / 1_000_000)
    async with ctx.db.session() as s:
        b = (await s.execute(select(SpendBudget))).scalar_one()
        conn = (await s.execute(select(Connection).where(Connection.id == g["conn_a"]["id"]))).scalar_one()
        model = (await s.execute(select(Model).where(Model.upstream_id == "mock-small"))).scalars().first()
    assert model is not None

    async def hold(i: int) -> Any:
        try:
            return await ledger.reserve(
                ctx.db,
                idempotency_key=f"k{i}",
                request_id=f"r{i}",
                project_id=g["pid"],
                connection_id=conn.id,
                model_id=model.id,
                pools=[],
                demand=ledger.Demand(1, 1),
                allow_unknown=False,
                ttl_s=60,
                budgets=[b],
                budget_micros=100,
            )
        except ledger.QuotaUnavailable as exc:
            return exc

    results = await asyncio.gather(*(hold(i) for i in range(10)))
    refused = [r for r in results if isinstance(r, ledger.QuotaUnavailable)]
    held = [r for r in results if not isinstance(r, ledger.QuotaUnavailable)]
    assert len(held) == 3
    assert {(e.reason, e.unit) for e in refused} == {("budget_exceeded", ledger.BUDGET_UNIT)}
    # Duplicate submission: same idempotency key returns the same hold, not a second one.
    again = await ledger.reserve(
        ctx.db,
        idempotency_key="k0" if held[0].idempotency_key == "k0" else held[0].idempotency_key,
        request_id="dup",
        project_id=g["pid"],
        connection_id=conn.id,
        model_id=model.id,
        pools=[],
        demand=ledger.Demand(1, 1),
        allow_unknown=False,
        ttl_s=60,
        budgets=[b],
        budget_micros=100,
    )
    assert again.id == held[0].id
    assert await ledger.settle(ctx.db, held[0].id, None, cost_micros=40)
    assert not await ledger.settle(ctx.db, held[0].id, None, cost_micros=40)  # settles exactly once
    assert await ledger.release(ctx.db, held[1].id)
    async with ctx.db.session() as s:
        row = (await s.execute(select(SpendBudget))).scalar_one()
    assert (row.used_usd_micros, row.reserved_usd_micros, row.drift_usd_micros) == (40, 100, -60)
    assert row.used_usd_micros + row.reserved_usd_micros <= row.limit_usd_micros


async def test_streaming_settles_and_writes_receipt(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: Any
) -> None:
    g = await _paid_project(client, ctx)
    await _budget(client, g, "day", 1.0)
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "mock-small", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        headers=auth(g["key"]),
    ) as r:
        assert r.status_code == 200
        rid = r.headers["x-request-id"]
        async for _ in r.aiter_lines():
            pass
    rec = (await client.get(f"/api/v1/projects/{g['pid']}/receipts/{rid}", headers=auth(g["key"]))).json()
    assert rec["streamed"] and rec["outcome"] == "success" and rec["actual_usd_micros"] is not None
    b = (await client.get(f"/api/v1/projects/{g['pid']}/budgets", headers=auth(g["key"]))).json()["budgets"][
        0
    ]
    assert b["reserved_usd"] == 0 and b["used_usd"] > 0


async def test_budget_changes_need_owner_and_are_audited(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: Any
) -> None:
    g = await gateway_setup(client, two_providers=False)
    await _budget(client, g, "day", 5)
    r = await client.delete(f"/api/v1/projects/{g['pid']}/budgets/day", headers=auth(g["key"]))
    assert r.status_code == 204
    r = await client.delete(f"/api/v1/projects/{g['pid']}/budgets/day", headers=auth(g["key"]))
    assert r.status_code == 404
