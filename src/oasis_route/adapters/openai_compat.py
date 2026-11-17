"""OpenAI-compatible chat-completions adapter.

Used for providers that genuinely implement the OpenAI chat-completions protocol (OpenAI itself, and
providers that document an OpenAI-compatible endpoint). Provider differences are captured in
:class:`OpenAIProfile`; anything beyond that belongs in a provider-specific adapter.
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

_POLICY_MARKERS = (
    "content_policy",
    "content_filter",
    "unsupported_country",
    "region",
    "moderation",
    "safety",
    "policy_violation",
)
_QUOTA_MARKERS = ("insufficient_quota", "quota_exceeded", "billing", "credit", "exceeded your current quota")


@dataclass(frozen=True)
class OpenAIProfile:
    provider_id: str
    stream_usage_option: bool = True  # supports stream_options.include_usage
    ratelimit_header_period: QuotaPeriod = QuotaPeriod.MINUTE
    default_capabilities: dict[str, Any] = field(
        default_factory=lambda: {"streaming": True, "tools": False, "json_mode": False, "vision": False}
    )
    extra_headers: dict[str, str] = field(default_factory=dict)


ALLOWED_PASSTHROUGH = (
    "temperature",
    "top_p",
    "stop",
    "tools",
    "tool_choice",
    "response_format",
    "seed",
    "user",
)


class OpenAICompatibleAdapter:
    def __init__(self, profile: OpenAIProfile) -> None:
        self.profile = profile
        self.metadata = AdapterMetadata(
            adapter_id="openai_compatible",
            version="1.0.0",
            protocols=("openai_chat",),
            auth_method="bearer_api_key",
            streaming=True,
            cancellation=True,
            usage_reporting="reported" if profile.stream_usage_option else "reported_non_streaming",
            quota_headers=True,
            retry_behavior="no internal retries; gateway performs bounded fallback",
            required_secrets=("api_key",),
            notes="Chat completions only. Embeddings, images, audio, and Assistants are not proxied.",
        )

    # --------------------------------------------------------------------------------- helpers
    def _headers(self, conn: ConnectionContext) -> dict[str, str]:
        h = {"content-type": "application/json", **self.profile.extra_headers, **conn.extra_headers}
        key = conn.secret.get("api_key")
        if key:
            h["authorization"] = f"Bearer {key}"
        return h

    def _url(self, conn: ConnectionContext, path: str) -> str:
        return conn.base_url.rstrip("/") + path

    def _body(self, req: ChatRequest, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {"model": req.model, "messages": req.messages}
        if req.max_tokens is not None:
            body["max_tokens"] = req.max_tokens
        for name in ALLOWED_PASSTHROUGH:
            value = getattr(req, name)
            if value is not None:
                body[name] = value
        if stream:
            body["stream"] = True
            if self.profile.stream_usage_option:
                body["stream_options"] = {"include_usage": True}
        return body

    # --------------------------------------------------------------------------------- contract
    def get_capabilities(self, upstream_model: str) -> dict[str, Any]:
        return dict(self.profile.default_capabilities)

    def estimate_usage(self, req: ChatRequest, default_max_output: int) -> UsageEstimate:
        return UsageEstimate(
            input_tokens=estimate_tokens_from_messages(req.messages, req.tools),
            output_tokens=req.max_tokens or default_max_output,
        )

    def extract_usage(self, payload: dict[str, Any]) -> Usage:
        u = payload.get("usage")
        if not isinstance(u, dict):
            return Usage(provider_reported=False)
        inp = u.get("prompt_tokens", u.get("input_tokens"))
        out = u.get("completion_tokens", u.get("output_tokens"))
        if not isinstance(inp, int) or not isinstance(out, int):
            return Usage(provider_reported=False)
        return Usage(input_tokens=inp, output_tokens=out, provider_reported=True)

    def quota_observations(self, headers: httpx.Headers) -> list[QuotaObservation]:
        out: list[QuotaObservation] = []
        period = self.profile.ratelimit_header_period
        for suffix, unit in (("requests", QuotaUnit.REQUESTS), ("tokens", QuotaUnit.TOTAL_TOKENS)):
            limit = to_int(headers.get(f"x-ratelimit-limit-{suffix}"))
            remaining = to_int(headers.get(f"x-ratelimit-remaining-{suffix}"))
            if limit is None and remaining is None:
                continue
            out.append(
                QuotaObservation(
                    unit=unit,
                    period=period,
                    limit=limit,
                    remaining=remaining,
                    reset_at=parse_reset(headers.get(f"x-ratelimit-reset-{suffix}")),
                )
            )
        return out

    def normalize_error(self, status: int, headers: httpx.Headers, body: bytes) -> UpstreamError:
        text = body.decode("utf-8", "replace")
        code, message = "", text
        try:
            parsed = json.loads(text)
            err = parsed.get("error", parsed) if isinstance(parsed, dict) else {}
            if isinstance(err, dict):
                code = str(err.get("code") or err.get("type") or "")
                message = str(err.get("message") or text)
            elif isinstance(err, str):
                message = err
        except (ValueError, AttributeError):
            pass
        lowered = f"{code} {message}".lower()
        safe = sanitize_provider_error(message)
        retry_after = parse_retry_after(headers)
        if status == 401:
            cat = ErrorCategory.AUTH_EXPIRED if "expired" in lowered else ErrorCategory.AUTH_INVALID
        elif status == 403:
            cat = (
                ErrorCategory.POLICY_REJECTED
                if any(m in lowered for m in _POLICY_MARKERS)
                else ErrorCategory.AUTH_INVALID
            )
        elif status == 404:
            cat = ErrorCategory.MODEL_UNAVAILABLE
        elif status == 429:
            cat = (
                ErrorCategory.QUOTA_EXHAUSTED
                if any(m in lowered for m in _QUOTA_MARKERS)
                else ErrorCategory.RATE_LIMITED
            )
        elif status in (400, 413, 422):
            cat = (
                ErrorCategory.POLICY_REJECTED
                if any(m in lowered for m in _POLICY_MARKERS[:2])
                else ErrorCategory.REQUEST_UNSUPPORTED
            )
        elif status == 402:
            cat = ErrorCategory.QUOTA_EXHAUSTED
        elif status in (408, 504):
            cat = ErrorCategory.TIMEOUT
        elif status >= 500:
            cat = ErrorCategory.PROVIDER_UNAVAILABLE
        else:
            cat = ErrorCategory.UNKNOWN_UPSTREAM
        return UpstreamError(cat, f"upstream {status}: {safe}", status=status, retry_after_s=retry_after)

    async def list_models(self, conn: ConnectionContext) -> list[ModelInfo]:
        try:
            r = await conn.client.get(
                self._url(conn, "/models"), headers=self._headers(conn), timeout=timeout_for(conn)
            )
        except httpx.HTTPError as exc:
            raise transport_error(exc) from exc
        body = r.content
        if r.status_code != 200:
            raise self.normalize_error(r.status_code, r.headers, body)
        try:
            data = json.loads(body)
            items = data.get("data", []) if isinstance(data, dict) else []
        except ValueError as exc:
            raise UpstreamError(
                ErrorCategory.UNKNOWN_UPSTREAM, "malformed model list", retryable=False
            ) from exc
        out = []
        for it in items:
            if isinstance(it, dict) and isinstance(it.get("id"), str):
                out.append(
                    ModelInfo(
                        upstream_id=it["id"],
                        display_name=str(it.get("name") or it["id"]),
                        context_window=to_int(str(it.get("context_length") or it.get("context_window") or ""))
                        or None,
                    )
                )
        return out

    async def validate_connection(self, conn: ConnectionContext) -> list[ModelInfo]:
        return await self.list_models(conn)

    async def health_check(self, conn: ConnectionContext) -> bool:
        try:
            await self.list_models(conn)
        except UpstreamError:
            return False
        return True

    async def invoke(self, conn: ConnectionContext, req: ChatRequest) -> ChatResult:
        try:
            async with conn.client.stream(
                "POST",
                self._url(conn, "/chat/completions"),
                headers=self._headers(conn),
                json=self._body(req, False),
                timeout=timeout_for(conn),
            ) as r:
                body = await read_capped(r, conn.max_response_bytes)
                headers = r.headers
                status = r.status_code
        except UpstreamError:
            raise
        except httpx.HTTPError as exc:
            raise transport_error(exc) from exc
        observations = self.quota_observations(headers)
        if status >= 300:
            err = self.normalize_error(status, headers, body)
            err.observations = observations
            raise err
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict) or not isinstance(payload.get("choices"), list):
                raise ValueError("missing choices")
        except ValueError as exc:
            raise UpstreamError(
                ErrorCategory.UNKNOWN_UPSTREAM, "malformed upstream response", retryable=True
            ) from exc
        payload.setdefault("object", "chat.completion")
        payload.setdefault("id", f"chatcmpl-{uuid.uuid4().hex}")
        payload.setdefault("created", int(time.time()))
        return ChatResult(
            payload=payload,
            usage=self.extract_usage(payload),
            upstream_model=str(payload.get("model") or req.model),
            observations=observations,
        )

    async def stream(self, conn: ConnectionContext, req: ChatRequest) -> StreamHandle:
        request = conn.client.build_request(
            "POST",
            self._url(conn, "/chat/completions"),
            headers=self._headers(conn),
            json=self._body(req, True),
            timeout=timeout_for(conn),
        )
        try:
            r = await conn.client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise transport_error(exc) from exc
        observations = self.quota_observations(r.headers)
        if r.status_code >= 300:
            try:
                body = await read_capped(r, 64_000)
            finally:
                await r.aclose()
            err = self.normalize_error(r.status_code, r.headers, body)
            err.observations = observations
            raise err

        async def gen() -> AsyncIterator[StreamChunk]:
            try:
                async for _event, data in iter_sse(r, conn.max_response_bytes):
                    if data.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError as exc:
                        raise UpstreamError(
                            ErrorCategory.UNKNOWN_UPSTREAM, "malformed stream chunk", retryable=False
                        ) from exc
                    if not isinstance(chunk, dict):
                        continue
                    if "error" in chunk:
                        err = chunk["error"]
                        msg = err.get("message", "") if isinstance(err, dict) else str(err)
                        raise UpstreamError(
                            ErrorCategory.UNKNOWN_UPSTREAM,
                            f"upstream stream error: {sanitize_provider_error(str(msg))}",
                            retryable=False,
                        )
                    usage = self.extract_usage(chunk) if chunk.get("usage") else None
                    chars = 0
                    for ch in chunk.get("choices") or []:
                        delta = ch.get("delta") or {}
                        chars += len(delta.get("content") or "") + len(str(delta.get("tool_calls") or ""))
                    yield StreamChunk(data=chunk, usage=usage, text_chars=chars)
            except httpx.HTTPError as exc:
                raise transport_error(exc) from exc
            finally:
                await r.aclose()

        return StreamHandle(chunks=gen(), observations=observations, upstream_model=req.model, _response=r)
