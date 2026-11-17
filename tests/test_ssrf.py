"""SSRF defences for upstream endpoints and discovery fetches (threat T7)."""

from __future__ import annotations

import httpx
import pytest

from oasis_route.context import AppContext
from oasis_route.discovery.fetcher import FetchError, fetcher_for
from oasis_route.security.ssrf import UnsafeUrlError, assert_safe_url, check_url_shape
from tests.conftest import FakeWeb
from tests.helpers import auth, bootstrap, create_user


async def _fixed(addr: str):  # type: ignore[no-untyped-def]
    async def r(host: str, port: int) -> list[str]:
        return [addr]

    return r


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.5",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # CGNAT
        "0.0.0.0",  # noqa: S104
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "224.0.0.1",
    ],
)
async def test_non_public_addresses_blocked(addr: str) -> None:
    with pytest.raises(UnsafeUrlError):
        await assert_safe_url("https://example.test/x", resolver=await _fixed(addr))


async def test_literal_ip_and_shapes() -> None:
    with pytest.raises(UnsafeUrlError):
        await assert_safe_url("https://169.254.169.254/latest/meta-data/")
    with pytest.raises(UnsafeUrlError):
        await assert_safe_url("https://[::1]/")
    for bad in (
        "ftp://x.test/",
        "file:///etc/passwd",
        "https://user:pw@x.test/",
        "https:///nohost",
        "gopher://x",
    ):
        with pytest.raises(UnsafeUrlError):
            check_url_shape(bad)
    with pytest.raises(UnsafeUrlError):
        check_url_shape("http://x.test/")  # plain http only when private/local explicitly allowed
    with pytest.raises(UnsafeUrlError):
        check_url_shape("https://metadata.google.internal/")


async def test_public_address_allowed_and_any_private_answer_blocks() -> None:
    await assert_safe_url("https://example.test/", resolver=await _fixed("93.184.216.34"))

    async def mixed(host: str, port: int) -> list[str]:
        return ["93.184.216.34", "10.0.0.1"]  # DNS answer containing any private address is rejected

    with pytest.raises(UnsafeUrlError):
        await assert_safe_url("https://example.test/", resolver=mixed)


async def test_connection_endpoint_ssrf_rejected(client: httpx.AsyncClient, ctx: AppContext) -> None:
    admin = await bootstrap(client)
    u = await create_user(
        client, admin["api_key"]["secret"], "dev@example.com", declared_age=30, country="BD"
    )

    async def meta(host: str, port: int) -> list[str]:
        return ["169.254.169.254"]

    ctx.resolver = meta
    r = await client.post(
        f"/api/v1/projects/{u['project']['id']}/connections",
        json={
            "provider_id": "custom",
            "api_key": "sk-test",
            "base_url": "https://attacker.test/v1",
            "accept_terms": True,
        },
        headers=auth(u["api_key"]["secret"]),
    )
    assert r.status_code == 403, r.text
    assert "non-public" in r.json()["error"]["message"]


async def test_discovery_blocks_private_targets_and_redirects(ctx: AppContext, web: FakeWeb) -> None:
    f = fetcher_for(ctx)
    web.dns["internal.test"] = "10.0.0.7"
    with pytest.raises(FetchError, match="unsafe_url"):
        await f.fetch("https://internal.test/page")
    assert web.requests == []  # nothing was sent
    with pytest.raises(FetchError, match="https"):
        await f.fetch("http://docs.example.test/page")

    # Redirect to a metadata address is re-validated and refused.
    web.page("https://docs.example.test/robots.txt", "User-agent: *\nAllow: /\n", content_type="text/plain")
    web.page("https://docs.example.test/a", "", status=302, location="https://docs.example.test/b")
    web.page("https://docs.example.test/b", "", status=302, location="https://169.254.169.254/latest/")
    with pytest.raises(FetchError):
        await f.fetch("https://docs.example.test/a")
    assert not any(r.url.host == "169.254.169.254" for r in web.requests)

    # Off-scope redirects are refused even to public hosts.
    web.page("https://docs.example.test/c", "", status=301, location="https://elsewhere.test/")
    with pytest.raises(FetchError, match="redirect_off_scope"):
        await f.fetch("https://docs.example.test/c")


async def test_discovery_size_and_type_caps(ctx: AppContext, web: FakeWeb) -> None:
    ctx.settings.discovery_max_bytes = 2048
    f = fetcher_for(ctx)
    web.page("https://docs.example.test/robots.txt", "", status=404)
    web.page("https://docs.example.test/big", "x" * 5000)
    with pytest.raises(FetchError, match="too_large"):
        await f.fetch("https://docs.example.test/big")
    web.page("https://docs.example.test/bin", b"\x00\x01", content_type="application/octet-stream")
    with pytest.raises(FetchError, match="unsupported_content_type"):
        await f.fetch("https://docs.example.test/bin")
    # No cookies or credentials are ever sent.
    assert all("cookie" not in r.headers and "authorization" not in r.headers for r in web.requests)
