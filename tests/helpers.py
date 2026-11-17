"""Shared test helpers."""

from __future__ import annotations

from typing import Any

import httpx


async def bootstrap(client: httpx.AsyncClient, email: str = "admin@example.com") -> dict[str, Any]:
    r = await client.post("/api/v1/bootstrap", json={"token": "bootstrap-test-token", "email": email})
    assert r.status_code == 201, r.text
    data: dict[str, Any] = r.json()
    return data


def auth(secret: str) -> dict[str, str]:
    return {"authorization": f"Bearer {secret}"}


async def create_user(
    client: httpx.AsyncClient, admin_secret: str, email: str, **extra: Any
) -> dict[str, Any]:
    r = await client.post("/api/v1/users", json={"email": email, **extra}, headers=auth(admin_secret))
    assert r.status_code == 201, r.text
    data: dict[str, Any] = r.json()
    return data


async def connect_mock(
    client: httpx.AsyncClient,
    secret: str,
    project_id: str,
    *,
    provider: str = "mock",
    host: str = "mock-a.test",
    api_key: str = "mock-key",
    **extra: Any,
) -> dict[str, Any]:
    body = {
        "provider_id": provider,
        "api_key": api_key,
        "base_url": f"http://{host}/v1",
        "accept_terms": True,
        **extra,
    }
    r = await client.post(f"/api/v1/projects/{project_id}/connections", json=body, headers=auth(secret))
    assert r.status_code == 201, r.text
    data: dict[str, Any] = r.json()
    return data


async def gateway_setup(client: httpx.AsyncClient, *, two_providers: bool = True) -> dict[str, Any]:
    """Admin + a user project with mock (mock-a.test) and optionally mock_alt (mock-b.test) connected."""
    admin = await bootstrap(client)
    u = await create_user(
        client, admin["api_key"]["secret"], "dev@example.com", declared_age=30, country="BD"
    )
    key, pid = u["api_key"]["secret"], u["project"]["id"]
    a = await connect_mock(client, key, pid, provider="mock", host="mock-a.test")
    out = {"admin": admin["api_key"]["secret"], "key": key, "pid": pid, "conn_a": a}
    if two_providers:
        out["conn_b"] = await connect_mock(client, key, pid, provider="mock_alt", host="mock-b.test")
    return out


async def chat(
    client: httpx.AsyncClient,
    key: str,
    model: str = "auto",
    content: str = "hi",
    *,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> httpx.Response:
    body = {"model": model, "messages": [{"role": "user", "content": content}], **extra}
    h = {**auth(key), **(headers or {})}
    return await client.post("/v1/chat/completions", json=body, headers=h)
