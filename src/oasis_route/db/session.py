"""Async engine and session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


class Database:
    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.url = url
        kwargs: dict[str, Any] = {"echo": echo, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            # Busy timeout lets concurrent writers wait instead of failing immediately.
            kwargs["connect_args"] = {"timeout": 30}
        else:
            kwargs["pool_size"] = 10
            kwargs["max_overflow"] = 20
        self.engine: AsyncEngine = create_async_engine(url, **kwargs)
        if url.startswith("sqlite"):
            self._configure_sqlite()
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    def _configure_sqlite(self) -> None:
        @event.listens_for(self.engine.sync_engine, "connect")
        def _pragmas(dbapi_conn: Any, _rec: Any) -> None:
            # Disable pysqlite's implicit transaction handling so BEGIN IMMEDIATE is honoured.
            dbapi_conn.isolation_level = None
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        @event.listens_for(self.engine.sync_engine, "begin")
        def _begin(conn: Any) -> None:
            # Take the write lock at transaction start: serializes writers, making ledger updates atomic.
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    @property
    def is_sqlite(self) -> bool:
        return self.url.startswith("sqlite")

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as s:
            yield s

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as s, s.begin():
            yield s

    async def dispose(self) -> None:
        await self.engine.dispose()
