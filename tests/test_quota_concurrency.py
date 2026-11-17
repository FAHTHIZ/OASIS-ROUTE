"""Atomic quota: concurrent gateway requests never overspend a pool."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from sqlalchemy import select

from oasis_route.context import AppContext
from oasis_route.db.models import QuotaPool, Reservation
from tests.helpers import auth, chat, gateway_setup


async def test_concurrent_requests_respect_pool_limit(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client, two_providers=False)
    r = await client.post(
        f"/api/v1/projects/{g['pid']}/quota-pools",
        json={"provider_id": "mock", "unit": "requests", "period": "day", "limit_value": 5},
        headers=auth(g["key"]),
    )
    assert r.status_code == 201
    upstreams["mock-a.test"].delay_s = 0.02  # overlap in-flight requests
    results = await asyncio.gather(*(chat(client, g["key"], content=f"r{i}") for i in range(20)))
    codes = [x.status_code for x in results]
    assert codes.count(200) == 5, codes
    assert codes.count(429) == 15, codes
    assert len(upstreams["mock-a.test"].calls) == 5
    async with ctx.db.session() as s:
        p = (await s.execute(select(QuotaPool).where(QuotaPool.limit_value == 5))).scalar_one()
        assert p.used == 5 and p.reserved == 0
        statuses = [x.status for x in (await s.execute(select(Reservation))).scalars().all()]
        assert statuses.count("committed") == 5 and "reserved" not in statuses
