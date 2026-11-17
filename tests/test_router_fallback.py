"""Automatic failover: bounded, visible, and never evading provider decisions."""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import select

from oasis_route.context import AppContext
from oasis_route.db.models import Connection, Notification, QuotaPool, RouteDecision, UsageRecord
from tests.helpers import auth, chat, connect_mock, gateway_setup


async def test_provider_outage_fails_over_same_model(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client)
    upstreams["mock-a.test"].fail_next("mock-large", 503)
    r = await chat(client, g["key"])
    assert r.status_code == 200, r.text
    assert r.headers["x-oasis-route-provider"] == "mock_alt"
    assert r.headers["x-oasis-route-model"] == "mock-large"
    assert r.headers["x-oasis-route-fallback"] == "true"
    assert r.headers["x-oasis-route-downgraded"] == "false"
    meta = r.json()["oasis_route"]
    assert [a["outcome"] for a in meta["attempts"]] == ["error", "success"]
    assert meta["attempts"][0]["category"] == "PROVIDER_UNAVAILABLE"
    async with ctx.db.session() as s:
        d = (await s.execute(select(RouteDecision))).scalar_one()
        assert d.fallback_used and d.served_provider == "mock_alt" and d.outcome == "success"
        usages = (await s.execute(select(UsageRecord).order_by(UsageRecord.attempt))).scalars().all()
        assert [u.outcome for u in usages] == ["error", "success"]
        assert usages[1].provider_reported


async def test_downgrade_is_flagged(client: httpx.AsyncClient, upstreams: dict[str, Any]) -> None:
    g = await gateway_setup(client)
    # Model-scoped failures on both providers leave the next-strongest model.
    upstreams["mock-a.test"].fail_next("mock-large", 404, code="model_not_found")
    upstreams["mock-b.test"].fail_next("mock-large", 404, code="model_not_found")
    r = await chat(client, g["key"])
    assert r.status_code == 200, r.text
    assert r.headers["x-oasis-route-model"] == "mock-medium"
    assert r.headers["x-oasis-route-downgraded"] == "true"
    assert r.json()["oasis_route"]["downgraded"] is True


async def test_policy_rejection_stops(client: httpx.AsyncClient, upstreams: dict[str, Any]) -> None:
    g = await gateway_setup(client)
    upstreams["mock-a.test"].fail_next("mock-large", 400, code="content_policy_violation", message="blocked")
    r = await chat(client, g["key"])
    assert r.status_code == 400, r.text
    assert r.json()["error"]["type"] == "provider_policy_error"
    assert len(upstreams["mock-b.test"].calls) == 0  # never retried elsewhere


async def test_auth_failure_suspends_and_notifies(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client)
    upstreams["mock-a.test"].fail_next("mock-large", 401, code="invalid_api_key")
    r = await chat(client, g["key"])
    assert r.status_code == 200 and r.headers["x-oasis-route-provider"] == "mock_alt"
    async with ctx.db.session() as s:
        c = await s.get(Connection, g["conn_a"]["id"])
        assert c is not None and c.status == "suspended"
        n = (await s.execute(select(Notification))).scalar_one()
        assert n.kind == "connection.suspended" and n.project_id == g["pid"]
    r = await chat(client, g["key"])
    assert r.headers["x-oasis-route-fallback"] == "false"  # suspended connection no longer a candidate


async def test_quota_exhausted_marks_pool(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client)
    r = await client.post(
        f"/api/v1/projects/{g['pid']}/quota-pools",
        json={"provider_id": "mock", "unit": "requests", "period": "day", "limit_value": 100},
        headers=auth(g["key"]),
    )
    pool_id = r.json()["id"]
    upstreams["mock-a.test"].fail_next("mock-large", 429, code="insufficient_quota", message="quota exceeded")
    r = await chat(client, g["key"])
    assert r.status_code == 200 and r.headers["x-oasis-route-provider"] == "mock_alt"
    async with ctx.db.session() as s:
        p = await s.get(QuotaPool, pool_id)
        assert p is not None and p.exhausted_until is not None and p.needs_recheck
        assert p.used == 1 and p.reserved == 0  # upstream saw the request
    plan = (
        await client.post(f"/api/v1/projects/{g['pid']}/route-preview", json={}, headers=auth(g["key"]))
    ).json()
    assert {c["provider_id"] for c in plan["candidates"]} == {"mock_alt"}


