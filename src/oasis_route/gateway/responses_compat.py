"""OpenAI Responses API compatibility: ``POST /v1/responses`` on top of the chat-completions engine.

Lets clients that speak the Responses API (the OpenAI SDKs' ``client.responses.create`` and tools such as
Codex CLI with ``wire_api = "responses"``) use every routed provider. Requests are translated to the
internal Chat Completions shape, routed exactly like ``/v1/chat/completions`` (same quota, health,
fallback and audit), and responses or stream chunks are translated back.

The gateway is stateless: ``previous_response_id`` and stored responses are not supported, so clients must
send the whole conversation in ``input`` (Codex does this with ``store = false``). Only function tools are
routable; hosted tools (web search, file search, code interpreter, computer use) are dropped because no
other provider can execute them. Reasoning items sent back as input are ignored.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import AsyncIterator
from typing import Any


class TranslationError(ValueError):
    """The Responses request cannot be expressed as a chat completion."""


def response_id() -> str:
    return "resp_" + secrets.token_hex(24)


def _item_id(prefix: str) -> str:
    return f"{prefix}_" + secrets.token_hex(24)


_TEXT_PARTS = {"input_text", "output_text", "text", "refusal"}


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, dict) and p.get("type") in _TEXT_PARTS:
                out.append(str(p.get("text") or p.get("refusal") or ""))
        return "".join(out)
    return ""


def _convert_parts(content: Any) -> Any:
    """Responses content parts -> chat content (plain string when there are no images)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise TranslationError("message content must be a string or an array of parts")
    parts: list[dict[str, Any]] = []
    has_image = False
    for p in content:
        if not isinstance(p, dict):
            raise TranslationError("content parts must be objects")
        kind = p.get("type")
        if kind in _TEXT_PARTS:
            parts.append({"type": "text", "text": str(p.get("text") or p.get("refusal") or "")})
        elif kind == "input_image":
            url = p.get("image_url")
            if not url:
                raise TranslationError("input_image requires image_url (file_id is not supported)")
            image: dict[str, Any] = {"url": str(url)}
            if p.get("detail") in ("low", "high", "auto"):
                image["detail"] = p["detail"]
            parts.append({"type": "image_url", "image_url": image})
            has_image = True
        else:
            raise TranslationError(f"content part type {kind!r} is not supported")
    if not has_image:
        return "".join(p["text"] for p in parts)
    return parts


def _convert_input(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        return [{"role": "user", "content": raw}]
    if not isinstance(raw, list) or not raw:
        raise TranslationError("'input' must be a string or a non-empty array")
    messages: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise TranslationError("input items must be objects")
        kind = item.get("type") or ("message" if "role" in item else None)
        if kind == "message":
            role = str(item.get("role") or "user")
            if role == "developer":
                role = "system"
            if role not in ("user", "assistant", "system"):
                raise TranslationError(f"message role {role!r} is not supported")
            content = _convert_parts(item.get("content", ""))
            if role != "user" and not isinstance(content, str):
                content = _text_of(item.get("content"))
            messages.append({"role": role, "content": content})
        elif kind == "function_call":
            call = {
                "id": str(item.get("call_id") or item.get("id") or "call_" + secrets.token_hex(8)),
                "type": "function",
                "function": {
                    "name": str(item.get("name", "")),
                    "arguments": str(item.get("arguments") or "{}"),
                },
            }
            last = messages[-1] if messages else None
            if last is not None and last["role"] == "assistant":
                last.setdefault("tool_calls", []).append(call)
                if not last.get("content"):
                    last["content"] = None
            else:
                messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
        elif kind == "function_call_output":
            output = item.get("output")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(item.get("call_id", "")),
                    "content": output if isinstance(output, str) else _text_of(output) or json.dumps(output),
                }
            )
        elif kind == "reasoning":
            continue  # our own earlier output; providers cannot consume another model's reasoning
        else:
            raise TranslationError(f"input item type {kind!r} is not supported")
    return messages


