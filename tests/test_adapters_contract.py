"""Deterministic adapter contract suite (docs/PROVIDER_ADAPTERS.md §Contract tests).

Every adapter is exercised against mock transports for: success, malformed responses, streaming,
cancellation, timeout, connection reset, 401/403/404/429/5xx, Retry-After, usage extraction and missing usage,
quota headers, secret redaction, oversized responses, and redirects (never followed).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from oasis_route.adapters.anthropic import AnthropicAdapter, from_anthropic, to_anthropic
from oasis_route.adapters.base import (
    ChatRequest,
    ConnectionContext,
    parse_duration,
    parse_reset,
    parse_retry_after,
)
from oasis_route.adapters.openai_compat import OpenAICompatibleAdapter
from oasis_route.adapters.registry import PROFILES
from oasis_route.domain.enums import ErrorCategory, QuotaUnit
from oasis_route.domain.errors import UpstreamError

SECRET = "sk-test-SECRETSECRETSECRETSECRET1234"
Handler = Callable[[httpx.Request], httpx.Response]


def conn(handler: Handler, *, provider: str = "openai", max_bytes: int = 1_000_000) -> ConnectionContext:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    base = "https://api.anthropic.test" if provider == "anthropic" else "https://api.provider.test/v1"
    return ConnectionContext(
        provider_id=provider,
        base_url=base,
        secret={"api_key": SECRET},
        client=client,
        max_response_bytes=max_bytes,
        read_timeout_s=5,
    )


def req(**kw: Any) -> ChatRequest:
    return ChatRequest(
        model=kw.pop("model", "m1"), messages=kw.pop("messages", [{"role": "user", "content": "hi"}]), **kw
    )


OPENAI_OK = {
    "id": "x",
    "object": "chat.completion",
    "model": "m1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}
ANTH_OK = {
    "id": "msg_1",
    "type": "message",
    "model": "claude-x",
    "role": "assistant",
    "content": [{"type": "text", "text": "hello"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 5, "output_tokens": 2},
}


def oa() -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(PROFILES["openai"])


ADAPTERS = {
    "openai": (oa, OPENAI_OK, "openai"),
    "anthropic": (AnthropicAdapter, ANTH_OK, "anthropic"),
}


@pytest.fixture(params=list(ADAPTERS))
def kind(request: pytest.FixtureRequest) -> str:
    return str(request.param)


# ----------------------------------------------------------------------------------------- success
async def test_success_and_usage(kind: str) -> None:
    factory, ok, provider = ADAPTERS[kind]
    seen: dict[str, Any] = {}

    def h(r: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(r.headers)
        seen["body"] = json.loads(r.content)
        return httpx.Response(200, json=ok)

    res = await factory().invoke(conn(h, provider=provider), req(max_tokens=50))
    assert res.payload["choices"][0]["message"]["content"] == "hello"
    assert res.usage.provider_reported
    assert (res.usage.input_tokens, res.usage.output_tokens) == (5, 2)
    if kind == "openai":
        assert seen["headers"]["authorization"] == f"Bearer {SECRET}"
    else:
        assert seen["headers"]["x-api-key"] == SECRET
        assert seen["body"]["max_tokens"] == 50


async def test_missing_usage_is_not_reported(kind: str) -> None:
    factory, ok, provider = ADAPTERS[kind]
    body = {k: v for k, v in ok.items() if k != "usage"}
    res = await factory().invoke(conn(lambda r: httpx.Response(200, json=body), provider=provider), req())
    assert not res.usage.provider_reported
    assert "usage" not in res.payload


async def test_malformed_response(kind: str) -> None:
    factory, _, provider = ADAPTERS[kind]
    with pytest.raises(UpstreamError) as e:
        await factory().invoke(
            conn(lambda r: httpx.Response(200, content=b"<html>"), provider=provider), req()
        )
    assert e.value.category is ErrorCategory.UNKNOWN_UPSTREAM


# ----------------------------------------------------------------------------------------- error mapping
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, {"error": {"message": "bad key", "code": "invalid_api_key"}}, ErrorCategory.AUTH_INVALID),
        (
            403,
            {
                "error": {
                    "message": "Country, region, or territory not supported",
                    "code": "unsupported_country_region_territory",
                }
            },
            ErrorCategory.POLICY_REJECTED,
        ),
        (404, {"error": {"message": "model not found"}}, ErrorCategory.MODEL_UNAVAILABLE),
        (
            429,
            {"error": {"message": "Rate limit reached", "code": "rate_limit_exceeded"}},
            ErrorCategory.RATE_LIMITED,
        ),
        (
            429,
            {"error": {"message": "You exceeded your current quota", "code": "insufficient_quota"}},
            ErrorCategory.QUOTA_EXHAUSTED,
        ),
        (400, {"error": {"message": "context length exceeded"}}, ErrorCategory.REQUEST_UNSUPPORTED),
        (
            400,
            {"error": {"message": "flagged", "code": "content_policy_violation"}},
            ErrorCategory.POLICY_REJECTED,
        ),
        (500, {"error": {"message": "boom"}}, ErrorCategory.PROVIDER_UNAVAILABLE),
        (503, {"error": {"message": "unavailable"}}, ErrorCategory.PROVIDER_UNAVAILABLE),
    ],
)
async def test_openai_error_mapping(status: int, body: dict[str, Any], expected: ErrorCategory) -> None:
    with pytest.raises(UpstreamError) as e:
        await oa().invoke(conn(lambda r: httpx.Response(status, json=body)), req())
    assert e.value.category is expected
    assert e.value.status == status


@pytest.mark.parametrize(
    ("status", "etype", "expected"),
    [
        (401, "authentication_error", ErrorCategory.AUTH_INVALID),
        (403, "permission_error", ErrorCategory.AUTH_INVALID),
        (404, "not_found_error", ErrorCategory.MODEL_UNAVAILABLE),
        (429, "rate_limit_error", ErrorCategory.RATE_LIMITED),
        (400, "invalid_request_error", ErrorCategory.REQUEST_UNSUPPORTED),
        (529, "overloaded_error", ErrorCategory.PROVIDER_UNAVAILABLE),
        (500, "api_error", ErrorCategory.PROVIDER_UNAVAILABLE),
    ],
)
async def test_anthropic_error_mapping(status: int, etype: str, expected: ErrorCategory) -> None:
    body = {"type": "error", "error": {"type": etype, "message": "x"}}
    with pytest.raises(UpstreamError) as e:
        await AnthropicAdapter().invoke(
            conn(lambda r: httpx.Response(status, json=body), provider="anthropic"), req()
        )
    assert e.value.category is expected


async def test_retry_after_on_429(kind: str) -> None:
    factory, _, provider = ADAPTERS[kind]
    with pytest.raises(UpstreamError) as e:
        await factory().invoke(
            conn(
                lambda r: httpx.Response(
                    429, json={"error": {"message": "slow down"}}, headers={"retry-after": "7"}
                ),
                provider=provider,
            ),
            req(),
        )
    assert e.value.category is ErrorCategory.RATE_LIMITED
    assert e.value.retry_after_s == 7


async def test_error_messages_redact_secrets(kind: str) -> None:
    factory, _, provider = ADAPTERS[kind]
    leak = {"error": {"message": f"Incorrect API key provided: {SECRET}", "type": "authentication_error"}}
    with pytest.raises(UpstreamError) as e:
        await factory().invoke(conn(lambda r: httpx.Response(401, json=leak), provider=provider), req())
    assert SECRET not in str(e.value)
    assert SECRET not in e.value.message


# ----------------------------------------------------------------------------------------- transport failures
async def test_timeout(kind: str) -> None:
    factory, _, provider = ADAPTERS[kind]

    def h(r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=r)

    with pytest.raises(UpstreamError) as e:
        await factory().invoke(conn(h, provider=provider), req())
    assert e.value.category is ErrorCategory.TIMEOUT


async def test_connection_reset(kind: str) -> None:
    factory, _, provider = ADAPTERS[kind]

    def h(r: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("reset", request=r)

    with pytest.raises(UpstreamError) as e:
        await factory().invoke(conn(h, provider=provider), req())
    assert e.value.category is ErrorCategory.PROVIDER_UNAVAILABLE


async def test_oversized_response(kind: str) -> None:
    factory, _, provider = ADAPTERS[kind]
    big = b"x" * 5000
    with pytest.raises(UpstreamError) as e:
        await factory().invoke(
            conn(lambda r: httpx.Response(200, content=big), provider=provider, max_bytes=1000), req()
        )
    assert "size limit" in e.value.message


async def test_redirect_not_followed(kind: str) -> None:
    factory, _, provider = ADAPTERS[kind]
    hits: list[str] = []

    def h(r: httpx.Request) -> httpx.Response:
        hits.append(str(r.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})

    with pytest.raises(UpstreamError):
        await factory().invoke(conn(h, provider=provider), req())
    assert len(hits) == 1
    assert "169.254" not in hits[0]


# ----------------------------------------------------------------------------------------- quota headers
def test_openai_quota_headers() -> None:
    h = httpx.Headers(
        {
            "x-ratelimit-limit-requests": "500",
            "x-ratelimit-remaining-requests": "499",
            "x-ratelimit-reset-requests": "120ms",
            "x-ratelimit-limit-tokens": "30000",
            "x-ratelimit-remaining-tokens": "29000",
            "x-ratelimit-reset-tokens": "6m0s",
        }
    )
    obs = {o.unit: o for o in oa().quota_observations(h)}
    assert obs[QuotaUnit.REQUESTS].remaining == 499
    assert obs[QuotaUnit.TOTAL_TOKENS].limit == 30000
    assert obs[QuotaUnit.TOTAL_TOKENS].reset_at is not None


def test_unknown_quota_headers_yield_nothing() -> None:
    assert oa().quota_observations(httpx.Headers({})) == []
    assert AnthropicAdapter().quota_observations(httpx.Headers({})) == []


def test_anthropic_quota_headers() -> None:
    h = httpx.Headers(
        {
            "anthropic-ratelimit-requests-limit": "50",
            "anthropic-ratelimit-requests-remaining": "0",
            "anthropic-ratelimit-requests-reset": "2026-11-17T10:00:00Z",
        }
    )
    (o,) = AnthropicAdapter().quota_observations(h)
    assert o.remaining == 0
    assert o.reset_at == datetime(2026, 11, 17, 10, tzinfo=UTC)


def test_parsers() -> None:
    assert parse_duration("6m0s") == 360
    assert parse_duration("1h2m3.5s") == 3723.5
    assert parse_duration("20ms") == pytest.approx(0.02)
    assert parse_duration("bogus") is None
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert parse_retry_after(httpx.Headers({"retry-after": "Thu, 01 Jan 2026 00:00:30 GMT"}), now) == 30
    assert parse_retry_after(httpx.Headers({"retry-after-ms": "1500"})) == 1.5
    assert parse_retry_after(httpx.Headers({"retry-after": "999999999"})) == 86_400
    assert parse_reset("30s", now) == now + timedelta(seconds=30)
    assert parse_reset("1767225600", now) == datetime(2026, 1, 1, tzinfo=UTC)


# ----------------------------------------------------------------------------------------- streaming
def _openai_sse(include_usage: bool = True, broken: bool = False) -> bytes:
    chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]},
        {"choices": [{"index": 0, "delta": {"content": "Hel"}}]},
        {"choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}]},
    ]
    if include_usage:
        chunks.append({"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
    out = "".join(
        f"data: {json.dumps({'id': 'c', 'object': 'chat.completion.chunk', **c})}\n\n" for c in chunks
    )
    if broken:
        out += "data: {not json\n\n"
    return (out + "data: [DONE]\n\n").encode()


def _anthropic_sse(complete: bool = True) -> bytes:
    events = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {"model": "claude-x", "usage": {"input_tokens": 4, "output_tokens": 1}},
            },
        ),
        (
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        ),
        (
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
        ),
        (
            "message_delta",
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}},
        ),
    ]
    if complete:
        events.append(("message_stop", {"type": "message_stop"}))
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()


async def test_openai_stream_with_usage() -> None:
    seen: dict[str, Any] = {}

    def h(r: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(r.content)
        return httpx.Response(200, content=_openai_sse(), headers={"content-type": "text/event-stream"})

    handle = await oa().stream(conn(h), req(stream=True))
    text, usage = "", None
    async for ch in handle.chunks:
        for c in (ch.data or {}).get("choices") or []:
            text += c["delta"].get("content") or ""
        usage = ch.usage or usage
    assert text == "Hello"
    assert usage is not None
    assert usage.provider_reported
    assert usage.output_tokens == 2
    assert seen["body"]["stream_options"] == {"include_usage": True}


async def test_openai_stream_malformed_chunk() -> None:
    handle = await oa().stream(
        conn(lambda r: httpx.Response(200, content=_openai_sse(broken=True))), req(stream=True)
    )
    with pytest.raises(UpstreamError):
        async for _ in handle.chunks:
            pass


async def test_stream_error_status_raises_before_body(kind: str) -> None:
    factory, _, provider = ADAPTERS[kind]
    with pytest.raises(UpstreamError) as e:
        await factory().stream(
            conn(
                lambda r: httpx.Response(429, json={"error": {"message": "x"}}, headers={"retry-after": "3"}),
                provider=provider,
            ),
            req(stream=True),
        )
    assert e.value.category is ErrorCategory.RATE_LIMITED
    assert e.value.retry_after_s == 3


async def test_anthropic_stream_translation() -> None:
    handle = await AnthropicAdapter().stream(
        conn(lambda r: httpx.Response(200, content=_anthropic_sse()), provider="anthropic"), req(stream=True)
    )
    texts, finish, usage = [], None, None
    async for ch in handle.chunks:
        for c in (ch.data or {}).get("choices") or []:
            if c["delta"].get("content"):
                texts.append(c["delta"]["content"])
            finish = c.get("finish_reason") or finish
        usage = ch.usage or usage
    assert "".join(texts) == "Hello"
    assert finish == "stop"
    assert usage is not None
    assert (usage.input_tokens, usage.output_tokens) == (4, 3)


async def test_anthropic_truncated_stream_errors() -> None:
    handle = await AnthropicAdapter().stream(
        conn(lambda r: httpx.Response(200, content=_anthropic_sse(False)), provider="anthropic"),
        req(stream=True),
    )
    with pytest.raises(UpstreamError):
        async for _ in handle.chunks:
            pass


async def test_stream_cancellation_closes_upstream(kind: str) -> None:
    factory, _, provider = ADAPTERS[kind]
    closed = {"v": False}

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            yield (_openai_sse() if kind == "openai" else _anthropic_sse())[:60]
            yield b"\n\n"

        async def aclose(self) -> None:
            closed["v"] = True

    handle = await factory().stream(
        conn(lambda r: httpx.Response(200, stream=Body()), provider=provider), req(stream=True)
    )
    await handle.close()
    assert closed["v"]


# ----------------------------------------------------------------------------------------- translation
def test_anthropic_request_translation() -> None:
    r = req(
        messages=[
            {"role": "system", "content": "be terse"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "t1", "type": "function", "function": {"name": "f", "arguments": '{"a":1}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "42"},
        ],
        tools=[{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}],
        tool_choice="required",
        stop=["END"],
    )
    body = to_anthropic(r, 256)
    assert body["system"] == "be terse"
    assert body["max_tokens"] == 256
    assert body["messages"][0]["content"][1]["source"]["type"] == "base64"
    assert body["messages"][1]["content"][0]["type"] == "tool_use"
    assert body["messages"][1]["content"][0]["input"] == {"a": 1}
    assert body["messages"][2]["content"][0]["type"] == "tool_result"
    assert body["tool_choice"] == {"type": "any"}
    assert body["stop_sequences"] == ["END"]


def test_anthropic_response_translation_tool_use() -> None:
    out = from_anthropic(
        {
            "id": "m",
            "model": "c",
            "content": [{"type": "tool_use", "id": "t", "name": "f", "input": {"x": 1}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )
    choice = out["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert json.loads(choice["message"]["tool_calls"][0]["function"]["arguments"]) == {"x": 1}


def test_required_features_detection() -> None:
    r = req(
        tools=[{"type": "function", "function": {"name": "f"}}],
        response_format={"type": "json_object"},
        stream=True,
        messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "u"}}]}],
    )
    assert r.required_features() == {"tools", "json_mode", "streaming", "vision"}


async def test_list_models(kind: str) -> None:
    factory, _, provider = ADAPTERS[kind]
    data = {"data": [{"id": "m-a"}, {"id": "m-b"}, {"bad": True}]}
    models = await factory().list_models(conn(lambda r: httpx.Response(200, json=data), provider=provider))
    assert [m.upstream_id for m in models] == ["m-a", "m-b"]
    with pytest.raises(UpstreamError):
        await factory().validate_connection(conn(lambda r: httpx.Response(401, json={}), provider=provider))
    assert not await factory().health_check(conn(lambda r: httpx.Response(500, json={}), provider=provider))
