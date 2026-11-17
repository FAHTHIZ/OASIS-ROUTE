"""OpenAI Responses API compatibility: /v1/responses routes through the same engine as chat completions."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from oasis_route.gateway import responses_compat as rc
from tests.helpers import auth, gateway_setup


def _events(raw: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for frame in raw.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_request_translation() -> None:
    body = {
        "model": "auto",
        "instructions": "Be brief.",
        "max_output_tokens": 100,
        "tools": [
            {"type": "function", "name": "search", "description": "Search", "parameters": {"type": "object"}},
            {"type": "web_search"},  # hosted tool: not routable, dropped
        ],
        "tool_choice": "required",
        "text": {
            "format": {"type": "json_schema", "name": "out", "schema": {"type": "object"}, "strict": True}
        },
        "input": [
            {"role": "developer", "content": "Answer in English."},
            {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "reasoning", "id": "rs_1", "summary": []},  # ignored
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "calling"}]},
            {"type": "function_call", "call_id": "call_1", "name": "search", "arguments": '{"q":"x"}'},
            {"type": "function_call_output", "call_id": "call_1", "output": "result"},
        ],
    }
    out = rc.to_chat_body(body)
    m = out["messages"]
    assert m[0] == {"role": "system", "content": "Be brief."}
    assert m[1] == {"role": "system", "content": "Answer in English."}
    assert m[2] == {"role": "user", "content": "hi"}
    # The function call joins the preceding assistant message, as chat completions expects.
    assert m[3]["role"] == "assistant" and m[3]["content"] == "calling"
    assert m[3]["tool_calls"][0] == {
        "id": "call_1",
        "type": "function",
        "function": {"name": "search", "arguments": '{"q":"x"}'},
    }
    assert m[4] == {"role": "tool", "tool_call_id": "call_1", "content": "result"}
    assert [t["function"]["name"] for t in out["tools"]] == ["search"]
    assert out["tool_choice"] == "required" and out["max_tokens"] == 100
    assert out["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "out", "schema": {"type": "object"}, "strict": True},
    }


def test_string_input_and_images() -> None:
    assert rc.to_chat_body({"input": "hello"})["messages"] == [{"role": "user", "content": "hello"}]
    out = rc.to_chat_body(
        {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "what is this?"},
                        {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "low"},
                    ],
                }
            ]
        }
    )
    parts = out["messages"][0]["content"]
    assert parts[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,AAAA", "detail": "low"},
    }


def test_request_translation_rejects_bad_input() -> None:
    bad_bodies: list[Any] = [
        None,
        {},
        {"input": []},
        {"input": [{"role": "system", "content": "x"}]},
        {"input": "x", "previous_response_id": "resp_1"},
        {"input": [{"type": "item_reference", "id": "x"}]},
        {"input": [{"role": "user", "content": [{"type": "input_file", "file_id": "f"}]}]},
    ]
    for bad in bad_bodies:
        with pytest.raises(rc.TranslationError):
            rc.to_chat_body(bad)


def test_response_translation_marks_truncation_incomplete() -> None:
    payload = {
        "model": "m",
        "choices": [{"message": {"content": "abc"}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }
    r = rc.to_response(payload, {"input": "x"})
    assert r["status"] == "incomplete" and r["incomplete_details"] == {"reason": "max_output_tokens"}
    assert r["usage"]["total_tokens"] == 5 and r["output_text"] == "abc"


async def test_responses_non_streaming(client: httpx.AsyncClient, upstreams: dict[str, Any]) -> None:
    g = await gateway_setup(client)
    r = await client.post(
        "/v1/responses",
        json={"model": "auto", "input": "ping", "max_output_tokens": 64},
        headers=auth(g["key"]),
    )
    assert r.status_code == 200, r.text
    resp = r.json()
    assert resp["object"] == "response" and resp["id"].startswith("resp_") and resp["status"] == "completed"
    item = resp["output"][0]
    assert item["type"] == "message" and item["role"] == "assistant"
    assert item["content"][0]["type"] == "output_text" and "ping" in item["content"][0]["text"]
    assert "ping" in resp["output_text"]
    assert resp["usage"]["input_tokens"] > 0 and resp["usage"]["output_tokens"] > 0
    assert r.headers["x-oasis-route-model"] == "mock-large" and resp["oasis_route"]["model"] == "mock-large"


async def test_responses_function_call_round_trip(
    client: httpx.AsyncClient, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client)
    tools = [
        {
            "type": "function",
            "name": "lookup",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        }
    ]
    r = await client.post(
        "/v1/responses", json={"model": "auto", "tools": tools, "input": "use_tool"}, headers=auth(g["key"])
    )
    assert r.status_code == 200, r.text
    call = r.json()["output"][0]
    assert call["type"] == "function_call" and call["name"] == "lookup"
    assert json.loads(call["arguments"]) == {"query": "mock"}
    # Codex-style follow-up: stateless, the whole conversation is re-sent with the tool output.
    follow = [
        {"role": "user", "content": "use_tool"},
        call,
        {"type": "function_call_output", "call_id": call["call_id"], "output": "42"},
    ]
    r = await client.post(
        "/v1/responses", json={"model": "auto", "tools": tools, "input": follow}, headers=auth(g["key"])
    )
    assert r.status_code == 200, r.text
    assert r.json()["output"][0]["type"] == "message"
    host = {"mock": "mock-a.test", "mock_alt": "mock-b.test"}[r.headers["x-oasis-route-provider"]]
    sent = upstreams[host].calls[-1]["body"]["messages"]
    assert sent[-2]["tool_calls"][0]["id"] == call["call_id"]
    assert sent[-1] == {"role": "tool", "tool_call_id": call["call_id"], "content": "42"}


async def test_responses_streaming(client: httpx.AsyncClient, upstreams: dict[str, Any]) -> None:
    g = await gateway_setup(client)
    r = await client.post(
        "/v1/responses", json={"input": "hello there", "stream": True}, headers=auth(g["key"])
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    ev = _events(r.text)
    names = [e for e, _ in ev]
    assert names[:4] == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
    ]
    assert names[-4:] == [
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert [d["sequence_number"] for _, d in ev] == list(range(len(ev)))
    text = "".join(d["delta"] for e, d in ev if e == "response.output_text.delta")
    assert "hello there" in text
    done = ev[-1][1]["response"]
    assert done["status"] == "completed" and done["output"][0]["content"][0]["text"] == text
    assert done["usage"]["output_tokens"] > 0


async def test_responses_streaming_function_call(
    client: httpx.AsyncClient, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client)
    tools = [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}]
    r = await client.post(
        "/v1/responses", json={"stream": True, "tools": tools, "input": "use_tool"}, headers=auth(g["key"])
    )
    ev = _events(r.text)
    added = next(d for e, d in ev if e == "response.output_item.added")
    assert added["item"]["type"] == "function_call" and added["item"]["name"] == "lookup"
    partial = "".join(d["delta"] for e, d in ev if e == "response.function_call_arguments.delta")
    assert json.loads(partial) == {"query": "mock"}
    done_args = next(d for e, d in ev if e == "response.function_call_arguments.done")["arguments"]
    assert json.loads(done_args) == {"query": "mock"}
    final = ev[-1][1]["response"]["output"][0]
    assert final["type"] == "function_call" and final["status"] == "completed"


async def test_responses_errors(client: httpx.AsyncClient, upstreams: dict[str, Any]) -> None:
    g = await gateway_setup(client)
    r = await client.post(
        "/v1/responses", json={"input": "x", "previous_response_id": "resp_1"}, headers=auth(g["key"])
    )
    assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request_error"
    assert "previous_response_id" in r.json()["error"]["message"]
    r = await client.post("/v1/responses", json={"input": "x"}, headers=auth("bad"))
    assert r.status_code == 401
    r = await client.post(
        "/v1/responses", json={"model": "no-such-model", "input": "x"}, headers=auth(g["key"])
    )
    assert r.status_code == 404
