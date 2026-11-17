"""Test harness.

Default backend is SQLite (fast). Set ``OASIS_TEST_DATABASE_URL`` to a PostgreSQL URL to run the whole suite
against PostgreSQL (CI does both). Migrations are applied once per session via Alembic, so every test also
exercises the real migration path.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from oasis_route.config import Environment, Settings
from oasis_route.context import AppContext
from oasis_route.db import migrate
from oasis_route.db.models import Base
from oasis_route.db.session import Database

PG_URL = os.environ.get("OASIS_TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def _template_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    if PG_URL:
        migrate.downgrade(PG_URL, "base")
        migrate.upgrade(PG_URL)
        yield PG_URL
        return
    path = tmp_path_factory.mktemp("db") / "template.db"
    url = f"sqlite+aiosqlite:///{path}"
    migrate.upgrade(url)
    yield str(path)


@pytest.fixture
async def db_url(_template_db: str, tmp_path: Path) -> AsyncIterator[str]:
    if PG_URL:
        db = Database(PG_URL)
        async with db.engine.begin() as conn:
            names = ", ".join(t.name for t in Base.metadata.sorted_tables)
            await conn.execute(text(f"TRUNCATE {names} CASCADE"))
        await db.dispose()
        yield PG_URL
        return
    target = tmp_path / "test.db"
    shutil.copy(_template_db, target)
    yield f"sqlite+aiosqlite:///{target}"


@pytest.fixture
def settings(db_url: str) -> Settings:
    return Settings(
        environment=Environment.TEST,
        database_url=db_url,
        log_json=True,
        log_level="WARNING",
        admin_bootstrap_token="bootstrap-test-token",
        breaker_base_cooldown_s=1.0,
    )


@pytest.fixture
async def ctx(settings: Settings) -> AsyncIterator[AppContext]:
    from oasis_route.registry.catalog import sync_catalog

    c = AppContext(settings=settings, db=Database(settings.database_url))
    await sync_catalog(c.db, settings)
    yield c
    await c.aclose()


@pytest.fixture
async def client(ctx: AppContext) -> AsyncIterator[httpx.AsyncClient]:
    from oasis_route.app import create_app

    app = create_app(ctx.settings, ctx)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


class HostRouterTransport(httpx.AsyncBaseTransport):
    """Routes upstream requests to per-host in-process ASGI apps (deterministic, no network)."""

    def __init__(self, apps: dict[str, httpx.AsyncBaseTransport]) -> None:
        self.apps = apps

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        t = self.apps.get(request.url.host)
        if t is None:
            raise httpx.ConnectError(f"no route to {request.url.host}", request=request)
        return await t.handle_async_request(request)


@pytest.fixture
def upstreams(ctx: AppContext) -> dict[str, object]:
    """Two independent mock providers reachable as http://mock-a.test/v1 and http://mock-b.test/v1."""
    from oasis_route.devtools.mock_provider import MockProviderState, create_mock_provider

    states = {"mock-a.test": MockProviderState(), "mock-b.test": MockProviderState()}
    ctx.upstream_transport = HostRouterTransport(
        {h: httpx.ASGITransport(app=create_mock_provider(st)) for h, st in states.items()}
    )

    async def resolver(host: str, port: int) -> list[str]:
        return ["127.0.0.1"]

    ctx.resolver = resolver
    ctx.settings.allow_private_upstreams = True
    return states  # type: ignore[return-value]
