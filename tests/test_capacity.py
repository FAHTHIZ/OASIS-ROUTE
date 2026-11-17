"""Capacity analytics: theoretical vs authorized vs usable vs consumed, and ledger reconciliation."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest

from oasis_route.capacity.service import binding_per_day, per_day
from oasis_route.context import AppContext
from oasis_route.db.models import Connection, HealthRecord, QuotaPool, utcnow
from oasis_route.domain.enums import HealthState
from tests.helpers import auth, chat, gateway_setup

pytestmark = pytest.mark.usefixtures("upstreams")


async def _pool(client: httpx.AsyncClient, g: dict[str, Any], **kw: Any) -> str:
    body = {"provider_id": "mock", "unit": "requests", "period": "day", "limit_value": 100, **kw}
    r = await client.post(f"/api/v1/projects/{g['pid']}/quota-pools", json=body, headers=auth(g["key"]))
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _cap(client: httpx.AsyncClient, g: dict[str, Any]) -> dict[str, Any]:
    r = await client.get(f"/api/v1/projects/{g['pid']}/capacity", headers=auth(g["key"]))
    assert r.status_code == 200, r.text
    data: dict[str, Any] = r.json()
    return data


def test_per_day_conversion_and_binding_limit() -> None:
    assert per_day(30, "minute") == 30 * 1440
    assert per_day(5, "one_time") is None and per_day(5, "none") is None
    # 30 RPM is 43,200/day but the 1,000 RPD cap binds: never add the two together.
    assert binding_per_day([(30, "minute"), (1000, "day")]) == 1000
    assert binding_per_day([]) is None


async def test_rpm_plus_rpd_reports_binding_daily_capacity(client: httpx.AsyncClient) -> None:
    g = await gateway_setup(client, two_providers=False)
    await _pool(client, g, period="minute", limit_value=30)
    await _pool(client, g, period="day", limit_value=1000)
    cap = await _cap(client, g)
    assert cap["authorized"]["per_day"]["requests"] == 1000
    assert cap["usable"]["per_day"]["requests"] == 1000
    assert cap["usable"]["headroom_now"]["requests"] == 30  # the minute window binds right now
    alloc = cap["allocations"][0]
    assert alloc["authorized"] and alloc["routable_now"]
    assert [p["period"] for p in alloc["dimensions"][0]["pools"]] == ["minute", "day"]


async def test_shared_account_pool_counted_once(client: httpx.AsyncClient, ctx: AppContext) -> None:
    g = await gateway_setup(client, two_providers=False)
    await _pool(client, g, limit_value=500)
    # A second connection to the *same* upstream account shares the pool: capacity must not double.
    async with ctx.db.transaction() as s:
        src = await s.get(Connection, g["conn_a"]["id"])
        assert src is not None
        s.add(
            Connection(
                project_id=src.project_id,
                provider_id=src.provider_id,
                upstream_account_ref=src.upstream_account_ref,
                status="active",
                credential_id=src.credential_id,
            )
        )
    cap = await _cap(client, g)
    assert cap["authorized"]["per_day"]["requests"] == 500
    assert len(cap["allocations"]) == 1 and len(cap["allocations"][0]["connections"]) == 2


async def test_separate_accounts_add_up(client: httpx.AsyncClient) -> None:
    g = await gateway_setup(client, two_providers=False)
    r = await client.post(
        f"/api/v1/projects/{g['pid']}/connections",
        json={
            "provider_id": "mock",
            "api_key": "mock-key",
            "base_url": "http://mock-a.test/v1",
            "accept_terms": True,
            "upstream_account_ref": "second",
        },
        headers=auth(g["key"]),
    )
    assert r.status_code == 201, r.text
    await _pool(client, g, limit_value=500)
    await _pool(client, g, limit_value=200, account_scope="second")
    cap = await _cap(client, g)
    assert cap["authorized"]["per_day"]["requests"] == 700


async def test_one_time_credits_reported_separately(client: httpx.AsyncClient) -> None:
    g = await gateway_setup(client, two_providers=False)
    await _pool(client, g, limit_value=100)
    await _pool(client, g, unit="credits_usd_micros", period="one_time", limit_value=5_000_000)
    cap = await _cap(client, g)
    assert cap["authorized"]["per_day"] == {"requests": 100}
    assert cap["authorized"]["one_time_remaining"] == {"credits_usd_micros": 5_000_000}


async def test_unknown_limits_excluded_and_unmetered_listed(client: httpx.AsyncClient) -> None:
    g = await gateway_setup(client)  # mock_alt has no pools at all
    await _pool(client, g, limit_value=None)
    cap = await _cap(client, g)
    assert cap["authorized"]["per_day"] == {}
    assert cap["unknown_limits"] == [{"provider_id": "mock", "account_scope": "default", "unit": "requests"}]
    assert [u["provider_id"] for u in cap["unmetered_connections"]] == ["mock_alt"]


async def test_suspended_connection_not_authorized(client: httpx.AsyncClient, ctx: AppContext) -> None:
    g = await gateway_setup(client, two_providers=False)
    await _pool(client, g, limit_value=100)
    async with ctx.db.transaction() as s:
        c = await s.get(Connection, g["conn_a"]["id"])
        assert c is not None
        c.status = "suspended"
    cap = await _cap(client, g)
    assert cap["authorized"]["per_day"] == {}
    assert cap["allocations"][0]["authorized"] is False


async def test_open_breaker_is_authorized_but_not_usable(client: httpx.AsyncClient, ctx: AppContext) -> None:
    g = await gateway_setup(client, two_providers=False)
    await _pool(client, g, limit_value=100)
    async with ctx.db.transaction() as s:
        s.add(
            HealthRecord(
                connection_id=g["conn_a"]["id"],
                model_scope="*",
                state=HealthState.OPEN.value,
                open_until=utcnow() + timedelta(minutes=5),
            )
        )
    cap = await _cap(client, g)
    assert cap["authorized"]["per_day"]["requests"] == 100
    assert cap["usable"]["per_day"] == {} and cap["usable"]["headroom_now"] == {}
    assert cap["allocations"][0]["routable_now"] is False


async def test_exhausted_pool_has_no_usable_headroom(client: httpx.AsyncClient, ctx: AppContext) -> None:
    g = await gateway_setup(client, two_providers=False)
    pid = await _pool(client, g, limit_value=100)
    async with ctx.db.transaction() as s:
        p = await s.get(QuotaPool, pid)
        assert p is not None
        p.exhausted_until = utcnow() + timedelta(hours=1)
        p.needs_recheck = True
    cap = await _cap(client, g)
    assert cap["usable"]["headroom_now"]["requests"] == 0
    assert cap["usable"]["per_day"]["requests"] == 0
    pool = cap["allocations"][0]["dimensions"][0]["pools"][0]
    assert pool["exhausted_until"] is not None and pool["needs_recheck"] is True


async def test_consumed_confirmed_vs_estimated_and_ledger_reconciles(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client, two_providers=False)
    pid = await _pool(client, g, limit_value=100)
    for _ in range(3):
        assert (await chat(client, g["key"])).status_code == 200
    upstreams["mock-a.test"].omit_usage = True
    for _ in range(2):
        assert (await chat(client, g["key"])).status_code == 200
    cap = await _cap(client, g)
    hour = cap["consumed"]["last_hour"]
    assert hour["confirmed"]["requests"] == 3
    assert hour["estimated"]["requests"] == 2
    async with ctx.db.session() as s:
        p = await s.get(QuotaPool, pid)
        assert p is not None
        # The dashboard and the ledger agree on what was consumed.
        assert p.used == hour["confirmed"]["requests"] + hour["estimated"]["requests"] == 5
    assert cap["usable"]["headroom_now"]["requests"] == 95
    fc = cap["forecast"]
    assert fc["sample_requests"] == 5 and fc["confidence"] == "low" and fc["items"]


async def test_capacity_is_tenant_scoped(client: httpx.AsyncClient) -> None:
    g = await gateway_setup(client, two_providers=False)
    r = await client.get("/api/v1/projects/not-mine/capacity", headers=auth(g["key"]))
    assert r.status_code == 404
    r = await client.get(f"/api/v1/projects/{g['pid']}/capacity")
    assert r.status_code == 401


async def test_usage_and_route_decision_listing(client: httpx.AsyncClient) -> None:
    g = await gateway_setup(client, two_providers=False)
    assert (await chat(client, g["key"])).status_code == 200
    u = await client.get(f"/api/v1/projects/{g['pid']}/usage", headers=auth(g["key"]))
    assert u.status_code == 200 and u.json()[0]["usage_source"] == "provider_reported"
    d = await client.get(f"/api/v1/projects/{g['pid']}/route-decisions", headers=auth(g["key"]))
    assert d.status_code == 200 and d.json()[0]["served_model"] == "mock-large"
