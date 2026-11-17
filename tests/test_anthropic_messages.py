"""Anthropic Messages API compatibility: /v1/messages routes through the same engine as chat completions."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from oasis_route.gateway import anthropic_compat as ac
from tests.helpers import gateway_setup


def _xkey(secret: str) -> dict[str, str]:
    # Anthropic SDKs authenticate with x-api-key and send anthropic-version.
    return {"x-api-key": secret, "anthropic-version": "2023-06-01"}


def _events(raw: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for frame in raw.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_request_translation() -> None:
    body = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 100,
        "system": [{"type": "text", "text": "Be brief."}],
        "stop_sequences": ["END"],
        "tools": [
            {"name": "search", "description": "Search", "input_schema": {"type": "object"}},
            {"type": "web_search_20250305", "name": "web_search"},  # server tool: not routable, dropped
        ],
        "tool_choice": {"type": "any"},
        "messages": [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "calling"},
                    {"type": "tool_use", "id": "toolu_1", "name": "search", "input": {"q": "x"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "result"},
                    {"type": "text", "text": "thanks"},
                ],
            },
        ],
    }
    out = ac.to_chat_body(body)
    assert out["messages"][0] == {"role": "system", "content": "Be brief."}
    assert out["messages"][2]["tool_calls"][0]["function"] == {"name": "search", "arguments": '{"q":"x"}'}
    assert out["messages"][3] == {"role": "tool", "tool_call_id": "toolu_1", "content": "result"}
    assert out["messages"][4] == {"role": "user", "content": "thanks"}
    assert [t["function"]["name"] for t in out["tools"]] == ["search"]
    assert out["tool_choice"] == "required" and out["stop"] == ["END"] and out["max_tokens"] == 100


def test_request_translation_rejects_bad_input() -> None:
    for bad in (None, {}, {"messages": []}, {"messages": [{"role": "system", "content": "x"}]}):
        with pytest.raises(ac.TranslationError):
            ac.to_chat_body(bad)
    with pytest.raises(ac.TranslationError):
        ac.to_chat_body({"messages": [{"role": "user", "content": [{"type": "document"}]}]})


def test_image_translation() -> None:
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    out = ac.to_chat_body({"messages": [{"role": "user", "content": [img, {"type": "text", "text": "?"}]}]})
    parts = out["messages"][0]["content"]
    assert parts[0] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


async def test_messages_non_streaming(client: httpx.AsyncClient, upstreams: dict[str, Any]) -> None:
    g = await gateway_setup(client)
    r = await client.post(
        "/v1/messages",
        json={
            "model": "claude-sonnet-4-5",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "ping"}],
        },
        headers=_xkey(g["key"]),
    )
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["type"] == "message" and m["role"] == "assistant" and m["id"].startswith("msg_")
    assert m["content"][0]["type"] == "text" and "ping" in m["content"][0]["text"]
    assert m["stop_reason"] == "end_turn"
    assert m["usage"]["input_tokens"] > 0 and m["usage"]["output_tokens"] > 0
    # Unknown Claude names route as auto: the strongest connected model serves.
    assert r.headers["x-oasis-route-model"] == "mock-large"
    assert m["oasis_route"]["model"] == "mock-large"


async def test_messages_tool_use_round_trip(client: httpx.AsyncClient, upstreams: dict[str, Any]) -> None:
    g = await gateway_setup(client)
    tools = [
        {"name": "lookup", "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}}}
    ]
    r = await client.post(
        "/v1/messages",
        json={
            "model": "auto",
            "max_tokens": 64,
            "tools": tools,
            "messages": [{"role": "user", "content": "use_tool"}],
        },
        headers=_xkey(g["key"]),
    )
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["stop_reason"] == "tool_use"
    block = m["content"][0]
    assert block["type"] == "tool_use" and block["name"] == "lookup" and block["input"] == {"query": "mock"}
    # Send the tool result back, Anthropic style.
    follow = [
        {"role": "user", "content": "use_tool"},
        {"role": "assistant", "content": m["content"]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": block["id"], "content": "42"}]},
    ]
    r = await client.post(
        "/v1/messages",
        json={"model": "auto", "max_tokens": 64, "tools": tools, "messages": follow},
        headers=_xkey(g["key"]),
    )
    assert r.status_code == 200, r.text
    assert r.json()["stop_reason"] == "end_turn"
    host = {"mock": "mock-a.test", "mock_alt": "mock-b.test"}[r.headers["x-oasis-route-provider"]]
    sent = upstreams[host].calls[-1]["body"]["messages"]
    assert sent[-1] == {"role": "tool", "tool_call_id": block["id"], "content": "42"}


async def test_messages_streaming(client: httpx.AsyncClient, upstreams: dict[str, Any]) -> None:
    g = await gateway_setup(client)
    r = await client.post(
        "/v1/messages",
        json={
            "model": "auto",
            "max_tokens": 64,
            "stream": True,
            "messages": [{"role": "user", "content": "hello there"}],
        },
        headers=_xkey(g["key"]),
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    ev = _events(r.text)
    names = [e for e, _ in ev]
    assert names[0] == "message_start" and names[1] == "content_block_start"
    assert names[-3:] == ["content_block_stop", "message_delta", "message_stop"]
    text = "".join(d["delta"]["text"] for e, d in ev if e == "content_block_delta")
    assert "hello there" in text
    delta = next(d for e, d in ev if e == "message_delta")
    assert delta["delta"]["stop_reason"] == "end_turn" and delta["usage"]["output_tokens"] > 0


async def test_messages_streaming_tool_use(client: httpx.AsyncClient, upstreams: dict[str, Any]) -> None:
    g = await gateway_setup(client)
    tools = [{"name": "lookup", "input_schema": {"type": "object"}}]
    r = await client.post(
        "/v1/messages",
        json={
            "max_tokens": 64,
            "stream": True,
            "tools": tools,
            "messages": [{"role": "user", "content": "use_tool"}],
        },
        headers=_xkey(g["key"]),
    )
    ev = _events(r.text)
    start = next(d for e, d in ev if e == "content_block_start")
    assert start["content_block"]["type"] == "tool_use" and start["content_block"]["name"] == "lookup"
    partial = "".join(d["delta"]["partial_json"] for e, d in ev if e == "content_block_delta")
    assert json.loads(partial) == {"query": "mock"}
    assert next(d for e, d in ev if e == "message_delta")["delta"]["stop_reason"] == "tool_use"


async def test_messages_errors_use_anthropic_shape(
    client: httpx.AsyncClient, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client)
    r = await client.post("/v1/messages", json={"messages": []}, headers=_xkey(g["key"]))
    assert r.status_code == 400
    assert r.json()["type"] == "error" and r.json()["error"]["type"] == "invalid_request_error"
    r = await client.post(
        "/v1/messages", json={"messages": [{"role": "user", "content": "x"}]}, headers=_xkey("bad")
    )
    assert r.status_code == 401
    # A non-Claude unknown model is not silently remapped.
    r = await client.post(
        "/v1/messages",
        json={"model": "no-such-model", "max_tokens": 8, "messages": [{"role": "user", "content": "x"}]},
        headers=_xkey(g["key"]),
    )
    assert r.status_code == 404 and r.json()["error"]["type"] == "not_found_error"


async def test_count_tokens(client: httpx.AsyncClient, upstreams: dict[str, Any]) -> None:
    g = await gateway_setup(client)
    r = await client.post(
        "/v1/messages/count_tokens",
        json={"model": "claude-x", "messages": [{"role": "user", "content": "hello " * 100}]},
        headers=_xkey(g["key"]),
    )
    assert r.status_code == 200, r.text
    assert r.json()["input_tokens"] > 50


async def test_auto_task_alias(client: httpx.AsyncClient, upstreams: dict[str, Any]) -> None:
    g = await gateway_setup(client)
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "auto/coding", "messages": [{"role": "user", "content": "x"}]},
        headers={"authorization": f"Bearer {g['key']}"},
    )
    assert r.status_code == 200, r.text
    ids = [m["id"] for m in (await client.get("/v1/models", headers=_xkey(g["key"]))).json()["data"]]
    assert "auto/coding" in ids and "auto/reasoning" in ids
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "auto/nonsense", "messages": [{"role": "user", "content": "x"}]},
        headers={"authorization": f"Bearer {g['key']}"},
    )
    assert r.status_code == 400
