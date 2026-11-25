"""Adapter layer: providers plug in through profiles, never through router changes."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from oasis_route.adapters.anthropic import AnthropicAdapter
from oasis_route.adapters.base import ChatRequest, ConnectionContext
from oasis_route.adapters.openai_compat import OpenAICompatibleAdapter
from oasis_route.adapters.registry import LOCAL_PROFILES, PROFILES, AdapterRegistry, profile_from_catalog
from oasis_route.registry import imported
from oasis_route.registry.catalog import LOCAL_SERVER_IDS, get_seed
from oasis_route.routing.cost import estimate_cost
from oasis_route.security.ssrf import resolves_private_only
from tests.helpers import auth, bootstrap, create_user


def _conn(seen: list[httpx.Request], provider: str, key: str = "k-123") -> ConnectionContext:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "model": "m",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "type": "message",
                "role": "assistant",
            },
        )

    return ConnectionContext(
        provider_id=provider,
        base_url="https://api.example.test/v1",
        secret={"api_key": key} if key else {"api_key": ""},
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def test_every_imported_active_provider_gets_an_adapter() -> None:
    reg = AdapterRegistry()
    for p in imported.providers():
        if p.status == "active":
            assert reg.for_provider(p.id, p.adapter_key) is not None, p.id


@pytest.mark.parametrize(
    ("style", "prefix", "header", "expected"),
    [
        ("bearer", None, "authorization", "Bearer k-123"),
        ("x-api-key", None, "x-api-key", "k-123"),
        ("header:Api-Key", None, "api-key", "k-123"),
        ("header:Authorization", "Key ", "authorization", "Key k-123"),
    ],
)
async def test_catalog_auth_styles(style: str, prefix: str | None, header: str, expected: str) -> None:
    profile = profile_from_catalog("x", {"auth": {"style": style, "prefix": prefix}})
    seen: list[httpx.Request] = []
    await OpenAICompatibleAdapter(profile).invoke(_conn(seen, "x"), ChatRequest(model="m", messages=[]))
    assert seen[0].headers[header] == expected
    if header != "authorization":
        assert "authorization" not in seen[0].headers


async def test_anthropic_compatible_bearer_auth() -> None:
    entry = next(p for p in imported.providers() if p.adapter_key == "anthropic" and p.id != "anthropic")
    adapter = AdapterRegistry().for_provider(entry.id, "anthropic")
    assert isinstance(adapter, AnthropicAdapter)
    seen: list[httpx.Request] = []
    msg = [{"role": "user", "content": "hi"}]
    await adapter.invoke(_conn(seen, entry.id), ChatRequest(model="m", messages=msg, max_tokens=5))
    style = (entry.raw.get("auth") or {}).get("style")
    if style == "bearer":
        assert seen[0].headers["authorization"] == "Bearer k-123"
    assert seen[0].headers["anthropic-version"]


async def test_default_anthropic_still_uses_x_api_key() -> None:
    seen: list[httpx.Request] = []
    msg = [{"role": "user", "content": "hi"}]
    await AnthropicAdapter().invoke(
        _conn(seen, "anthropic"), ChatRequest(model="m", messages=msg, max_tokens=5)
    )
    assert seen[0].headers["x-api-key"] == "k-123" and "authorization" not in seen[0].headers


async def test_local_presets_send_no_key() -> None:
    assert {"ollama", "llama_cpp", "vllm", "lm_studio"} <= LOCAL_SERVER_IDS
    for pid in LOCAL_SERVER_IDS:
        seed = get_seed(pid)
        assert seed is not None and "local" in seed.regions
        assert seed.base_url.startswith("http://127.0.0.1:")
        assert pid in LOCAL_PROFILES and pid not in PROFILES
    seen: list[httpx.Request] = []
    adapter = AdapterRegistry().for_provider("ollama", "openai_compatible")
    await adapter.invoke(_conn(seen, "ollama", key=""), ChatRequest(model="m", messages=[]))
    assert "authorization" not in seen[0].headers


async def test_private_only_resolution() -> None:
    async def public(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    assert await resolves_private_only("http://127.0.0.1:11434/v1")
    assert await resolves_private_only("http://192.168.1.4:8000/v1")
    assert not await resolves_private_only("https://ollama.com/v1", resolver=public)
    assert not await resolves_private_only("ftp://127.0.0.1/")


async def test_local_preset_refuses_public_urls(client: httpx.AsyncClient, ctx: Any) -> None:
    admin = await bootstrap(client)
    u = await create_user(client, admin["api_key"]["secret"], "loc@example.com")
    key, pid = u["api_key"]["secret"], u["project"]["id"]

    async def public(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    ctx.resolver = public
    r = await client.post(
        f"/api/v1/projects/{pid}/connections",
        json={"provider_id": "ollama", "base_url": "https://ollama.example.com/v1", "validate_now": False},
        headers=auth(key),
    )
    assert r.status_code == 403, r.text
    assert "private or loopback" in r.json()["error"]["message"]


def test_cost_estimates_are_honest() -> None:
    assert estimate_cost({}, 1000, 1000).status == "unknown"
    assert estimate_cost({}, 1000, 1000).usd_micros is None
    assert estimate_cost({"free": True, "source": "x"}, 10**6, 10**6).usd_micros == 0
    assert estimate_cost({}, 10, 10, local=True).status == "free"
    priced = estimate_cost({"prompt_usd_per_mtok": 1.0, "completion_usd_per_mtok": 2.0}, 1000, 500)
    assert priced.status == "priced" and priced.usd_micros == 2000 and priced.verified
    imp = estimate_cost(
        {
            "prompt_usd_per_mtok": 0.0,
            "completion_usd_per_mtok": 0.0,
            "source": "omniroute_catalog",
            "verified": False,
        },
        1000,
        1000,
    )
    # A zero price from a research snapshot is an unverified estimate, never "free".
    assert imp.status == "priced" and not imp.verified
    # Rounded up: one token at $0.10/Mtok is 0.1 micros -> 1 micro.
    assert estimate_cost({"prompt_usd_per_mtok": 0.1, "completion_usd_per_mtok": 0.1}, 1, 0).usd_micros == 1
