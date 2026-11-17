"""Provider adapter contract (see docs/PROVIDER_ADAPTERS.md).

Adapters translate between the normalized gateway request (OpenAI chat-completions shape) and a provider
protocol. They receive credentials at call time and must never persist or log them.
"""

from __future__ import annotations

import email.utils
import math
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx

from oasis_route.domain.enums import ErrorCategory, QuotaPeriod, QuotaUnit
from oasis_route.domain.errors import UpstreamError


@dataclass
class ChatRequest:
    """Normalized chat request. ``model`` is the upstream model id."""

    model: str
    messages: list[dict[str, Any]]
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop: list[str] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    response_format: dict[str, Any] | None = None
    seed: int | None = None
    stream: bool = False
    user: str | None = None

    def required_features(self) -> set[str]:
        feats: set[str] = set()
        if self.tools:
            feats.add("tools")
        if self.response_format and self.response_format.get("type") in {"json_object", "json_schema"}:
            feats.add("json_mode")
        if self.stream:
            feats.add("streaming")
        if any(_has_image(m) for m in self.messages):
            feats.add("vision")
        return feats


def _has_image(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(p, dict) and p.get("type") in {"image_url", "input_image", "image"} for p in content
    )


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    provider_reported: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class UsageEstimate:
    input_tokens: int
    output_tokens: int
    approximate: bool = True


@dataclass
class QuotaObservation:
    """A provider-reported quota fact (from headers or a quota API)."""

    unit: QuotaUnit
    period: QuotaPeriod
    limit: int | None
    remaining: int | None
    reset_at: datetime | None
    source: str = "response_headers"


@dataclass
class ChatResult:
    payload: dict[str, Any]  # OpenAI chat.completion object
    usage: Usage
    upstream_model: str
    observations: list[QuotaObservation] = field(default_factory=list)


@dataclass
class StreamChunk:
    """An OpenAI ``chat.completion.chunk`` object, or a terminal usage report."""

    data: dict[str, Any] | None
    usage: Usage | None = None
    text_chars: int = 0


@dataclass
class StreamHandle:
    """An opened upstream stream. ``chunks`` is consumed by the gateway; ``close`` cancels upstream."""

    chunks: AsyncIterator[StreamChunk]
    observations: list[QuotaObservation]
    upstream_model: str
    _response: httpx.Response | None = None

    async def close(self) -> None:
        if self._response is not None:
            await self._response.aclose()


@dataclass
class ConnectionContext:
    """Everything an adapter needs for one call. ``secret`` is decrypted per call and never stored."""

    provider_id: str
    base_url: str
    secret: dict[str, Any]
    client: httpx.AsyncClient
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 120.0
    max_response_bytes: int = 20_000_000
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class ModelInfo:
    upstream_id: str
    display_name: str = ""
    context_window: int | None = None
    max_output_tokens: int | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    # True when the provider's own authenticated model list prices this model at zero; None == not reported.
    free: bool | None = None


@dataclass
class AccountStatus:
    """Provider-reported facts about the connected account (never secrets)."""

    free_tier: bool | None = None  # True == the account cannot be billed (e.g. never purchased credits)
    credit_limit: float | None = None
    credit_remaining: float | None = None
    usage_total: float | None = None
    free_model_daily_limit: int | None = None
    free_model_daily_used: int | None = None
    payment_method: bool | None = None  # provider says a payment method is on file
    plan: str | None = None
    source: str = ""


@dataclass(frozen=True)
class AdapterMetadata:
    adapter_id: str
    version: str
    protocols: tuple[str, ...]
    auth_method: str
    streaming: bool
    cancellation: bool
    usage_reporting: str  # "reported" | "reported_when_requested" | "none"
    quota_headers: bool
    retry_behavior: str
    required_secrets: tuple[str, ...]
    notes: str = ""
    tested_models: tuple[str, ...] = ()


class ProviderAdapter(Protocol):
    metadata: AdapterMetadata

    async def list_models(self, conn: ConnectionContext) -> list[ModelInfo]: ...

    def get_capabilities(self, upstream_model: str) -> dict[str, Any]: ...

    async def validate_connection(self, conn: ConnectionContext) -> list[ModelInfo]: ...

    def estimate_usage(self, req: ChatRequest, default_max_output: int) -> UsageEstimate: ...

    async def invoke(self, conn: ConnectionContext, req: ChatRequest) -> ChatResult: ...

    async def stream(self, conn: ConnectionContext, req: ChatRequest) -> StreamHandle: ...

    def normalize_error(self, status: int, headers: httpx.Headers, body: bytes) -> UpstreamError: ...

    def extract_usage(self, payload: dict[str, Any]) -> Usage: ...

    def quota_observations(self, headers: httpx.Headers) -> list[QuotaObservation]: ...

    async def health_check(self, conn: ConnectionContext) -> bool: ...


