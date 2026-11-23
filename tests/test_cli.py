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


def test_dev_state_dir_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli.build_parser().parse_args(["dev"]).state_dir == ".oasis-dev"
    monkeypatch.setenv("OASIS_ROUTE_DEV_STATE_DIR", "/data/dev")
    assert cli.build_parser().parse_args(["dev"]).state_dir == "/data/dev"
    assert cli.build_parser().parse_args(["dev", "--state-dir", "x"]).state_dir == "x"


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


def test_connect_collects_only_provider_variables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import httpx

    monkeypatch.setenv("GROQ_API_KEY", "gsk_" + "a" * 48)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:secretsecret@db/x")
    env = tmp_path / ".env"
    env.write_text("ZAI_API_KEY=0123456789abcdef0123456789abcdef.AbCdEfGhIjKl\n")
    args = cli.build_parser().parse_args(["connect", "--from-env", "--env-file", str(env)])
    text = cli._connect_text(args)
    assert "GROQ_API_KEY=" in text and "ZAI_API_KEY=" in text and "secretsecret" not in text
    assert args.accept_terms is False  # without consent the command is a dry run

    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/me":
            return httpx.Response(200, json={"project": {"id": "p1"}})
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "dry_run": True,
                "results": [{"key_hint": "…aaaa", "provider_id": "groq", "detected_by": "prefix"}],
            },
        )

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("OASIS_API_KEY", "oasis_test")
    assert cli.main(["connect", "--from-env"]) == 0
    assert sent and sent[0]["dry_run"] is True and sent[0]["accept_terms"] is False
    out = capsys.readouterr()
    assert "groq" in out.out and "Nothing was stored" in out.err
