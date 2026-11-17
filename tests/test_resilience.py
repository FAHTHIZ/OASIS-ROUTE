"""Load, concurrency, soak and fault-injection tests for the gateway, ledger and failover engine.

A seeded chaos transport sits in front of provider A and injects connection errors, timeouts, 5xx and
429 responses. Provider B stays healthy. Invariants checked after every burst:

- every response is a success or a documented error (never a 500 from Oasis Route itself);
- quota pools are never overspent and nothing is left reserved;
- every reservation reaches a terminal state, and usage records match the successes;
- background tasks do not leak.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any

import httpx
from sqlalchemy import func, select

from oasis_route.context import AppContext
from oasis_route.db.models import QuotaPool, Reservation, UsageRecord
from tests.helpers import auth, chat, gateway_setup

DOCUMENTED = {200, 429, 502, 503}


class ChaosTransport(httpx.AsyncBaseTransport):
    """Wraps the upstream transport; injects faults for one host with a fixed seed."""

    def __init__(self, inner: httpx.AsyncBaseTransport, host: str, rate: float, seed: int = 7) -> None:
        self.inner, self.host, self.rate = inner, host, rate
        self.rng = random.Random(seed)
        self.injected: dict[str, int] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        targeted = request.url.host == self.host and request.url.path.endswith("/chat/completions")
        if targeted and self.rng.random() < self.rate:
            kind = self.rng.choice(["connect", "timeout", "500", "503", "429"])
            self.injected[kind] = self.injected.get(kind, 0) + 1
            await asyncio.sleep(self.rng.random() * 0.01)
            if kind == "connect":
                raise httpx.ConnectError("injected connection reset", request=request)
            if kind == "timeout":
                raise httpx.ReadTimeout("injected timeout", request=request)
            status = int(kind)
            headers = {"retry-after": "1"} if status == 429 else {}
            body = {"error": {"message": "injected", "type": "server_error"}}
            return httpx.Response(status, json=body, headers=headers, request=request)
        return await self.inner.handle_async_request(request)


async def _invariants(ctx: AppContext, successes: int) -> None:
    async with ctx.db.session() as s:
        for p in (await s.execute(select(QuotaPool))).scalars():
            assert p.reserved == 0, f"pool {p.id} left {p.reserved} reserved"
            if p.limit_value is not None:
                assert p.used <= p.limit_value, f"pool {p.id} overspent: {p.used}/{p.limit_value}"
        statuses = list((await s.execute(select(Reservation.status))).scalars())
        assert "reserved" not in statuses, statuses
        ok_usage = (
            await s.execute(
                select(func.count()).select_from(UsageRecord).where(UsageRecord.outcome == "success")
            )
        ).scalar_one()
        assert ok_usage == successes, (ok_usage, successes)


async def test_concurrent_burst_with_injected_faults_fails_over_and_keeps_ledger_exact(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client)
    chaos = ChaosTransport(ctx.upstream_transport, "mock-a.test", rate=0.35)  # type: ignore[arg-type]
    ctx.upstream_transport = chaos
    ctx._upstream_client = None  # rebuild the shared client on the chaos transport
    for st in upstreams.values():
        st.delay_s = 0.005

    results = await asyncio.gather(*(chat(client, g["key"], content=f"burst {i}") for i in range(80)))
    codes = [r.status_code for r in results]
    assert set(codes) <= DOCUMENTED, codes
    ok = [r for r in results if r.status_code == 200]
    # Provider B is healthy, so injected faults on A are absorbed by failover.
    assert len(ok) == 80, [r.text for r in results if r.status_code != 200][:3]
    assert sum(chaos.injected.values()) > 0
    assert any(r.headers.get("x-oasis-route-fallback") == "true" for r in ok)
    for r in ok:
        assert r.json()["choices"][0]["message"]["content"]
    await _invariants(ctx, successes=len(ok))


async def test_pool_limits_hold_under_concurrency_and_faults(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client)
    for provider, limit in (("mock", 12), ("mock_alt", 9)):
        r = await client.post(
            f"/api/v1/projects/{g['pid']}/quota-pools",
            json={"provider_id": provider, "unit": "requests", "period": "day", "limit_value": limit},
            headers=auth(g["key"]),
        )
        assert r.status_code == 201, r.text
    chaos = ChaosTransport(ctx.upstream_transport, "mock-a.test", rate=0.3, seed=11)  # type: ignore[arg-type]
    ctx.upstream_transport = chaos
    ctx._upstream_client = None
    for st in upstreams.values():
        st.delay_s = 0.01

    results = await asyncio.gather(*(chat(client, g["key"], content=f"q {i}") for i in range(60)))
    codes = [r.status_code for r in results]
    assert set(codes) <= DOCUMENTED, codes
    ok = codes.count(200)
    assert 0 < ok <= 21, codes  # never more successes than the combined allocation
    exhausted = [r for r in results if r.status_code == 429]
    assert exhausted and all(r.json()["error"]["code"] == "capacity_exhausted" for r in exhausted)
    assert all("retry-after" in r.headers for r in exhausted)
    calls_a = len(upstreams["mock-a.test"].calls)
    calls_b = len(upstreams["mock-b.test"].calls)
    assert calls_b <= 9  # injected faults on A happen before its mock is reached
    assert calls_a <= 12
    await _invariants(ctx, successes=ok)


async def test_streaming_under_concurrency_and_faults(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client)
    chaos = ChaosTransport(ctx.upstream_transport, "mock-a.test", rate=0.4, seed=3)  # type: ignore[arg-type]
    ctx.upstream_transport = chaos
    ctx._upstream_client = None

    async def stream(i: int) -> tuple[int, str]:
        body = {"model": "auto", "stream": True, "messages": [{"role": "user", "content": f"s{i}"}]}
        async with client.stream("POST", "/v1/chat/completions", json=body, headers=auth(g["key"])) as r:
            text = "".join([chunk async for chunk in r.aiter_text()])
            return r.status_code, text

    out = await asyncio.gather(*(stream(i) for i in range(30)))
    for status, text in out:
        assert status == 200, text
        assert text.rstrip().endswith("data: [DONE]"), text[-200:]
        deltas = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: {")]
        assert any(d["choices"] and d["choices"][0].get("delta", {}).get("content") for d in deltas)
    await _invariants(ctx, successes=30)


async def test_soak_many_sequential_batches_no_leaks(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client)
    chaos = ChaosTransport(ctx.upstream_transport, "mock-a.test", rate=0.15, seed=5)  # type: ignore[arg-type]
    ctx.upstream_transport = chaos
    ctx._upstream_client = None
    baseline = len(asyncio.all_tasks())
    total_ok = 0
    for batch in range(10):
        results = await asyncio.gather(*(chat(client, g["key"], content=f"{batch}-{i}") for i in range(25)))
        assert {r.status_code for r in results} == {200}, [r.text for r in results if r.status_code != 200][
            :2
        ]
        total_ok += len(results)
    await asyncio.sleep(0)
    assert len(asyncio.all_tasks()) <= baseline + 2  # no orphaned settlement or relay tasks
    await _invariants(ctx, successes=total_ok)


async def test_total_outage_returns_documented_error_and_releases_everything(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client)
    inner = ctx.upstream_transport
    a = ChaosTransport(inner, "mock-a.test", rate=1.0)  # type: ignore[arg-type]
    ctx.upstream_transport = ChaosTransport(a, "mock-b.test", rate=1.0, seed=9)
    ctx._upstream_client = None
    results = await asyncio.gather(*(chat(client, g["key"], content=f"o{i}") for i in range(20)))
    codes = {r.status_code for r in results}
    assert codes <= {429, 502, 503}, codes
    for r in results:
        err = r.json()["error"]
        assert err["code"] in {"capacity_exhausted", "upstream_failed", "no_eligible_route"}, err
        assert "injected" not in json.dumps(err).lower() or err["code"] == "upstream_failed"
    await _invariants(ctx, successes=0)
