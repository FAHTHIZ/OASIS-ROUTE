"""Anthropic Messages API adapter.

Translates OpenAI-style chat requests to ``POST /v1/messages`` and translates responses/stream events back
into OpenAI chat-completion objects. Compatibility notes:

- ``system`` messages are concatenated into the top-level ``system`` parameter.
- ``tools``/``tool_calls``/``tool`` messages map to ``tool_use``/``tool_result`` blocks.
- ``image_url`` data URLs map to base64 image blocks; remote image URLs map to ``url`` sources.
- ``response_format`` is not supported natively; capability ``json_mode`` is false so the router avoids it.
- ``n > 1``, ``logprobs``, ``presence_penalty`` and ``frequency_penalty`` are not supported.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from oasis_route.adapters.base import (
    AdapterMetadata,
    ChatRequest,
    ChatResult,
    ConnectionContext,
    ModelInfo,
    QuotaObservation,
    StreamChunk,
    StreamHandle,
    Usage,
    UsageEstimate,
    estimate_tokens_from_messages,
    iter_sse,
    parse_reset,
    parse_retry_after,
    read_capped,
    timeout_for,
    to_int,
    transport_error,
)
from oasis_route.domain.enums import ErrorCategory, QuotaPeriod, QuotaUnit
from oasis_route.domain.errors import UpstreamError
from oasis_route.security.redaction import sanitize_provider_error

API_VERSION = "2023-06-01"
_STOP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
    "pause_turn": "stop",
}


def _image_block(part: dict[str, Any]) -> dict[str, Any] | None:
    url = (
        (part.get("image_url") or {}).get("url")
        if isinstance(part.get("image_url"), dict)
        else part.get("image_url")
    )
    if not isinstance(url, str):
        return None
    if url.startswith("data:") and ";base64," in url:
        media, data = url[5:].split(";base64,", 1)
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _content_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks: list[dict[str, Any]] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            blocks.append({"type": "text", "text": str(part.get("text", ""))})
        elif part.get("type") == "image_url":
            img = _image_block(part)
            if img:
                blocks.append(img)
    return blocks


def to_anthropic(req: ChatRequest, default_max: int) -> dict[str, Any]:
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": blocks})

    for m in req.messages:
        role = m.get("role")
        if role in ("system", "developer"):
            c = m.get("content")
            system_parts.append(
                c
                if isinstance(c, str)
                else " ".join(str(p.get("text", "")) for p in c or [] if isinstance(p, dict))
            )
        elif role == "user":
            push("user", _content_blocks(m.get("content")))
        elif role == "assistant":
            blocks = _content_blocks(m.get("content"))
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {"_raw": fn.get("arguments")}
                blocks.append({"type": "tool_use", "id": tc.get("id"), "name": fn.get("name"), "input": args})
            push("assistant", blocks)
        elif role == "tool":
            c = m.get("content")
            push(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": m.get("tool_call_id"),
                        "content": c if isinstance(c, str) else json.dumps(c),
                    }
                ],
            )
    body: dict[str, Any] = {
        "model": req.model,
        "messages": messages,
        "max_tokens": req.max_tokens or default_max,
    }
    if system_parts:
        body["system"] = "\n\n".join(p for p in system_parts if p)
    if req.temperature is not None:
        body["temperature"] = req.temperature
    if req.top_p is not None:
        body["top_p"] = req.top_p
    if req.stop:
        body["stop_sequences"] = req.stop
    if req.tools:
        body["tools"] = [
            {
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}},
            }
            for t in req.tools
            if t.get("type") == "function" and "function" in t
        ]
        tc = req.tool_choice
        if tc == "required":
            body["tool_choice"] = {"type": "any"}
        elif tc == "none":
            body["tool_choice"] = {"type": "none"}
        elif isinstance(tc, dict) and tc.get("type") == "function":
            body["tool_choice"] = {"type": "tool", "name": tc["function"]["name"]}
    if req.user:
        body["metadata"] = {"user_id": req.user[:256]}
    return body


def from_anthropic(payload: dict[str, Any]) -> dict[str, Any]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in payload.get("content") or []:
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input") or {}),
                    },
                }
            )
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage = payload.get("usage")
    result: dict[str, Any] = {
        "id": f"chatcmpl-{payload.get('id') or uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": payload.get("model"),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _STOP.get(str(payload.get("stop_reason")), "stop"),
            }
        ],
    }
    if not isinstance(usage, dict) or "input_tokens" not in usage:
        return result
    inp, out = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    inp += int(usage.get("cache_read_input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0)
    result["usage"] = {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": inp + out}
    return result


@dataclass(frozen=True)
class AnthropicProfile:
    """Differences between Anthropic itself and Anthropic-compatible Messages APIs (MiniMax, Kimi, ...)."""

    provider_id: str = "anthropic"
    auth_header: str = "x-api-key"
    auth_prefix: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)


class AnthropicAdapter:
    def __init__(self, profile: AnthropicProfile | None = None) -> None:
        self.profile = profile or AnthropicProfile()
        self.metadata = AdapterMetadata(
            adapter_id="anthropic",
            version="1.0.0",
            protocols=("anthropic_messages",),
            auth_method=self.profile.auth_header.lower()
            if self.profile.auth_header.lower() != "authorization"
            else "bearer_api_key",
            streaming=True,
            cancellation=True,
            usage_reporting="reported",
            quota_headers=True,
            retry_behavior="no internal retries; gateway performs bounded fallback",
            required_secrets=("api_key",),
            notes="Translates OpenAI chat format. response_format/json_schema unsupported; n>1 unsupported.",
        )

    def _headers(self, conn: ConnectionContext) -> dict[str, str]:
        key = str(conn.secret.get("api_key", ""))
        return {
            self.profile.auth_header.lower(): f"{self.profile.auth_prefix}{key}",
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
            **self.profile.extra_headers,
            **conn.extra_headers,
        }

    def _url(self, conn: ConnectionContext, path: str) -> str:
        base = conn.base_url.rstrip("/")
        return base + path if base.endswith("/v1") else base + "/v1" + path

    def get_capabilities(self, upstream_model: str) -> dict[str, Any]:
        return {"streaming": True, "tools": True, "json_mode": False, "vision": True}

    def estimate_usage(self, req: ChatRequest, default_max_output: int) -> UsageEstimate:
        return UsageEstimate(
            input_tokens=estimate_tokens_from_messages(req.messages, req.tools),
            output_tokens=req.max_tokens or default_max_output,
        )

    def extract_usage(self, payload: dict[str, Any]) -> Usage:
        u = payload.get("usage")
        if not isinstance(u, dict) or "prompt_tokens" not in u:
            return Usage()
        return Usage(int(u["prompt_tokens"]), int(u.get("completion_tokens") or 0), True)

    def quota_observations(self, headers: httpx.Headers) -> list[QuotaObservation]:
        out = []
        for suffix, unit in (
            ("requests", QuotaUnit.REQUESTS),
            ("input-tokens", QuotaUnit.INPUT_TOKENS),
            ("output-tokens", QuotaUnit.OUTPUT_TOKENS),
            ("tokens", QuotaUnit.TOTAL_TOKENS),
        ):
            limit = to_int(headers.get(f"anthropic-ratelimit-{suffix}-limit"))
            remaining = to_int(headers.get(f"anthropic-ratelimit-{suffix}-remaining"))
            if limit is None and remaining is None:
                continue
            out.append(
                QuotaObservation(
                    unit,
                    QuotaPeriod.MINUTE,
                    limit,
                    remaining,
                    parse_reset(headers.get(f"anthropic-ratelimit-{suffix}-reset")),
                )
            )
        return out

    def normalize_error(self, status: int, headers: httpx.Headers, body: bytes) -> UpstreamError:
        etype, message = "", body.decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
            err = parsed.get("error") or {}
            etype, message = str(err.get("type") or ""), str(err.get("message") or message)
        except (ValueError, AttributeError):
            pass
        lowered = f"{etype} {message}".lower()
        retry_after = parse_retry_after(headers)
        if status == 401 or etype == "authentication_error":
            cat = ErrorCategory.AUTH_INVALID
        elif status == 403 or etype == "permission_error":
            cat = (
                ErrorCategory.POLICY_REJECTED
                if any(w in lowered for w in ("region", "country", "policy"))
                else ErrorCategory.AUTH_INVALID
            )
        elif status == 404 or etype == "not_found_error":
            cat = ErrorCategory.MODEL_UNAVAILABLE
        elif status == 429 or etype == "rate_limit_error":
            cat = (
                ErrorCategory.QUOTA_EXHAUSTED
                if any(w in lowered for w in ("credit", "billing", "quota"))
                else ErrorCategory.RATE_LIMITED
            )
        elif status == 402 or "credit balance" in lowered:
            cat = ErrorCategory.QUOTA_EXHAUSTED
        elif status in (400, 413) or etype in ("invalid_request_error", "request_too_large"):
            cat = ErrorCategory.REQUEST_UNSUPPORTED
        elif status == 529 or etype == "overloaded_error" or status >= 500:
            cat = ErrorCategory.PROVIDER_UNAVAILABLE
        else:
            cat = ErrorCategory.UNKNOWN_UPSTREAM
        return UpstreamError(
            cat,
            f"upstream {status}: {sanitize_provider_error(message)}",
            status=status,
            retry_after_s=retry_after,
        )

    async def list_models(self, conn: ConnectionContext) -> list[ModelInfo]:
        try:
            r = await conn.client.get(
                self._url(conn, "/models"), headers=self._headers(conn), timeout=timeout_for(conn)
            )
        except httpx.HTTPError as exc:
            raise transport_error(exc) from exc
        if r.status_code != 200:
            raise self.normalize_error(r.status_code, r.headers, r.content)
        try:
            items = r.json().get("data", [])
        except ValueError as exc:
            raise UpstreamError(
                ErrorCategory.UNKNOWN_UPSTREAM, "malformed model list", retryable=False
            ) from exc
        return [
            ModelInfo(
                upstream_id=i["id"],
                display_name=i.get("display_name") or i["id"],
                capabilities=self.get_capabilities(i["id"]),
            )
            for i in items
            if isinstance(i, dict) and isinstance(i.get("id"), str)
        ]

    async def validate_connection(self, conn: ConnectionContext) -> list[ModelInfo]:
        return await self.list_models(conn)

    async def health_check(self, conn: ConnectionContext) -> bool:
        try:
            await self.list_models(conn)
        except UpstreamError:
            return False
        return True

    async def invoke(self, conn: ConnectionContext, req: ChatRequest) -> ChatResult:
        body = to_anthropic(req, req.max_tokens or 1024)
        try:
            async with conn.client.stream(
                "POST",
                self._url(conn, "/messages"),
                headers=self._headers(conn),
                json=body,
                timeout=timeout_for(conn),
            ) as r:
                raw = await read_capped(r, conn.max_response_bytes)
                status, headers = r.status_code, r.headers
        except UpstreamError:
            raise
        except httpx.HTTPError as exc:
            raise transport_error(exc) from exc
        obs = self.quota_observations(headers)
        if status >= 300:
            err = self.normalize_error(status, headers, raw)
            err.observations = obs
            raise err
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or "content" not in payload:
                raise ValueError("missing content")
        except ValueError as exc:
            raise UpstreamError(ErrorCategory.UNKNOWN_UPSTREAM, "malformed upstream response") from exc
        converted = from_anthropic(payload)
        return ChatResult(
            payload=converted,
            usage=self.extract_usage(converted),
            upstream_model=str(payload.get("model") or req.model),
            observations=obs,
        )

    async def stream(self, conn: ConnectionContext, req: ChatRequest) -> StreamHandle:
        body = to_anthropic(req, req.max_tokens or 1024)
        body["stream"] = True
        request = conn.client.build_request(
            "POST",
            self._url(conn, "/messages"),
            headers=self._headers(conn),
            json=body,
            timeout=timeout_for(conn),
        )
        try:
            r = await conn.client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise transport_error(exc) from exc
        obs = self.quota_observations(r.headers)
        if r.status_code >= 300:
            try:
                raw = await read_capped(r, 64_000)
            finally:
                await r.aclose()
            err = self.normalize_error(r.status_code, r.headers, raw)
            err.observations = obs
            raise err
        cid = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())

        def chunk(delta: dict[str, Any], finish: str | None = None, model: str = req.model) -> dict[str, Any]:
            return {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }

        async def gen() -> AsyncIterator[StreamChunk]:
            inp = out = 0
            model = req.model
            tool_index: dict[int, int] = {}
            stopped = False
            try:
                async for event, data in iter_sse(r, conn.max_response_bytes):
                    try:
                        ev = json.loads(data)
                    except ValueError as exc:
                        raise UpstreamError(
                            ErrorCategory.UNKNOWN_UPSTREAM, "malformed stream event", retryable=False
                        ) from exc
                    kind = ev.get("type") or event
                    if kind == "message_start":
                        msg = ev.get("message") or {}
                        model = msg.get("model") or model
                        u = msg.get("usage") or {}
                        inp = int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
                        out = int(u.get("output_tokens") or 0)
                        yield StreamChunk(chunk({"role": "assistant", "content": ""}, model=model))
                    elif kind == "content_block_start":
                        block = ev.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            idx = len(tool_index)
                            tool_index[int(ev.get("index", 0))] = idx
                            yield StreamChunk(
                                chunk(
                                    {
                                        "tool_calls": [
                                            {
                                                "index": idx,
                                                "id": block.get("id"),
                                                "type": "function",
                                                "function": {"name": block.get("name"), "arguments": ""},
                                            }
                                        ]
                                    },
                                    model=model,
                                )
                            )
                    elif kind == "content_block_delta":
                        d = ev.get("delta") or {}
                        if d.get("type") == "text_delta":
                            text = d.get("text", "")
                            yield StreamChunk(chunk({"content": text}, model=model), text_chars=len(text))
                        elif d.get("type") == "input_json_delta":
                            part = d.get("partial_json", "")
                            idx = tool_index.get(int(ev.get("index", 0)), 0)
                            yield StreamChunk(
                                chunk(
                                    {"tool_calls": [{"index": idx, "function": {"arguments": part}}]},
                                    model=model,
                                ),
                                text_chars=len(part),
                            )
                    elif kind == "message_delta":
                        u = ev.get("usage") or {}
                        out = int(u.get("output_tokens") or out)
                        stop = (ev.get("delta") or {}).get("stop_reason")
                        if stop:
                            yield StreamChunk(chunk({}, _STOP.get(stop, "stop"), model=model))
                    elif kind == "message_stop":
                        stopped = True
                        break
                    elif kind == "error":
                        err = ev.get("error") or {}
                        cat = (
                            ErrorCategory.PROVIDER_UNAVAILABLE
                            if err.get("type") == "overloaded_error"
                            else ErrorCategory.UNKNOWN_UPSTREAM
                        )
                        raise UpstreamError(
                            cat,
                            f"upstream stream error: {sanitize_provider_error(str(err.get('message', '')))}",
                            retryable=False,
                        )
                if not stopped:
                    raise UpstreamError(
                        ErrorCategory.PROVIDER_UNAVAILABLE,
                        "upstream stream ended unexpectedly",
                        retryable=False,
                    )
                usage = Usage(inp, out, provider_reported=True)
                final = chunk({}, model=model)
                final["choices"] = []
                final["usage"] = {"prompt_tokens": inp, "completion_tokens": out, "total_tokens": inp + out}
                yield StreamChunk(final, usage=usage)
            except httpx.HTTPError as exc:
                raise transport_error(exc) from exc
            finally:
                await r.aclose()

        return StreamHandle(chunks=gen(), observations=obs, upstream_model=req.model, _response=r)
