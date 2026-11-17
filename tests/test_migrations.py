"""Migrations apply and revert cleanly on a fresh database (SQLite tier)."""

from pathlib import Path

import sqlalchemy as sa

from oasis_route.db import migrate
from oasis_route.db.models import Base


def test_upgrade_downgrade_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    migrate.upgrade(url)
    eng = sa.create_engine(f"sqlite:///{path}")
    tables = set(sa.inspect(eng).get_table_names())
    assert {t.name for t in Base.metadata.sorted_tables} <= tables
    migrate.downgrade(url, "base")
    assert set(sa.inspect(eng).get_table_names()) <= {"alembic_version"}
    migrate.upgrade(url)
    eng.dispose()


def test_models_match_migrations(tmp_path: Path) -> None:
    """Autogenerate against the migrated DB must produce no diff (schema drift guard)."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    path = tmp_path / "d.db"
    migrate.upgrade(f"sqlite+aiosqlite:///{path}")
    eng = sa.create_engine(f"sqlite:///{path}")
    with eng.connect() as conn:
        mc = MigrationContext.configure(conn, opts={"compare_type": False})
        diff = compare_metadata(mc, Base.metadata)
    eng.dispose()
    assert diff == []