def _convert_tool_choice(choice: Any) -> Any:
    if choice in ("auto", "none", "required"):
        return choice
    if isinstance(choice, dict) and choice.get("type") == "function" and choice.get("name"):
        return {"type": "function", "function": {"name": str(choice["name"])}}
    return None


def _convert_format(text: Any) -> dict[str, Any] | None:
    fmt = text.get("format") if isinstance(text, dict) else None
    if not isinstance(fmt, dict):
        return None
    if fmt.get("type") == "json_object":
        return {"type": "json_object"}
    if fmt.get("type") == "json_schema":
        schema: dict[str, Any] = {
            "name": str(fmt.get("name") or "response"),
            "schema": fmt.get("schema") or {},
        }
        if fmt.get("strict") is not None:
            schema["strict"] = bool(fmt["strict"])
        return {"type": "json_schema", "json_schema": schema}
    return None


def to_chat_body(body: Any) -> dict[str, Any]:
    """Translate a Responses request body into an OpenAI Chat Completions body."""
    if not isinstance(body, dict):
        raise TranslationError("request body must be a JSON object")
    if body.get("previous_response_id"):
        raise TranslationError(
            "previous_response_id is not supported: Oasis Route does not store responses; "
            "send the full conversation in 'input'"
        )
    if body.get("background"):
        raise TranslationError("background responses are not supported")
    messages = _convert_input(body.get("input"))
    if body.get("instructions"):
        messages.insert(0, {"role": "system", "content": str(body["instructions"])})
    if not any(m["role"] != "system" for m in messages):
        raise TranslationError("'input' contains no translatable content")

    out: dict[str, Any] = {"model": str(body.get("model") or "auto"), "messages": messages}
    if body.get("max_output_tokens") is not None:
        out["max_tokens"] = body["max_output_tokens"]
    for key in ("temperature", "top_p", "user"):
        if body.get(key) is not None:
            out[key] = body[key]
    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        converted = []
        for t in tools:
            if not isinstance(t, dict) or t.get("type") != "function" or not t.get("name"):
                continue  # hosted tools (web_search, file_search, ...) cannot run on other providers
            fn: dict[str, Any] = {
                "name": str(t["name"]),
                "parameters": t.get("parameters") or {"type": "object", "properties": {}},
            }
            if t.get("description"):
                fn["description"] = str(t["description"])
            if t.get("strict") is not None:
                fn["strict"] = bool(t["strict"])
            converted.append({"type": "function", "function": fn})
        if converted:
            out["tools"] = converted
            tc = _convert_tool_choice(body.get("tool_choice"))
            if tc is not None:
                out["tool_choice"] = tc
    rf = _convert_format(body.get("text"))
    if rf is not None:
        out["response_format"] = rf
    if body.get("stream"):
        out["stream"] = True
        out["stream_options"] = {"include_usage": True}
    if isinstance(body.get("oasis_route"), dict):
        out["oasis_route"] = body["oasis_route"]
    return out


def _usage(u: Any) -> dict[str, Any]:
    u = u if isinstance(u, dict) else {}
    inp = int(u.get("prompt_tokens") or 0)
    outp = int(u.get("completion_tokens") or 0)
    cached = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
    reasoning = int(((u.get("completion_tokens_details") or {}).get("reasoning_tokens")) or 0)
    return {
        "input_tokens": inp,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens": outp,
        "output_tokens_details": {"reasoning_tokens": reasoning},
        "total_tokens": inp + outp,
    }


def _envelope(rid: str, created: int, model: str, body: dict[str, Any], status: str) -> dict[str, Any]:
    return {
        "id": rid,
        "object": "response",
        "created_at": created,
        "status": status,
        "error": None,
        "incomplete_details": None,
        "instructions": body.get("instructions"),
        "max_output_tokens": body.get("max_output_tokens"),
        "model": model,
        "output": [],
        "parallel_tool_calls": bool(body.get("parallel_tool_calls", True)),
        "previous_response_id": None,
        "store": False,
        "temperature": body.get("temperature"),
        "text": body.get("text") if isinstance(body.get("text"), dict) else {"format": {"type": "text"}},
        "tool_choice": body.get("tool_choice", "auto"),
        "tools": body.get("tools") or [],
        "top_p": body.get("top_p"),
        "truncation": "disabled",
        "usage": None,
        "user": body.get("user"),
        "metadata": body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
    }


