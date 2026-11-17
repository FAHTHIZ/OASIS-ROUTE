"""Deterministic OpenAI-compatible mock provider for local development, demos, and tests.

Run standalone: ``oasis-route mock-provider --port 9100`` and connect it as provider ``mock`` with base URL
``http://127.0.0.1:9100/v1`` (requires ``OASIS_ROUTE_ALLOW_PRIVATE_UPSTREAMS=true`` because it is local).

Behaviour:
- Accepts any bearer key starting with ``mock-`` (``mock-invalid`` is rejected with 401).
- Replies ``"[<model>] echo: <last user text>"``; usage is computed deterministically (chars/4).
- Failures can be queued per model via ``POST /_control/fail`` or :meth:`MockProviderState.fail_next`.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

MODELS = [
    {"id": "mock-large", "context_length": 128_000},
    {"id": "mock-medium", "context_length": 32_000},
    {"id": "mock-small", "context_length": 8_000},
]


@dataclass
class Failure:
    status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class MockProviderState:
    failures: dict[str, deque[Failure]] = field(default_factory=lambda: defaultdict(deque))
    calls: list[dict[str, Any]] = field(default_factory=list)
    remaining_requests: int = 1000
    delay_s: float = 0.0
    omit_usage: bool = False
    stream_chunk_delay_s: float = 0.0
    stream_fail_after_chunks: int | None = None  # emit an in-stream error event after N content chunks
    revoked: bool = False  # simulate the upstream revoking/expiring every key
    hidden_models: set[str] = field(default_factory=set)  # simulate upstream model removal
    # Models listed upstream but absent from the curated catalog (exercises discovered-model sync).
    extra_models: list[dict[str, Any]] = field(default_factory=list)

    def listed(self) -> list[dict[str, Any]]:
        return [m for m in (*MODELS, *self.extra_models) if m["id"] not in self.hidden_models]

    def fail_next(
        self,
        model: str,
        status: int,
        *,
        code: str = "",
        message: str = "mock failure",
        retry_after: float | None = None,
        times: int = 1,
    ) -> None:
        headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
        body = {"error": {"message": message, "type": code or "mock_error", "code": code or None}}
        for _ in range(times):
            self.failures[model].append(Failure(status, body, headers))


def _usage(messages: list[dict[str, Any]], text: str) -> dict[str, int]:
    prompt = sum(len(str(m.get("content", ""))) for m in messages)
    p, c = max(1, math.ceil(prompt / 4)), max(1, math.ceil(len(text) / 4))
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _reply(model: str, messages: list[dict[str, Any]]) -> str:
    last = next((m for m in reversed(messages) if m.get("role") == "user"), {"content": ""})
    content = last.get("content")
    if isinstance(content, list):
        content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return f"[{model}] echo: {content}"


def _tool_call(body: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Deterministic tool use: when tools are offered and the last user turn says ``use_tool``."""
    tools = body.get("tools") or []
    last = next((m for m in reversed(messages) if m.get("role") in ("user", "tool")), None)
    if not tools or last is None or last.get("role") != "user" or "use_tool" not in str(last.get("content")):
        return None
    name = str(((tools[0] or {}).get("function") or {}).get("name", "tool"))
    return {
        "id": f"call_{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps({"query": "mock"})},
    }


