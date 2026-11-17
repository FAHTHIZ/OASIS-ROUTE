"""Prometheus metrics and OpenTelemetry tracing helpers.

Metric labels never include prompts, keys, or user identifiers beyond opaque project ids.
Span attributes follow the OpenTelemetry GenAI semantic conventions (pinned to semconv 1.28 naming:
``gen_ai.system``, ``gen_ai.request.model``, ``gen_ai.response.model``, ``gen_ai.usage.input_tokens``,
``gen_ai.usage.output_tokens``). Prompt/response content is never recorded.
"""

from __future__ import annotations

from opentelemetry import trace
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

tracer = trace.get_tracer("oasis_route")


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        r = self.registry
        self.http_requests = Counter(
            "oasis_http_requests_total", "HTTP requests", ["method", "route", "status"], registry=r
        )
        self.gateway_requests = Counter(
            "oasis_gateway_requests_total", "Gateway requests by outcome", ["outcome"], registry=r
        )
        self.upstream_attempts = Counter(
            "oasis_upstream_attempts_total",
            "Upstream attempts by provider and outcome",
            ["provider", "outcome"],
            registry=r,
        )
        self.fallbacks = Counter(
            "oasis_fallbacks_total", "Requests served after at least one fallback", ["reason"], registry=r
        )
        self.upstream_latency = Histogram(
            "oasis_upstream_latency_seconds",
            "Upstream latency",
            ["provider"],
            buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120),
            registry=r,
        )
        self.tokens = Counter(
            "oasis_tokens_total", "Tokens processed", ["provider", "direction", "reported"], registry=r
        )
        self.reservations = Counter(
            "oasis_quota_reservations_total", "Quota reservation attempts", ["result"], registry=r
        )
        self.worker_jobs = Counter(
            "oasis_worker_jobs_total", "Background job runs", ["job", "outcome"], registry=r
        )
        self.worker_restarts = Counter(
            "oasis_worker_restarts_total", "Supervised worker restarts after a crash", registry=r
        )
        self.breaker_open = Gauge("oasis_circuit_open", "Open circuit breakers", ["provider"], registry=r)
        self.discovery_fetches = Counter(
            "oasis_discovery_fetches_total", "Discovery fetch results", ["result"], registry=r
        )


def configure_tracing(enabled: bool, service_name: str = "oasis-route") -> bool:
    """Install an OTLP/HTTP exporting tracer provider when enabled and the ``otel`` extra is installed.

    Endpoint, headers, and sampling come from the standard ``OTEL_EXPORTER_OTLP_*`` / ``OTEL_TRACES_SAMPLER``
    environment variables. Returns True if tracing was installed.
    """
    if not enabled:
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:  # pragma: no cover - extra not installed
        import logging

        logging.getLogger(__name__).warning("otel_enabled but the 'otel' extra is not installed")
        return False
    import os

    name = os.environ.get("OTEL_SERVICE_NAME") or service_name
    provider = TracerProvider(resource=Resource.create({"service.name": name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    return True
