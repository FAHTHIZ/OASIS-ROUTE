"""Operator CLI (providers, models, routes, quota, verify, preview, decisions, explain, doctor) against the
real app: the synchronous CLI client is bridged onto the in-process ASGI client from a worker thread."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from oasis_route import cli
from tests.helpers import auth, chat, gateway_setup


class _Bridge(httpx.BaseTransport):
    def __init__(self, ac: httpx.AsyncClient, loop: asyncio.AbstractEventLoop) -> None:
        self.ac, self.loop = ac, loop

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        async def go() -> httpx.Response:
            r = await self.ac.request(
                request.method, str(request.url), content=request.read(), headers=dict(request.headers)
            )
            return httpx.Response(r.status_code, headers=r.headers, content=r.content)

        return asyncio.run_coroutine_threadsafe(go(), self.loop).result(timeout=60)


@pytest.fixture
async def run_cli(client: httpx.AsyncClient, upstreams: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    g = await gateway_setup(client, two_providers=True)
    loop = asyncio.get_running_loop()

    def api(args: Any) -> tuple[httpx.Client, str]:
        c = httpx.Client(
            transport=_Bridge(client, loop), base_url="http://testserver", headers=auth(g["key"])
        )
        assert c.get("/api/v1/me").status_code == 200  # like the real _api: the client is already used
        return c, g["pid"]

    monkeypatch.setattr(cli, "_api", api)

    async def run(*argv: str) -> int:
        return await asyncio.to_thread(cli.main, list(argv))

    run.g = g  # type: ignore[attr-defined]
    return run


async def test_providers_models_routes(run_cli: Any, capsys: pytest.CaptureFixture[str]) -> None:
    assert await run_cli("providers", "--connected") == 0
    out = capsys.readouterr().out
    assert "mock" in out and "2 connected" in out
    assert await run_cli("models", "--provider", "mock") == 0
    assert "mock/mock-large" in capsys.readouterr().out
    assert await run_cli("routes", "--cost", "free") == 0
    out = capsys.readouterr().out
    assert "mock/mock-large" in out and "eligible free" in out


async def test_imported_catalogue_is_labelled_advertised(
    run_cli: Any, ctx: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    from oasis_route.registry.catalog import sync_catalog

    await sync_catalog(ctx.db, ctx.settings.model_copy(update={"catalog_import_enabled": True}))
    assert await run_cli("providers", "--advertised", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) > 50
    assert all(r["advertised_free_tier"] for r in rows)
    curated = {r["id"] for r in rows if (r.get("catalog") or {}).get("curated_override")}
    assert {"groq", "cerebras", "gemini"} <= curated  # hand-written adapters keep their reviewed label
    assert all(r["verification"] == "reviewed" for r in rows if r["id"] in curated)
    assert all(r["verification"] == "advertised" for r in rows if r["id"] not in curated)
    assert await run_cli("providers", "--advertised") == 0
    assert "advertised data: unverified" in capsys.readouterr().out
    assert await run_cli("models", "--search", "cerebras/zai-glm-4.7") == 0
    out = capsys.readouterr().out
    assert "unconfirmed" in out and "(unverified)" in out  # imported models/prices never shown as verified
    assert await run_cli("models", "--routable", "--json", "--limit", "0") == 0
    assert all(m["lifecycle"] != "catalog" for m in json.loads(capsys.readouterr().out))


async def test_quota_shows_cooldowns_after_a_429(
    run_cli: Any, client: httpx.AsyncClient, upstreams: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    upstreams["mock-a.test"].fail_next("mock-large", 429, retry_after=120)
    upstreams["mock-b.test"].fail_next("mock-large", 429, retry_after=120)
    await chat(client, run_cli.g["key"], "auto", "hi")
    assert await run_cli("quota") == 0
    out = capsys.readouterr().out
    assert "Active cooldowns" in out and "none" not in out.split("Active cooldowns")[1].split("Forecast")[0]
    assert "Budgets" in out


async def test_preview_decisions_explain(
    run_cli: Any, client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await run_cli("preview", "hello", "--task", "coding") == 0
    out = capsys.readouterr().out
    assert "Selected" in out and "no provider called" in out
    upstreams_429 = await chat(client, run_cli.g["key"], "auto", "hi")
    rid = upstreams_429.json()["oasis_route"]["request_id"]
    assert await run_cli("decisions") == 0
    assert rid in capsys.readouterr().out
    assert await run_cli("explain", rid) == 0
    out = capsys.readouterr().out
    assert "Reason" in out and "Receipt" in out and "valid=True" in out and "Fallback" in out
    assert await run_cli("explain", "req_missing") == 1


async def test_verify_and_doctor(run_cli: Any, capsys: pytest.CaptureFixture[str]) -> None:
    assert await run_cli("verify") == 0
    out = capsys.readouterr().out
    assert "ok," in out and "free:local" in out
    rc = await run_cli("doctor")
    out = capsys.readouterr().out
    assert "free routes" in out and "credential" in out and "FAIL  server" not in out
    assert rc in (0, 1)
    assert await run_cli("doctor", "--json") in (0, 1)
    checks = json.loads(capsys.readouterr().out)["checks"]
    assert {c["check"] for c in checks} >= {"oasis key", "project", "free routes", "spend policy"}


def test_key_cleanup_accepts_the_dev_key_file() -> None:
    assert cli._clean_key("proj-id\noasis_live_abc_def\n") == "oasis_live_abc_def"
    assert cli._clean_key("  oasis_live_x  ") == "oasis_live_x"


def test_unreachable_server_is_a_clear_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["routes", "--url", "http://127.0.0.1:9", "--oasis-key", "oasis_x"]) == 2
    assert "Cannot reach Oasis Route" in capsys.readouterr().err


def test_parser_has_operator_commands() -> None:
    p = cli.build_parser()
    for argv in (
        ["providers"],
        ["models"],
        ["routes"],
        ["quota"],
        ["verify"],
        ["preview"],
        ["doctor"],
        ["decisions"],
        ["explain", "req_x"],
    ):
        assert callable(p.parse_args(argv).func)


async def test_billing_command_declares_free_tier(
    run_cli: Any, client: httpx.AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await run_cli("billing", "mock", "free_tier") == 0
    assert "billing_mode=free_tier" in capsys.readouterr().out
    g = run_cli.g
    conns = (await client.get(f"/api/v1/projects/{g['pid']}/connections", headers=auth(g["key"]))).json()
    assert {c["billing_mode"] for c in conns if c["provider_id"] == "mock"} == {"free_tier"}
    assert await run_cli("billing", "groq", "free_tier") == 2  # nothing connected for it


async def test_connect_without_confirmation_stores_nothing(
    run_cli: Any, client: httpx.AsyncClient, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    env = tmp_path / ".env"
    env.write_text("GROQ_API_KEY=gsk_" + "a" * 52 + "\n")
    assert await run_cli("connect", "--env-file", str(env)) == 0
    io = capsys.readouterr()
    assert "groq" in io.out and "Nothing was stored" in io.err
    g = run_cli.g
    conns = (await client.get(f"/api/v1/projects/{g['pid']}/connections", headers=auth(g["key"]))).json()
    assert "groq" not in {c["provider_id"] for c in conns}