def _message_item(item_id: str, text: str, status: str = "completed") -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "message",
        "status": status,
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _finish(resp: dict[str, Any], finish_reason: Any) -> None:
    if finish_reason == "length":
        resp["status"] = "incomplete"
        resp["incomplete_details"] = {"reason": "max_output_tokens"}
    elif finish_reason == "content_filter":
        resp["status"] = "incomplete"
        resp["incomplete_details"] = {"reason": "content_filter"}
    else:
        resp["status"] = "completed"


def to_response(payload: dict[str, Any], request_body: dict[str, Any]) -> dict[str, Any]:
    """Translate an OpenAI chat completion into a Responses ``response`` object."""
    choice = (payload.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    model = str(payload.get("model") or request_body.get("model") or "auto")
    resp = _envelope(
        response_id(), int(payload.get("created") or time.time()), model, request_body, "completed"
    )
    output: list[dict[str, Any]] = []
    text = msg.get("content")
    if isinstance(text, list):
        text = _text_of(text)
    if text:
        output.append(_message_item(_item_id("msg"), str(text)))
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        args = fn.get("arguments")
        output.append(
            {
                "id": _item_id("fc"),
                "type": "function_call",
                "status": "completed",
                "call_id": str(call.get("id") or "call_" + secrets.token_hex(8)),
                "name": str(fn.get("name", "")),
                "arguments": args if isinstance(args, str) else json.dumps(args or {}),
            }
        )
    resp["output"] = output
    resp["output_text"] = str(text or "")
    resp["usage"] = _usage(payload.get("usage"))
    _finish(resp, choice.get("finish_reason"))
    return resp


def error_body(message: str, code: str | None, type_: str = "invalid_request_error") -> dict[str, Any]:
    return {"error": {"message": message, "type": type_, "param": None, "code": code}}


class _Seq:
    def __init__(self) -> None:
        self.n = 0

    def event(self, name: str, data: dict[str, Any]) -> bytes:
        payload = {"type": name, **data, "sequence_number": self.n}
        self.n += 1
        return f"event: {name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()


async def translate_stream(
    source: AsyncIterator[bytes], request_body: dict[str, Any], input_tokens_estimate: int
) -> AsyncIterator[bytes]:
    """Turn the engine's OpenAI SSE relay into Responses streaming events."""
    seq = _Seq()
    model = str(request_body.get("model") or "auto")
    resp = _envelope(response_id(), int(time.time()), model, request_body, "in_progress")
    started = False
    output: list[dict[str, Any]] = []
    text_item: dict[str, Any] | None = None  # open message item
    text_index = -1
    text_buf: list[str] = []
    tools: dict[int, dict[str, Any]] = {}  # openai tool index -> {"item", "output_index", "args"}
    finish_reason: Any = None
    usage: dict[str, Any] = {"prompt_tokens": input_tokens_estimate, "completion_tokens": 0}
    buf = b""

    def start_events() -> list[bytes]:
        nonlocal started
        started = True
        resp["model"] = model
        return [
            seq.event("response.created", {"response": dict(resp)}),
            seq.event("response.in_progress", {"response": dict(resp)}),
        ]

    def close_text() -> list[bytes]:
        nonlocal text_item
        if text_item is None:
            return []
        full = "".join(text_buf)
        done = _message_item(text_item["id"], full)
        output[text_index] = done
        ids = {"item_id": text_item["id"], "output_index": text_index, "content_index": 0}
        text_item = None
        return [
            seq.event("response.output_text.done", {**ids, "text": full, "logprobs": []}),
            seq.event(
                "response.content_part.done",
                {**ids, "part": {"type": "output_text", "text": full, "annotations": []}},
            ),
            seq.event("response.output_item.done", {"output_index": ids["output_index"], "item": done}),
        ]

    def close_tools() -> list[bytes]:
        events = []
        for t in tools.values():
            if t.get("closed"):
                continue
            t["closed"] = True
            args = "".join(t["args"])
            item = {**t["item"], "arguments": args, "status": "completed"}
            output[t["output_index"]] = item
            events.append(
                seq.event(
                    "response.function_call_arguments.done",
                    {"item_id": item["id"], "output_index": t["output_index"], "arguments": args},
                )
            )
            events.append(
                seq.event("response.output_item.done", {"output_index": t["output_index"], "item": item})
            )
        return events

    async for raw in source:
        buf += raw
        while b"\n\n" in buf:
            frame, buf = buf.split(b"\n\n", 1)
            line = frame.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data_s = line[5:].strip()
            if data_s == "[DONE]":
                continue
            try:
                data = json.loads(data_s)
            except ValueError:
                continue
            if "error" in data and not data.get("choices"):
                err = data.get("error") or {}
                if not started:
                    for e in start_events():
                        yield e
                resp["status"] = "failed"
                resp["error"] = {
                    "code": str(err.get("code") or "server_error"),
                    "message": str(err.get("message", "upstream error")),
                }
                resp["output"] = [o for o in output if o]
                yield seq.event("response.failed", {"response": resp})
                return
            if data.get("model"):
                model = str(data["model"])
            if not started:
                for e in start_events():
                    yield e
            if isinstance(data.get("usage"), dict):
                usage = {**usage, **{k: v for k, v in data["usage"].items() if v}}
            for choice in data.get("choices") or []:
                delta = choice.get("delta") or {}
                text = delta.get("content")
                if text:
                    if text_item is None:
                        text_item = {"id": _item_id("msg")}
                        text_index = len(output)
                        output.append({})
                        text_buf = []
                        yield seq.event(
                            "response.output_item.added",
                            {
                                "output_index": text_index,
                                "item": {
                                    "id": text_item["id"],
                                    "type": "message",
                                    "status": "in_progress",
                                    "role": "assistant",
                                    "content": [],
                                },
                            },
                        )
                        yield seq.event(
                            "response.content_part.added",
                            {
                                "item_id": text_item["id"],
                                "output_index": text_index,
                                "content_index": 0,
                                "part": {"type": "output_text", "text": "", "annotations": []},
                            },
                        )
                    text_buf.append(str(text))
                    yield seq.event(
                        "response.output_text.delta",
                        {
                            "item_id": text_item["id"],
                            "output_index": text_index,
                            "content_index": 0,
                            "delta": str(text),
                            "logprobs": [],
                        },
                    )
                for call in delta.get("tool_calls") or []:
                    oi = int(call.get("index", 0))
                    fn = call.get("function") or {}
                    if oi not in tools:
                        for e in close_text():
                            yield e
                        item = {
                            "id": _item_id("fc"),
                            "type": "function_call",
                            "status": "in_progress",
                            "call_id": str(call.get("id") or "call_" + secrets.token_hex(8)),
                            "name": str(fn.get("name", "")),
                            "arguments": "",
                        }
                        tools[oi] = {"item": item, "output_index": len(output), "args": []}
                        output.append({})
                        yield seq.event(
                            "response.output_item.added",
                            {"output_index": tools[oi]["output_index"], "item": item},
                        )
                    args = fn.get("arguments")
                    if args:
                        tools[oi]["args"].append(str(args))
                        yield seq.event(
                            "response.function_call_arguments.delta",
                            {
                                "item_id": tools[oi]["item"]["id"],
                                "output_index": tools[oi]["output_index"],
                                "delta": str(args),
                            },
                        )
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]

    if not started:
        for e in start_events():
            yield e
    for e in close_text():
        yield e
    for e in close_tools():
        yield e
    resp["output"] = [o for o in output if o]
    resp["output_text"] = "".join(
        p.get("text", "") for o in resp["output"] if o.get("type") == "message" for p in o["content"]
    )
    resp["usage"] = _usage(usage)
    _finish(resp, finish_reason)
    name = "response.incomplete" if resp["status"] == "incomplete" else "response.completed"
    yield seq.event(name, {"response": resp})
