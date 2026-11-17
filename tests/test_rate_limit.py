"""T9 DoS controls: per-key token bucket, body size limit, bounded fallback hops."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from oasis_route.context import AppContext
from oasis_route.db.models import Project
from oasis_route.security.rate_limit import TokenBucketLimiter
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
