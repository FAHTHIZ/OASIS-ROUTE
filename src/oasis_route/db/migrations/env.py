"""Alembic environment (async)."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from oasis_route.config import get_settings
from oasis_route.db.models import Base
from oasis_route.db.session import Database

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, render_as_batch=True, compare_type=True
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    db = Database(_url())
    async with db.engine.connect() as conn:
        await conn.run_sync(_do_run)
        await conn.commit()
    await db.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
