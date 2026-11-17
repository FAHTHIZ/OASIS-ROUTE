"""OpenTelemetry spans: GenAI semantic-convention attributes, never prompt content."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from oasis_route.telemetry import configure_tracing
from tests.helpers import chat, gateway_setup

_EXPORTER = InMemorySpanExporter()


@pytest.fixture(scope="module", autouse=True)
def _provider() -> None:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
    trace.set_tracer_provider(provider)  # first call wins for the process; idempotent afterwards


@pytest.fixture(autouse=True)
def _clear() -> None:
    _EXPORTER.clear()


def test_tracing_disabled_by_default() -> None:
    assert configure_tracing(False) is False


@pytest.mark.usefixtures("upstreams")
async def test_gateway_spans_have_genai_attributes_and_no_content(
    client: httpx.AsyncClient, upstreams: dict[str, Any]
) -> None:
    if not isinstance(trace.get_tracer_provider(), TracerProvider):  # pragma: no cover
        pytest.skip("another tracer provider was installed first")
    g = await gateway_setup(client)
    upstreams["mock-a.test"].fail_next("mock-large", 503)
    r = await chat(client, g["key"], content="TOP-SECRET-PROMPT")
    assert r.status_code == 200
    r = await chat(client, g["key"], content="TOP-SECRET-PROMPT", stream=True)
    assert r.status_code == 200
    spans = _EXPORTER.get_finished_spans()
    names = [s.name for s in spans]
    assert "oasis.route.plan" in names
    up = [s for s in spans if s.name == "oasis.upstream.chat"]
    assert len(up) == 2  # failed attempt + fallback
    assert up[0].attributes is not None and up[0].attributes["error.type"] == "PROVIDER_UNAVAILABLE"
    ok = up[1].attributes or {}
    assert ok["gen_ai.system"] == "mock_alt" and ok["gen_ai.request.model"] == "mock-large"
    assert int(ok["gen_ai.usage.input_tokens"]) > 0  # type: ignore[arg-type]
    assert any(s.name == "oasis.upstream.chat_stream" for s in spans)
    dump = repr([(s.name, dict(s.attributes or {})) for s in spans])
    assert "TOP-SECRET-PROMPT" not in dump
