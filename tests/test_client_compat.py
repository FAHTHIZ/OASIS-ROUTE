"""Client compatibility: every integration recipe's protocol works end to end with route metadata."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from oasis_route import cli, integrations
from tests.helpers import auth, gateway_setup

TOOLS = ("claude_code", "codex", "cursor", "cline", "roo_code", "aider", "opencode", "continue", "openai_sdk")


def test_recipes_cover_the_tools_and_hold_no_secrets() -> None:
    rs = {r["id"]: r for r in integrations.recipes("https://gw.example.com")}
    assert set(TOOLS) <= set(rs)
    for r in rs.values():
        blob = json.dumps(r)
        assert "oasis_" not in blob.replace("oasis_route", "")  # never embeds a real Oasis Key
        assert r["endpoint"].startswith("https://gw.example.com/v1/")
    assert rs["codex"]["protocol"] == "openai_responses" and "wire_api" in rs["codex"]["files"][0]["content"]
    assert rs["claude_code"]["env"]["ANTHROPIC_BASE_URL"] == "https://gw.example.com"
    assert rs["cursor"]["model"] == "auto" and any("localhost" in n for n in rs["cursor"]["notes"])
    json.loads(rs["opencode"]["files"][0]["content"])  # valid JSON
    assert integrations.recipe("Roo-Code", "http://x")["id"] == "roo_code"  # type: ignore[index]
    assert integrations.recipe("nope", "http://x") is None


def test_cli_integrate_prints_without_a_server(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["integrate", "aider", "--url", "http://gw:8080"]) == 0
    out = capsys.readouterr().out
    assert 'export OPENAI_API_BASE="http://gw:8080/v1"' in out and "aider --model openai/auto/coding" in out
    assert cli.main(["integrate"]) == 0
    assert "claude_code" in capsys.readouterr().out
    assert cli.main(["integrate", "nope"]) == 2


async def _call(client: httpx.AsyncClient, key: str, r: dict[str, Any]) -> httpx.Response:
    model = r["model"].removeprefix("openai/").removeprefix("oasis/")
    msgs = [{"role": "user", "content": "hi"}]
    if r["protocol"] == "anthropic_messages":
        return await client.post(
            "/v1/messages", json={"model": model, "max_tokens": 16, "messages": msgs}, headers=auth(key)
        )
    if r["protocol"] == "openai_responses":
        return await client.post("/v1/responses", json={"model": model, "input": "hi"}, headers=auth(key))
    return await client.post(
        "/v1/chat/completions", json={"model": model, "messages": msgs, "max_tokens": 16}, headers=auth(key)
    )


async def test_every_recipe_protocol_serves_with_route_metadata(
    client: httpx.AsyncClient, upstreams: Any
) -> None:
    g = await gateway_setup(client)
    listed = await client.get("/api/v1/integrations", headers=auth(g["key"]))
    assert listed.status_code == 200, listed.text
    for r in listed.json()["integrations"]:
        resp = await _call(client, g["key"], r)
        assert resp.status_code == 200, (r["id"], resp.text)
        assert resp.headers["x-oasis-route-provider"] and resp.headers["x-oasis-route-paid"] == "false"
        assert resp.json()["oasis_route"]["route"]["reason"], r["id"]
    one = await client.get("/api/v1/integrations/codex", headers=auth(g["key"]))
    assert one.status_code == 200 and "model_providers.oasis" in one.json()["text"]
    assert (await client.get("/api/v1/integrations/nope", headers=auth(g["key"]))).status_code == 404


async def test_tools_streaming_usage_and_errors(client: httpx.AsyncClient, upstreams: Any) -> None:
    g = await gateway_setup(client)
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "auto", "messages": [{"role": "user", "content": "use_tool"}], "tools": tools},
        headers=auth(g["key"]),
    )
    assert r.status_code == 200, r.text
    msg = r.json()["choices"][0]["message"]
    assert msg["tool_calls"][0]["function"]["name"] == "read_file" and r.json()["usage"]["total_tokens"] > 0

    first: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "auto",
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers=auth(g["key"]),
    ) as s:
        assert s.headers["x-oasis-route-spend-mode"] == "zero_budget"
        async for line in s.aiter_lines():
            if line.startswith("data: {"):
                data = json.loads(line[6:])
                first = first or data
                usage = data.get("usage") or usage
    assert first is not None and first["oasis_route"]["request_id"] and "spend" in first["oasis_route"]
    assert usage is not None and usage["total_tokens"] > 0

    bad = await client.post(
        "/v1/chat/completions",
        json={"model": "no-such-model", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth(g["key"]),
    )
    assert bad.status_code == 404
    err = bad.json()
    assert err["error"]["code"] == "model_not_found" and err["oasis_route"]["request_id"]
    anth = await client.post(
        "/v1/messages",
        json={"model": "no-such-model", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]},
        headers=auth(g["key"]),
    )
    assert anth.status_code == 404 and anth.json()["type"] == "error"
    assert anth.json()["oasis_route"]["request_id"]
