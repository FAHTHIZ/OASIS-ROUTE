"""Anthropic Messages API compatibility: ``POST /v1/messages`` on top of the OpenAI-shaped engine.

Lets Anthropic SDKs and tools that speak the Messages API (for example Claude Code with
``ANTHROPIC_BASE_URL`` pointed at Oasis Route) use every routed provider. Requests are translated to the
internal OpenAI Chat Completions shape, routed exactly like ``/v1/chat/completions`` (same quota, health,
fallback and audit), and responses or stream chunks are translated back.

Only documented, stable parts of the Messages API are translated: text and image content, tool use and tool
results, system prompts, stop sequences, sampling parameters and streaming events. Extended-thinking
blocks are not produced (thinking requests are accepted and ignored).
"""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator
from typing import Any

STOP_REASONS = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}

ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    500: "api_error",
    502: "api_error",
    503: "overloaded_error",
    504: "api_error",
}

# Default when a client omits max_tokens (the Anthropic API requires it, but be lenient).
DEFAULT_MAX_TOKENS = 4096


class TranslationError(ValueError):
    """The Messages request cannot be expressed as a chat completion."""


def message_id() -> str:
    return "msg_" + secrets.token_hex(12)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _image_part(block: dict[str, Any]) -> dict[str, Any] | None:
    src = block.get("source") or {}
    if src.get("type") == "base64" and src.get("data"):
        media = src.get("media_type") or "image/png"
        return {"type": "image_url", "image_url": {"url": f"data:{media};base64,{src['data']}"}}
    if src.get("type") == "url" and src.get("url"):
        return {"type": "image_url", "image_url": {"url": src["url"]}}
    return None


def _tool_result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    text = _text_of(content) if not isinstance(content, str) else content
    if block.get("is_error"):
        return f"[tool error] {text}"
    return text


def _convert_message(msg: dict[str, Any]) -> list[dict[str, Any]]:
    role = msg.get("role")
    if role not in ("user", "assistant"):
        raise TranslationError(f"unsupported message role: {role!r}")
    content = msg.get("content")
    if isinstance(content, str):
        return [{"role": role, "content": content}]
    if not isinstance(content, list):
        raise TranslationError("message content must be a string or an array of content blocks")

    out: list[dict[str, Any]] = []
    parts: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            raise TranslationError("content blocks must be objects")
        kind = block.get("type")
        if kind == "text":
            parts.append({"type": "text", "text": str(block.get("text", ""))})
        elif kind == "image":
            img = _image_part(block)
            if img is None:
                raise TranslationError("unsupported image source")
            parts.append(img)
        elif kind == "tool_use" and role == "assistant":
            tool_calls.append(
                {
                    "id": str(block.get("id") or "toolu_" + secrets.token_hex(8)),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name", "")),
                        "arguments": json.dumps(block.get("input") or {}, separators=(",", ":")),
                    },
                }
            )
        elif kind == "tool_result" and role == "user":
            # OpenAI requires tool results as separate role=tool messages, before any further user text.
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": str(block.get("tool_use_id", "")),
                    "content": _tool_result_text(block),
                }
            )
        elif kind in ("thinking", "redacted_thinking"):
            continue  # prior reasoning is not replayable on other providers
        else:
            raise TranslationError(f"unsupported content block type: {kind!r}")

    if role == "assistant":
        text = "".join(p["text"] for p in parts if p["type"] == "text")
        m: dict[str, Any] = {"role": "assistant", "content": text or None}
        if tool_calls:
            m["tool_calls"] = tool_calls
        if text or tool_calls:
            out.append(m)
        return out
    if parts:
        only_text = all(p["type"] == "text" for p in parts)
        out.append({"role": "user", "content": "".join(p["text"] for p in parts) if only_text else parts})
    return out


def _convert_tool_choice(choice: Any) -> Any:
    if not isinstance(choice, dict):
        return None
    kind = choice.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool" and choice.get("name"):
        return {"type": "function", "function": {"name": str(choice["name"])}}
    return None


def to_chat_body(body: Any) -> dict[str, Any]:
    """Translate an Anthropic Messages request body into an OpenAI Chat Completions body."""
    if not isinstance(body, dict):
        raise TranslationError("request body must be a JSON object")
    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise TranslationError("'messages' must be a non-empty array")
    messages: list[dict[str, Any]] = []
    system = body.get("system")
    if system:
        text = _text_of(system)
        if text:
            messages.append({"role": "system", "content": text})
    for msg in raw_messages:
        if not isinstance(msg, dict):
            raise TranslationError("messages must be objects")
        messages.extend(_convert_message(msg))
    if not any(m["role"] != "system" for m in messages):
        raise TranslationError("'messages' contains no translatable content")

    out: dict[str, Any] = {
        "model": str(body.get("model") or "auto"),
        "messages": messages,
        "max_tokens": body.get("max_tokens") or DEFAULT_MAX_TOKENS,
    }
    for key in ("temperature", "top_p"):
        if body.get(key) is not None:
            out[key] = body[key]
    if body.get("stop_sequences"):
        out["stop"] = list(body["stop_sequences"])
    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        converted = []
        for t in tools:
            if not isinstance(t, dict) or not t.get("name"):
                continue
            if t.get("type") not in (None, "custom"):
                continue  # server-side Anthropic tools (web search, computer use) cannot be routed elsewhere
            fn: dict[str, Any] = {
                "name": str(t["name"]),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            }
            if t.get("description"):
                fn["description"] = str(t["description"])
            converted.append({"type": "function", "function": fn})
        if converted:
            out["tools"] = converted
            tc = _convert_tool_choice(body.get("tool_choice"))
            if tc is not None:
                out["tool_choice"] = tc
    if isinstance(body.get("metadata"), dict) and body["metadata"].get("user_id"):
        out["user"] = str(body["metadata"]["user_id"])
    if body.get("stream"):
        out["stream"] = True
        out["stream_options"] = {"include_usage": True}
    if isinstance(body.get("oasis_route"), dict):
        out["oasis_route"] = body["oasis_route"]
    return out


