"""CLI: argument parsing, bootstrap, migrations and one-shot worker against a real database."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oasis_route import cli
from oasis_route.config import get_settings
from oasis_route.context import AppContext


def test_parser_commands() -> None:
    p = cli.build_parser()
    a = p.parse_args(["serve", "--migrate", "--no-worker", "--port", "9000"])
    assert a.migrate and a.no_worker and a.port == 9000
    assert p.parse_args(["worker", "--once"]).once is True
    assert p.parse_args(["dev"]).port == 8080
    assert p.parse_args(["migrate"]).direction == "up"
    b = p.parse_args(["bootstrap", "--email", "a@b.c"])
    assert b.email == "a@b.c" and b.project == "default"
    with pytest.raises(SystemExit):
        p.parse_args(["serve", "--with-worker"])  # worker is on by default now


async def test_bootstrap_admin_once(ctx: AppContext) -> None:
    out = await cli._bootstrap_admin(ctx, "admin@example.com", "main")
    assert out is not None
    pid, secret = out
    assert secret.startswith("oasis_") and len(pid) == 36
    assert await cli._bootstrap_admin(ctx, "other@example.com", "x") is None


def test_migrate_bootstrap_and_worker_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "cli.db"
    monkeypatch.setenv("OASIS_ROUTE_DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    monkeypatch.setenv("OASIS_ROUTE_ENVIRONMENT", "development")
    get_settings.cache_clear()
    try:
        assert cli.main(["migrate", "up"]) == 0
        assert cli.main(["bootstrap", "--email", "root@example.com"]) == 0
        out = capsys.readouterr().out
        assert "OASIS_API_KEY=oasis_" in out
        assert cli.main(["bootstrap", "--email", "again@example.com"]) == 1
        capsys.readouterr()
        assert cli.main(["worker", "--once"]) == 0
        ran = json.loads(capsys.readouterr().out)
        assert "ledger.expire_reservations" in ran and "quota.refresh" in ran
    finally:
        get_settings.cache_clear()