# ----------------------------------------------------------------------------------------- shared helpers
_DURATION = re.compile(r"(?P<n>\d+(?:\.\d+)?)(?P<u>ms|s|m|h|d)")


def parse_duration(value: str) -> float | None:
    """Parse ``1s``, ``6m0s``, ``1h2m3.5s``, ``20ms`` durations (OpenAI/Groq header style) to seconds."""
    value = value.strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    total, pos = 0.0, 0
    for m in _DURATION.finditer(value):
        if m.start() != pos:
            return None
        pos = m.end()
        n = float(m.group("n"))
        total += n * {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group("u")]
    return total if pos == len(value) and pos > 0 else None


def parse_retry_after(headers: httpx.Headers, now: datetime | None = None) -> float | None:
    """Parse ``Retry-After`` (delta-seconds or HTTP-date) and ``retry-after-ms``. Bounded to 1 day."""
    now = now or datetime.now(UTC)
    ms = headers.get("retry-after-ms")
    if ms:
        try:
            return min(86_400.0, max(0.0, float(ms) / 1000))
        except ValueError:
            pass
    ra = headers.get("retry-after")
    if not ra:
        return None
    try:
        return min(86_400.0, max(0.0, float(ra)))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(ra)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return min(86_400.0, max(0.0, (dt - now).total_seconds()))


def parse_reset(value: str | None, now: datetime | None = None) -> datetime | None:
    """Reset header: duration (``6m0s``), epoch seconds, or RFC 3339 timestamp."""
    if not value:
        return None
    now = now or datetime.now(UTC)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        pass
    try:
        num = float(value)
        if num > 1_000_000_000:  # epoch seconds
            return datetime.fromtimestamp(num, UTC)
    except ValueError:
        pass
    d = parse_duration(value)
    return now + timedelta(seconds=d) if d is not None else None


def to_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def estimate_tokens_from_messages(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> int:
    """Conservative approximation (~3.5 chars/token, rounded up, plus per-message overhead).

    Explicitly approximate; replaced by provider-reported usage at reconciliation.
    """
    chars = 0
    images = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"image_url", "input_image", "image"}:
                    images += 1
                else:
                    chars += len(str(part.get("text", "")))
        for tc in m.get("tool_calls") or []:
            chars += len(str(tc))
    if tools:
        chars += len(str(tools))
    return math.ceil(chars / 3.5) + 4 * len(messages) + 3 + images * 1000


async def read_capped(response: httpx.Response, limit: int) -> bytes:
    buf = bytearray()
    async for chunk in response.aiter_bytes():
        buf.extend(chunk)
        if len(buf) > limit:
            raise UpstreamError(
                ErrorCategory.UNKNOWN_UPSTREAM, "upstream response exceeded size limit", retryable=False
            )
    return bytes(buf)


def transport_error(exc: Exception) -> UpstreamError:
    if isinstance(exc, httpx.TimeoutException):
        return UpstreamError(ErrorCategory.TIMEOUT, "upstream timed out")
    if isinstance(exc, httpx.TransportError):
        return UpstreamError(
            ErrorCategory.PROVIDER_UNAVAILABLE, f"upstream connection failed: {type(exc).__name__}"
        )
    return UpstreamError(ErrorCategory.UNKNOWN_UPSTREAM, f"upstream error: {type(exc).__name__}")


def timeout_for(conn: ConnectionContext) -> httpx.Timeout:
    return httpx.Timeout(conn.read_timeout_s, connect=conn.connect_timeout_s)


async def iter_sse(response: httpx.Response, limit: int) -> AsyncIterator[tuple[str | None, str]]:
    """Yield (event, data) pairs from a server-sent-events response, bounded by ``limit`` bytes."""
    total = 0
    event: str | None = None
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        total += len(line) + 1
        if total > limit:
            raise UpstreamError(
                ErrorCategory.UNKNOWN_UPSTREAM, "upstream stream exceeded size limit", retryable=False
            )
        if line == "":
            if data_lines:
                yield event, "\n".join(data_lines)
            event, data_lines = None, []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "event":
            event = value
        elif name == "data":
            data_lines.append(value)
    if data_lines:
        yield event, "\n".join(data_lines)
