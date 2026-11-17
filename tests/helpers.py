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
