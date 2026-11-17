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