def create_mock_provider(state: MockProviderState | None = None) -> FastAPI:
    app = FastAPI(title="Oasis Route mock provider")
    st = state or MockProviderState()
    app.state.mock = st

    def _auth(request: Request) -> JSONResponse | None:
        auth = request.headers.get("authorization", "")
        key = auth[7:] if auth.lower().startswith("bearer ") else ""
        if not key.startswith("mock-") or key == "mock-invalid" or st.revoked:
            return JSONResponse(
                {
                    "error": {
                        "message": "Invalid API key",
                        "type": "invalid_request_error",
                        "code": "invalid_api_key",
                    }
                },
                status_code=401,
            )
        return None

    def _rl_headers() -> dict[str, str]:
        st.remaining_requests = max(0, st.remaining_requests - 1)
        return {
            "x-ratelimit-limit-requests": "1000",
            "x-ratelimit-remaining-requests": str(st.remaining_requests),
            "x-ratelimit-reset-requests": "1m0s",
        }

    @app.get("/v1/models")
    async def models(request: Request) -> JSONResponse:
        if (err := _auth(request)) is not None:
            return err
        data = [{"object": "model", **m} for m in st.listed()]
        return JSONResponse({"object": "list", "data": data})

    @app.post("/_control/fail")
    async def control(body: dict[str, Any]) -> dict[str, str]:
        st.fail_next(
            str(body["model"]),
            int(body.get("status", 500)),
            code=str(body.get("code", "")),
            retry_after=body.get("retry_after"),
            times=int(body.get("times", 1)),
        )
        return {"status": "queued"}

    @app.post("/v1/chat/completions", response_model=None)
    async def chat(request: Request) -> Any:
        if (err := _auth(request)) is not None:
            return err
        body = await request.json()
        model = str(body.get("model"))
        st.calls.append({"model": model, "stream": bool(body.get("stream")), "body": body})
        if st.delay_s:
            await asyncio.sleep(st.delay_s)
        if st.failures.get(model):
            f = st.failures[model].popleft()
            return JSONResponse(f.body, status_code=f.status, headers=f.headers)
        if model not in {m["id"] for m in st.listed()}:
            return JSONResponse(
                {
                    "error": {
                        "message": f"model {model} not found",
                        "type": "invalid_request_error",
                        "code": "model_not_found",
                    }
                },
                status_code=404,
            )
        messages = body.get("messages") or []
        text = _reply(model, messages)
        usage = _usage(messages, text)
        cid, created = f"chatcmpl-{uuid.uuid4().hex[:16]}", int(time.time())
        call = _tool_call(body, messages)
        if call is not None and not body.get("stream"):
            message = {"role": "assistant", "content": None, "tool_calls": [call]}
            tool_payload: dict[str, Any] = {
                "id": cid,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
                "usage": usage,
            }
            return JSONResponse(tool_payload, headers=_rl_headers())
        if not body.get("stream"):
            payload: dict[str, Any] = {
                "id": cid,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
                ],
            }
            if not st.omit_usage:
                payload["usage"] = usage
            return JSONResponse(payload, headers=_rl_headers())

        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

        async def events() -> AsyncIterator[bytes]:
            def c(delta: dict[str, Any], finish: str | None = None) -> bytes:
                obj = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                return f"data: {json.dumps(obj)}\n\n".encode()

            yield c({"role": "assistant", "content": ""})
            if call is not None:
                fn = call["function"]
                yield c(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call["id"],
                                "type": "function",
                                "function": {"name": fn["name"], "arguments": ""},
                            }
                        ]
                    }
                )
                yield c({"tool_calls": [{"index": 0, "function": {"arguments": fn["arguments"]}}]})
                yield c({}, "tool_calls")
                if include_usage and not st.omit_usage:
                    u0 = {
                        "id": cid,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [],
                        "usage": usage,
                    }
                    yield f"data: {json.dumps(u0)}\n\n".encode()
                yield b"data: [DONE]\n\n"
                return
            for i, word in enumerate(text.split(" ")):
                if st.stream_fail_after_chunks is not None and i >= st.stream_fail_after_chunks:
                    err = {"error": {"message": "mock mid-stream failure", "type": "server_error"}}
                    yield f"data: {json.dumps(err)}\n\n".encode()
                    return
                if st.stream_chunk_delay_s:
                    await asyncio.sleep(st.stream_chunk_delay_s)
                yield c({"content": word + " "})
            yield c({}, "stop")
            if include_usage and not st.omit_usage:
                u = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [],
                    "usage": usage,
                }
                yield f"data: {json.dumps(u)}\n\n".encode()
            yield b"data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream", headers=_rl_headers())

    return app
