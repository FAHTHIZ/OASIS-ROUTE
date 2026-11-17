"""T9 DoS controls: per-key token bucket, body size limit, bounded fallback hops."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import delete, select

from oasis_route.config import Settings
from oasis_route.context import AppContext
from oasis_route.db.models import Project, RateLimitWindow
from oasis_route.db.session import Database
from oasis_route.security.rate_limit import DatabaseRateLimiter, TokenBucketLimiter
from tests.helpers import auth, chat, gateway_setup


def test_token_bucket_refills_and_reports_retry_after() -> None:
    lim = TokenBucketLimiter()
    assert all(lim.check("k", 60, now=0.0)[0] for _ in range(60))
    ok, retry = lim.check("k", 60, now=0.0)
    assert not ok and retry == pytest.approx(1.0)
    assert lim.check("k", 60, now=1.0)[0]  # one token per second at 60 rpm
    assert lim.check("other", 60, now=0.0)[0]  # buckets are per key


def test_token_bucket_memory_is_bounded() -> None:
    lim = TokenBucketLimiter(max_keys=10)
    for i in range(25):
        lim.check(f"k{i}", 5, now=0.0)
    assert len(lim._buckets) <= 10


@pytest.mark.usefixtures("upstreams")
async def test_per_key_limit_returns_429_with_retry_after(client: httpx.AsyncClient, ctx: AppContext) -> None:
    g = await gateway_setup(client, two_providers=False)
    async with ctx.db.transaction() as s:
        p = await s.get(Project, g["pid"])
        assert p is not None
        p.rate_limit_rpm = 3
        await s.execute(delete(RateLimitWindow))  # shared-counter backend: forget setup traffic
    codes = [(await chat(client, g["key"])).status_code for _ in range(3)]
    assert codes == [200, 200, 200]
    r = await chat(client, g["key"])
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"
    assert int(r.headers["retry-after"]) >= 1
    # Admin API calls draw from the same per-key bucket.
    r = await client.get(f"/api/v1/projects/{g['pid']}/capacity", headers=auth(g["key"]))
    assert r.status_code == 429
    # Another key is unaffected.
    r = await client.get(f"/api/v1/projects/{g['pid']}/capacity", headers=auth(g["admin"]))
    assert r.status_code == 200


@pytest.mark.usefixtures("upstreams")
async def test_oversized_body_rejected_with_413(client: httpx.AsyncClient, ctx: AppContext) -> None:
    g = await gateway_setup(client, two_providers=False)
    big = "x" * (ctx.settings.max_request_bytes + 10)
    r = await chat(client, g["key"], content=big)
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large"


async def test_oversized_body_rejected_before_auth(client: httpx.AsyncClient, ctx: AppContext) -> None:
    r = await client.post(
        "/api/v1/bootstrap",
        content=b"{" + b" " * (ctx.settings.max_request_bytes + 1) + b"}",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 413


async def test_fallback_hops_are_bounded(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client)
    for host in ("mock-a.test", "mock-b.test"):
        for m in ("mock-large", "mock-medium", "mock-small"):
            upstreams[host].fail_next(m, 404, code="model_not_found", times=5)
    r = await chat(client, g["key"])
    assert r.status_code >= 400
    total = len(upstreams["mock-a.test"].calls) + len(upstreams["mock-b.test"].calls)
    assert total <= ctx.settings.max_fallback_hops + 1


async def test_database_limiter_sliding_window(ctx: AppContext) -> None:
    lim = DatabaseRateLimiter()
    t0 = 6_000_000 * 60.0  # start of a minute

    async def hit(now: float, key: str = "k") -> tuple[bool, float]:
        async with ctx.db.transaction() as s:
            return await lim.check(s, key, 4, now=now)

    assert [(await hit(t0 + i))[0] for i in range(4)] == [True] * 4
    ok, retry = await hit(t0 + 5)
    assert not ok and 0 < retry <= 55
    assert (await hit(t0 + 5, key="other"))[0]  # counters are per key
    # 30 s into the next minute half of the previous minute still counts: 4 * 0.5 = 2, so two more fit.
    assert [(await hit(t0 + 90 + i / 10))[0] for i in range(3)] == [True, True, False]
    # Rejected requests are not counted, and stale windows are deleted when a new minute starts.
    assert (await hit(t0 + 60 * 5))[0]
    async with ctx.db.session() as s:
        windows = sorted(
            (await s.execute(select(RateLimitWindow.window).where(RateLimitWindow.key == "k"))).scalars()
        )
    assert windows == [int(t0 // 60) + 5]


async def test_database_limiter_is_shared_between_replicas(settings: Settings) -> None:
    """Two app contexts (two replicas) on one database share the same per-key budget."""
    s = settings.model_copy(update={"rate_limit_backend": "database"})
    a = AppContext(settings=s, db=Database(s.database_url))
    b = AppContext(settings=s, db=Database(s.database_url))
    try:
        assert a.shared_rate_limits and b.shared_rate_limits
        results = []
        for i in range(6):
            c = a if i % 2 == 0 else b
            async with c.db.transaction() as sess:
                results.append((await c.db_limiter.check(sess, "key:x", 4, now=600.0 + i))[0])
        assert results == [True, True, True, True, False, False]
    finally:
        await a.aclose()
        await b.aclose()


def test_auto_backend_uses_memory_on_sqlite(ctx: AppContext) -> None:
    assert ctx.settings.rate_limit_backend == "auto"
    assert ctx.shared_rate_limits is (not ctx.db.is_sqlite)


@pytest.mark.usefixtures("upstreams")
async def test_per_key_limit_with_database_backend(client: httpx.AsyncClient, ctx: AppContext) -> None:
    ctx.settings = ctx.settings.model_copy(update={"rate_limit_backend": "database"})
    g = await gateway_setup(client, two_providers=False)
    async with ctx.db.transaction() as s:
        p = await s.get(Project, g["pid"])
        assert p is not None
        p.rate_limit_rpm = 2
        await s.execute(delete(RateLimitWindow))  # forget the requests made during setup
    codes = [(await chat(client, g["key"])).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
