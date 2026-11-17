"""Master key (KEK) rotation: automatic re-wrap lets the old key be retired without losing credentials."""

from __future__ import annotations

import base64
import os
from typing import Any

import httpx
from pydantic import SecretStr
from sqlalchemy import select

from oasis_route.context import AppContext
from oasis_route.db.models import Connection, Credential
from oasis_route.db.session import Database
from oasis_route.security.vault import VaultError
from oasis_route.worker import runner
from tests.helpers import bootstrap, connect_mock, create_user

DEV_KEK = base64.b64encode(b"oasis-route-dev-insecure-kek-32b").decode()


async def _open_all(c: AppContext) -> list[dict[str, Any]]:
    from oasis_route.connections import service as conns

    out = []
    async with c.db.session() as s:
        for conn in (await s.execute(select(Connection))).scalars():
            cred = await s.get(Credential, conn.credential_id)
            out.append(conns.open_secret(c, conn, cred))
    return out


async def test_rewrap_then_retire_old_key(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    admin = (await bootstrap(client))["api_key"]["secret"]
    u = await create_user(client, admin, "dev@example.com", declared_age=30, country="BD")
    await connect_mock(client, u["api_key"]["secret"], u["project"]["id"], api_key="sk-rotate-me")

    new_kek = SecretStr(base64.b64encode(os.urandom(32)).decode())
    rotated = ctx.settings.model_copy(
        update={"master_key": new_kek, "previous_master_keys": [SecretStr(DEV_KEK)]}
    )
    c2 = AppContext(settings=rotated, db=Database(rotated.database_url))
    try:
        assert (await _open_all(c2))[0]["api_key"] == "sk-rotate-me"  # old key still decrypts
        r = await runner.rewrap_credentials(c2)
        assert r == {"rewrapped": 1, "failed": 0, "remaining": 0}
        assert (await runner.rewrap_credentials(c2))["rewrapped"] == 0  # idempotent
    finally:
        await c2.aclose()

    retired = rotated.model_copy(update={"previous_master_keys": []})
    c3 = AppContext(settings=retired, db=Database(retired.database_url))
    try:
        assert (await _open_all(c3))[0]["api_key"] == "sk-rotate-me"
    finally:
        await c3.aclose()

    # The original key alone can no longer open the re-wrapped credential.
    try:
        await _open_all(ctx)
        raise AssertionError("expected VaultError")
    except VaultError:
        pass


async def test_rewrap_reports_unknown_keys(
    ctx: AppContext, client: httpx.AsyncClient, upstreams: dict[str, Any]
) -> None:
    admin = (await bootstrap(client))["api_key"]["secret"]
    u = await create_user(client, admin, "dev@example.com", declared_age=30, country="BD")
    await connect_mock(client, u["api_key"]["secret"], u["project"]["id"])
    lost = ctx.settings.model_copy(
        update={"master_key": SecretStr(base64.b64encode(os.urandom(32)).decode())}
    )
    c2 = AppContext(settings=lost, db=Database(lost.database_url))
    try:
        assert await runner.rewrap_credentials(c2) == {"rewrapped": 0, "failed": 1, "remaining": 1}
    finally:
        await c2.aclose()