def _usage(u: Any) -> dict[str, int]:
    u = u if isinstance(u, dict) else {}
    return {
        "input_tokens": int(u.get("prompt_tokens") or 0),
        "output_tokens": int(u.get("completion_tokens") or 0),
    }


def _tool_input(arguments: Any) -> Any:
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments or "{}")
    except (TypeError, ValueError):
        return {"_raw": arguments}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def to_message(payload: dict[str, Any], requested_model: str) -> dict[str, Any]:
    """Translate an OpenAI chat completion into an Anthropic ``message`` object."""
    choice = (payload.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content: list[dict[str, Any]] = []
    text = msg.get("content")
    if isinstance(text, list):
        text = _text_of(text)
    if text:
        content.append({"type": "text", "text": str(text)})
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        content.append(
            {
                "type": "tool_use",
                "id": str(call.get("id") or "toolu_" + secrets.token_hex(8)),
                "name": str(fn.get("name", "")),
                "input": _tool_input(fn.get("arguments")),
            }
        )
    finish = choice.get("finish_reason")
    return {
        "id": message_id(),
        "type": "message",
        "role": "assistant",
        "model": str(payload.get("model") or requested_model),
        "content": content,
        "stop_reason": STOP_REASONS.get(str(finish), "end_turn"),
        "stop_sequence": None,
        "usage": _usage(payload.get("usage")),
    }


def error_body(status: int, message: str, code: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": "error",
        "error": {"type": ERROR_TYPES.get(status, "api_error"), "message": message},
    }
    if code:
        body["error"]["code"] = code
    return body


def _event(name: str, data: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


async def translate_stream(
    source: AsyncIterator[bytes], requested_model: str, input_tokens_estimate: int
) -> AsyncIterator[bytes]:
    """Turn the engine's OpenAI SSE relay into Anthropic Messages streaming events."""
    started = False
    block_open: str | None = None  # "text" or "tool:<openai index>"
    index = -1
    tool_blocks: dict[int, int] = {}
    stop_reason = "end_turn"
    usage = {"input_tokens": input_tokens_estimate, "output_tokens": 0}
    model = requested_model
    buf = b""

    def start(data_model: str | None) -> bytes:
        nonlocal started
        started = True
        return _event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message_id(),
                    "type": "message",
                    "role": "assistant",
                    "model": data_model or model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": usage["input_tokens"], "output_tokens": 0},
                },
            },
        )

    def close_block() -> bytes:
        nonlocal block_open
        block_open = None
        return _event("content_block_stop", {"type": "content_block_stop", "index": index})

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
                if block_open is not None:
                    yield close_block()
                yield _event(
                    "error",
                    {
                        "type": "error",
                        "error": {"type": "api_error", "message": str(err.get("message", "upstream error"))},
                    },
                )
                return
            if data.get("model"):
                model = str(data["model"])
            if not started:
                yield start(data.get("model"))
            if isinstance(data.get("usage"), dict):
                u = _usage(data["usage"])
                usage["output_tokens"] = u["output_tokens"] or usage["output_tokens"]
                usage["input_tokens"] = u["input_tokens"] or usage["input_tokens"]
            for choice in data.get("choices") or []:
                delta = choice.get("delta") or {}
                text = delta.get("content")
                if text:
                    if block_open != "text":
                        if block_open is not None:
                            yield close_block()
                        index += 1
                        block_open = "text"
                        yield _event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": index,
                                "content_block": {"type": "text", "text": ""},
                            },
                        )
                    yield _event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {"type": "text_delta", "text": str(text)},
                        },
                    )
                for call in delta.get("tool_calls") or []:
                    oi = int(call.get("index", 0))
                    fn = call.get("function") or {}
                    if oi not in tool_blocks:
                        if block_open is not None:
                            yield close_block()
                        index += 1
                        tool_blocks[oi] = index
                        block_open = f"tool:{oi}"
                        yield _event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": index,
                                "content_block": {
                                    "type": "tool_use",
                                    "id": str(call.get("id") or "toolu_" + secrets.token_hex(8)),
                                    "name": str(fn.get("name", "")),
                                    "input": {},
                                },
                            },
                        )
                    args = fn.get("arguments")
                    if args:
                        yield _event(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": tool_blocks[oi],
                                "delta": {"type": "input_json_delta", "partial_json": str(args)},
                            },
                        )
                if choice.get("finish_reason"):
                    stop_reason = STOP_REASONS.get(str(choice["finish_reason"]), "end_turn")

    if not started:
        yield start(None)
    if block_open is not None:
        yield close_block()
    yield _event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": usage["output_tokens"]},
        },
    )
    yield _event("message_stop", {"type": "message_stop"})