async def test_rate_limit_does_not_hop_accounts(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client, two_providers=False)
    await connect_mock(
        client, g["key"], g["pid"], provider="mock", host="mock-b.test", upstream_account_ref="acct2"
    )
    # Operator records that this provider does NOT permit multiple accounts.
    await client.patch(
        "/api/v1/admin/providers/mock", json={"multi_account_permitted": False}, headers=auth(g["admin"])
    )
    upstreams["mock-a.test"].fail_next("mock-large", 429, retry_after=30)
    upstreams["mock-b.test"].fail_next("mock-large", 429, retry_after=30)
    r = await chat(client, g["key"], model="mock-large")
    assert r.status_code == 429, r.text
    assert r.json()["error"]["code"] == "capacity_exhausted"
    attempts = r.json()["oasis_route"]["attempts"]
    assert len([a for a in attempts if a["outcome"] == "error"]) == 1  # second account never tried
    assert len(upstreams["mock-b.test"].calls) == 0


async def test_rate_limit_falls_back_to_other_model_on_same_account(
    client: httpx.AsyncClient, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client, two_providers=False)
    await connect_mock(
        client, g["key"], g["pid"], provider="mock", host="mock-b.test", upstream_account_ref="acct2"
    )
    await client.patch(
        "/api/v1/admin/providers/mock", json={"multi_account_permitted": False}, headers=auth(g["admin"])
    )
    upstreams["mock-a.test"].fail_next("mock-large", 429, retry_after=30)
    upstreams["mock-b.test"].fail_next("mock-large", 429, retry_after=30)
    r = await chat(client, g["key"])
    assert r.status_code == 200, r.text
    meta = r.json()["oasis_route"]
    # The rate limit was per model: the same account's next model serves, and the second account is untouched.
    assert meta["model"] == "mock-medium" and meta["downgraded"] is True
    assert [(a["model"], a["outcome"]) for a in meta["attempts"]] == [
        ("mock-large", "error"),
        ("mock-medium", "success"),
    ]
    a_calls, b_calls = upstreams["mock-a.test"].calls, upstreams["mock-b.test"].calls
    served_by_a = len(a_calls) == 2
    assert len(a_calls) + len(b_calls) == 2 and (served_by_a or len(b_calls) == 2)  # one account only


async def test_rate_limit_respects_retry_after_cooldown(
    client: httpx.AsyncClient, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client, two_providers=False)
    upstreams["mock-a.test"].fail_next("mock-large", 429, retry_after=60)
    r = await chat(client, g["key"], model="mock-large")
    assert r.status_code == 429
    calls = len(upstreams["mock-a.test"].calls)
    r = await chat(client, g["key"], model="mock-large")
    assert r.status_code == 429  # circuit open: not even attempted
    assert len(upstreams["mock-a.test"].calls) == calls
    assert r.json()["oasis_route"]["rejections"]["health:circuit_open"] == 1


async def test_max_hops_bounded(client: httpx.AsyncClient, upstreams: dict[str, Any]) -> None:
    g = await gateway_setup(client)
    await client.patch(f"/api/v1/projects/{g['pid']}", json={"max_fallback_hops": 1}, headers=auth(g["key"]))
    for host in ("mock-a.test", "mock-b.test"):
        for m in ("mock-large", "mock-medium", "mock-small"):
            upstreams[host].fail_next(m, 500, times=3)
    r = await chat(client, g["key"])
    assert r.status_code == 502
    total = len(upstreams["mock-a.test"].calls) + len(upstreams["mock-b.test"].calls)
    assert total == 2  # first attempt + 1 hop


async def test_no_fallback_when_disabled(client: httpx.AsyncClient, upstreams: dict[str, Any]) -> None:
    g = await gateway_setup(client)
    upstreams["mock-a.test"].fail_next("mock-large", 500)
    r = await chat(client, g["key"], oasis_route={"allow_fallback": False})
    assert r.status_code == 502


async def test_model_unavailable_scoped_to_model(
    client: httpx.AsyncClient, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client, two_providers=False)
    upstreams["mock-a.test"].fail_next("mock-large", 404, code="model_not_found")
    r = await chat(client, g["key"])
    assert r.status_code == 200 and r.headers["x-oasis-route-model"] == "mock-medium"
    plan = (
        await client.post(f"/api/v1/projects/{g['pid']}/route-preview", json={}, headers=auth(g["key"]))
    ).json()
    assert "mock-large" not in [c["upstream_model"] for c in plan["candidates"]]
    assert "mock-medium" in [c["upstream_model"] for c in plan["candidates"]]
