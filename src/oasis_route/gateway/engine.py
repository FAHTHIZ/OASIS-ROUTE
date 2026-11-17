"""Gateway request engine (docs/ARCHITECTURE.md §4 request lifecycle).

Per attempt: health probe claim → quota reservation (idempotent ``{request_id}:{attempt}``) → adapter call →
settlement (provider-reported usage or labelled estimate) → provider quota observations → health update.

Failure effects (docs/ROUTING_AND_QUOTAS.md §6-7):
- QUOTA_EXHAUSTED → pools marked exhausted until reset (recheck required); try next candidate.
- RATE_LIMITED → circuit opened for Retry-After; try next candidate.
- AUTH_INVALID / AUTH_EXPIRED → connection suspended + owner notified; try next candidate.
- POLICY_REJECTED / CANCELLED → stop. Never evade a provider's decision.
- After QUOTA/RATE/AUTH failures, *other accounts of the same provider* are excluded unless the provider
  explicitly permits multiple accounts — hopping accounts to extend a free allocation is quota evasion.
- Hops are bounded (project.max_fallback_hops or settings.max_fallback_hops).

Streaming: the first upstream chunk is obtained before response headers are sent, so pre-stream failures can
still fail over. Once bytes reach the client there is no fallback; mid-stream failures emit an error event.
Client disconnects close the upstream and settle with an output estimate.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import anyio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from oasis_route.adapters.base import (
    ChatRequest,
    ChatResult,
    StreamChunk,
    StreamHandle,
    Usage,
    estimate_tokens_from_messages,
)
from oasis_route.connections import service as conns
from oasis_route.context import AppContext
from oasis_route.db.models import (
    Connection,
    IdempotencyRecord,
    Project,
    RouteDecision,
    UsageRecord,
    utcnow,
)
from oasis_route.domain.enums import ErrorCategory, RoutingMode, Task
from oasis_route.domain.errors import STOP_CATEGORIES, UpstreamError
from oasis_route.health import breaker
from oasis_route.logging import get_logger
from oasis_route.quota import ledger
from oasis_route.routing.router import Candidate, RouteOptions, RoutePlan, build_plan
from oasis_route.telemetry import tracer

log = get_logger("oasis_route.gateway")

_ACCOUNT_SCOPED = {
    ErrorCategory.QUOTA_EXHAUSTED,
    ErrorCategory.RATE_LIMITED,
    ErrorCategory.AUTH_INVALID,
    ErrorCategory.AUTH_EXPIRED,
}
_CONNECTION_WIDE = {
    ErrorCategory.AUTH_INVALID,
    ErrorCategory.AUTH_EXPIRED,
    ErrorCategory.PROVIDER_UNAVAILABLE,
}


class GatewayError(Exception):
    """Terminal gateway failure rendered in the OpenAI error shape."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        type_: str = "oasis_route_error",
        retry_after_s: float | None = None,
        meta: dict[str, Any] | None = None,
        param: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.type = type_
        self.retry_after_s = retry_after_s
        self.meta = meta or {}
        self.param = param

    def body(self) -> dict[str, Any]:
        return {
            "error": {"message": self.message, "type": self.type, "code": self.code, "param": self.param},
            "oasis_route": self.meta,
        }


# ------------------------------------------------------------------------------------------- parsing
_PASSTHROUGH = ("temperature", "top_p", "tools", "tool_choice", "response_format", "seed", "user")


@dataclass
class ParsedRequest:
    chat: ChatRequest
    options: RouteOptions
    include_usage_chunk: bool
    raw_hash: str


def parse_body(body: Any, project: Project, settings_default_max: int) -> ParsedRequest:
    if not isinstance(body, dict):
        raise GatewayError(
            400, "invalid_request", "request body must be a JSON object", type_="invalid_request_error"
        )
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages or not all(isinstance(m, dict) for m in messages):
        raise GatewayError(
            400,
            "invalid_request",
            "'messages' must be a non-empty array",
            type_="invalid_request_error",
            param="messages",
        )
    n = body.get("n")
    if n not in (None, 1):
        raise GatewayError(
            400, "unsupported_parameter", "only n=1 is supported", type_="invalid_request_error", param="n"
        )
    max_tokens = body.get("max_completion_tokens", body.get("max_tokens"))
    if max_tokens is not None and (not isinstance(max_tokens, int) or max_tokens < 1):
        raise GatewayError(
            400, "invalid_request", "max_tokens must be a positive integer", type_="invalid_request_error"
        )
    stop = body.get("stop")
    if isinstance(stop, str):
        stop = [stop]
    ext = body.get("oasis_route") or {}
    if not isinstance(ext, dict):
        raise GatewayError(
            400, "invalid_request", "'oasis_route' must be an object", type_="invalid_request_error"
        )
    model_name = str(body.get("model") or "auto")
    task_alias: str | None = None
    if model_name.startswith("auto/"):  # "auto/coding" == model "auto" with task "coding"
        model_name, task_alias = "auto", model_name.split("/", 1)[1]
    try:
        task = Task(ext.get("task") or task_alias or Task.GENERAL.value)
        mode = RoutingMode(ext.get("mode") or project.routing_mode or RoutingMode.STRICT_QUALITY.value)
    except ValueError as exc:
        raise GatewayError(400, "invalid_request", str(exc), type_="invalid_request_error") from exc
    stream = bool(body.get("stream"))
    chat = ChatRequest(
        model=model_name,
        messages=messages,
        max_tokens=max_tokens,
        stop=stop,
        stream=stream,
        **{k: body.get(k) for k in _PASSTHROUGH if body.get(k) is not None},
    )
    require = set(chat.required_features())
    require |= {str(x) for x in ext.get("require") or []}
    allow_downgrade = bool(ext.get("allow_downgrade", project.allow_downgrade)) and project.allow_downgrade
    opts = RouteOptions(
        requested_model=chat.model,
        task=task,
        mode=mode,
        min_quality=float(ext["min_quality"]) if ext.get("min_quality") is not None else None,
        allow_fallback=bool(ext.get("allow_fallback", True)),
        allow_downgrade=allow_downgrade,
        providers=[str(p) for p in ext.get("providers") or []],
        require=require,
        input_tokens=estimate_tokens_from_messages(messages, chat.tools),
        max_output_tokens=max_tokens or settings_default_max,
        free_only=ext.get("free_only") is True,  # can only tighten the project's spend policy
    )
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return ParsedRequest(
        chat=chat,
        options=opts,
        include_usage_chunk=bool((body.get("stream_options") or {}).get("include_usage")),
        raw_hash=hashlib.sha256(canonical.encode()).hexdigest(),
    )


# ------------------------------------------------------------------------------------------- engine
@dataclass
class Attempt:
    provider: str
    model: str
    connection_id: str
    outcome: str
    category: str | None = None
    latency_ms: int | None = None
    message: str | None = None
    retry_in_s: float | None = None

    def public(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "outcome": self.outcome,
            "category": self.category,
            "latency_ms": self.latency_ms,
        }


@dataclass
class RequestState:
    request_id: str
    project: Project
    api_key_id: str
    parsed: ParsedRequest
    plan: RoutePlan | None = None
    attempts: list[Attempt] = field(default_factory=list)
    exclude: set[tuple[str, str]] = field(default_factory=set)
    # provider id -> the connection whose account failed. Other accounts of that provider are not tried for
    # the rest of the request (no account hopping); other models on the same connection still may be.
    excluded_providers: dict[str, str] = field(default_factory=dict)
    served: Candidate | None = None

    def meta(self) -> dict[str, Any]:
        served = self.served
        return {
            "request_id": self.request_id,
            "provider": served.provider_id if served else None,
            "model": served.upstream_model if served else None,
            "fallback": self.fallback_used,
            "downgraded": bool(served and self.plan and self.plan.is_downgrade(served)),
            "attempts": [a.public() for a in self.attempts],
        }

    @property
    def fallback_used(self) -> bool:
        return any(a.outcome != "success" for a in self.attempts if a.outcome != "skipped")


@dataclass
class Dispatch:
    cand: Candidate
    reservation_id: str
    pool_ids: list[str]
    started: float
    probe: bool


class Engine:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    # -- helpers -------------------------------------------------------------------------------
    def _max_hops(self, project: Project) -> int:
        return (
            project.max_fallback_hops
            if project.max_fallback_hops is not None
            else self.ctx.settings.max_fallback_hops
        )

    async def _plan(self, st: RequestState) -> RoutePlan:
        async with self.ctx.db.session() as s:
            project = await s.get(Project, st.project.id)
            assert project is not None
            st.project = project
            return await build_plan(s, project, st.parsed.options, exclude=st.exclude)

    def _skip(self, st: RequestState, c: Candidate) -> str | None:
        if (c.connection_id, c.upstream_model) in st.exclude or (c.connection_id, "*") in st.exclude:
            return "excluded_after_failure"
        failed_conn = st.excluded_providers.get(c.provider_id)
        if failed_conn is not None and failed_conn != c.connection_id:
            return "provider_excluded_after_account_failure"
        return None

    async def _prepare(self, st: RequestState, c: Candidate, attempt_no: int) -> Dispatch | None:
        """Claim a probe if needed and reserve quota. Returns None (with an attempt recorded) if unusable."""
        if c.needs_probe and not await breaker.claim_probe(self.ctx.db, c.connection_id, c.upstream_model):
            st.attempts.append(
                Attempt(c.provider_id, c.upstream_model, c.connection_id, "skipped", "probe_in_flight")
            )
            return None
        async with self.ctx.db.session() as s:
            pools = await ledger.pools_for(s, st.project.id, c.provider_id, c.account_scope, c.upstream_model)
        o = st.parsed.options
        try:
            res = await ledger.reserve(
                self.ctx.db,
                idempotency_key=f"{st.request_id}:{attempt_no}",
                request_id=st.request_id,
                project_id=st.project.id,
                connection_id=c.connection_id,
                model_id=c.model_id,
                pools=pools,
                demand=ledger.Demand(input_tokens=o.input_tokens, output_tokens=o.max_output_tokens),
                allow_unknown=st.project.allow_unknown_quota,
                ttl_s=self.ctx.settings.reservation_ttl_s,
            )
        except ledger.QuotaUnavailable as exc:
            self.ctx.metrics.reservations.labels("rejected").inc()
            if c.needs_probe:
                await breaker.release_probe(self.ctx.db, c.connection_id, c.upstream_model)
            retry_in = (exc.retry_at - utcnow()).total_seconds() if exc.retry_at else None
            st.attempts.append(
                Attempt(
                    c.provider_id,
                    c.upstream_model,
                    c.connection_id,
                    "skipped",
                    f"quota:{exc.reason}",
                    retry_in_s=retry_in,
                )
            )
            return None
        self.ctx.metrics.reservations.labels("reserved").inc()
        return Dispatch(c, res.id, [p.id for p in pools], time.perf_counter(), c.needs_probe)

    async def _target(self, c: Candidate) -> conns.CallTarget:
        async with self.ctx.db.session() as s:
            conn = await s.get(Connection, c.connection_id)
            assert conn is not None
            return await conns.load_target(self.ctx, s, conn)

    async def _on_success(
        self,
        st: RequestState,
        d: Dispatch,
        usage: Usage | None,
        observations: list[Any],
        *,
        output_estimate: int | None = None,
        streamed: bool = False,
    ) -> None:
        c = d.cand
        latency = (time.perf_counter() - d.started) * 1000
        await ledger.settle(self.ctx.db, d.reservation_id, usage, output_estimate=output_estimate)
        await ledger.apply_observations(
            self.ctx.db,
            project_id=st.project.id,
            provider_id=c.provider_id,
            account_scope=c.account_scope,
            upstream_model=c.upstream_model,
            observations=observations,
        )
        await ledger.clear_recheck(self.ctx.db, d.pool_ids)
        await breaker.record_success(self.ctx.db, c.connection_id, c.upstream_model, latency)
        st.attempts.append(
            Attempt(c.provider_id, c.upstream_model, c.connection_id, "success", None, int(latency))
        )
        st.served = c
        reported = bool(usage and usage.provider_reported)
        in_tok = usage.input_tokens if reported and usage else st.parsed.options.input_tokens
        out_tok = usage.output_tokens if reported and usage else (output_estimate or 0)
        await self._usage(st, c, in_tok, out_tok, reported, "success", None, int(latency), streamed)
        m = self.ctx.metrics
        m.upstream_attempts.labels(c.provider_id, "success").inc()
        m.upstream_latency.labels(c.provider_id).observe(latency / 1000)
        m.tokens.labels(c.provider_id, "input", str(reported).lower()).inc(in_tok)
        m.tokens.labels(c.provider_id, "output", str(reported).lower()).inc(out_tok)

    async def _on_failure(
        self, st: RequestState, d: Dispatch, err: UpstreamError, *, streamed: bool = False
    ) -> None:
        c = d.cand
        latency = int((time.perf_counter() - d.started) * 1000)
        cat = err.category
        await ledger.release(self.ctx.db, d.reservation_id, count_request=err.status is not None)
        if err.observations:
            await ledger.apply_observations(
                self.ctx.db,
                project_id=st.project.id,
                provider_id=c.provider_id,
                account_scope=c.account_scope,
                upstream_model=c.upstream_model,
                observations=err.observations,
            )
        if cat is ErrorCategory.QUOTA_EXHAUSTED:
            until = None
            if err.retry_after_s:
                until = utcnow() + timedelta(seconds=err.retry_after_s)
            await ledger.mark_exhausted(self.ctx.db, d.pool_ids, until)
            if not d.pool_ids:
                # No local pool knows this allocation: cool the connection/model down instead.
                await breaker.record_failure(
                    self.ctx.db,
                    self.ctx.settings,
                    c.connection_id,
                    c.upstream_model,
                    ErrorCategory.RATE_LIMITED,
                    err.retry_after_s or self.ctx.settings.breaker_max_cooldown_s,
                )
        elif cat in (ErrorCategory.AUTH_INVALID, ErrorCategory.AUTH_EXPIRED):
            await conns.suspend_for_auth(self.ctx, c.connection_id, cat, err.message)
        elif cat is ErrorCategory.CANCELLED and d.probe:
            await breaker.release_probe(self.ctx.db, c.connection_id, c.upstream_model)
        rec = await breaker.record_failure(
            self.ctx.db, self.ctx.settings, c.connection_id, c.upstream_model, cat, err.retry_after_s
        )
        if rec is None and d.probe:
            await breaker.release_probe(self.ctx.db, c.connection_id, c.upstream_model)
        # Exclusions for the remainder of this request.
        st.exclude.add((c.connection_id, "*" if cat in _CONNECTION_WIDE else c.upstream_model))
        if cat in _ACCOUNT_SCOPED and not c.multi_account_permitted:
            st.excluded_providers.setdefault(c.provider_id, c.connection_id)
        st.attempts.append(
            Attempt(
                c.provider_id,
                c.upstream_model,
                c.connection_id,
                "error",
                cat.value,
                latency,
                err.message,
                err.retry_after_s,
            )
        )
        await self._usage(st, c, 0, 0, False, "error", cat.value, latency, streamed)
        self.ctx.metrics.upstream_attempts.labels(c.provider_id, cat.value).inc()

    async def _usage(
        self,
        st: RequestState,
        c: Candidate,
        in_tok: int,
        out_tok: int,
        reported: bool,
        outcome: str,
        category: str | None,
        latency_ms: int,
        streamed: bool,
    ) -> None:
        async with self.ctx.db.transaction() as s:
            s.add(
                UsageRecord(
                    request_id=st.request_id,
                    attempt=len(st.attempts),
                    project_id=st.project.id,
                    api_key_id=st.api_key_id,
                    connection_id=c.connection_id,
                    provider_id=c.provider_id,
                    model_id=c.model_id,
                    upstream_model=c.upstream_model,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    provider_reported=reported,
                    outcome=outcome,
                    error_category=category,
                    latency_ms=latency_ms,
                    streamed=streamed,
                )
            )

    async def _decision(self, st: RequestState, outcome: str) -> None:
        plan = st.plan
        served = st.served
        try:
            async with self.ctx.db.transaction() as s:
                s.add(
                    RouteDecision(
                        request_id=st.request_id,
                        project_id=st.project.id,
                        requested_model=st.parsed.options.requested_model[:200],
                        task=st.parsed.options.task.value,
                        mode=st.parsed.options.mode.value,
                        candidates=[c.describe() for c in (plan.candidates if plan else [])][:20],
                        rejected=[r.describe() for r in (plan.rejected if plan else [])][:50],
                        attempts=[{**a.public(), "connection_id": a.connection_id} for a in st.attempts],
                        served_model=served.upstream_model if served else None,
                        served_provider=served.provider_id if served else None,
                        fallback_used=st.fallback_used,
                        downgraded=bool(served and plan and plan.is_downgrade(served)),
                        outcome=outcome,
                    )
                )
        except IntegrityError:  # duplicate request id (client-supplied x-request-id reuse)
            log.warning("duplicate route decision", extra={"request_id": st.request_id})
        self.ctx.metrics.gateway_requests.labels(outcome).inc()
        if outcome == "success" and st.fallback_used:
            first_err = next((a.category for a in st.attempts if a.outcome != "success"), "unknown")
            self.ctx.metrics.fallbacks.labels(first_err or "unknown").inc()

    def _no_route(self, st: RequestState) -> GatewayError:
        plan = st.plan
        rejected = plan.rejected if plan else []
        reasons: dict[str, int] = {}
        for r in rejected:
            reasons[r.reason] = reasons.get(r.reason, 0) + 1
        for a in st.attempts:
            key = a.category or a.outcome
            reasons[key] = reasons.get(key, 0) + 1
        meta = {**st.meta(), "rejections": reasons}
        if any(r.reason == "model_not_found_in_authorized_connections" for r in rejected):
            return GatewayError(
                404,
                "model_not_found",
                f"model '{st.parsed.options.requested_model}' is not available through this "
                "project's connections",
                type_="invalid_request_error",
                meta=meta,
                param="model",
            )
        errs = [a for a in st.attempts if a.outcome == "error"]
        if errs and errs[-1].category in {c.value for c in STOP_CATEGORIES}:
            last = errs[-1]
            return GatewayError(
                400 if last.category == ErrorCategory.POLICY_REJECTED.value else 499,
                last.category.lower() if last.category else "rejected",
                f"provider rejected the request: {last.message}",
                type_="provider_policy_error",
                meta=meta,
            )
        quota_like = {"quota_exhausted", "QUOTA_EXHAUSTED", "RATE_LIMITED", "health:circuit_open"}
        only_capacity = bool(reasons) and all(k in quota_like or k.startswith("quota") for k in reasons)
        if only_capacity:
            return GatewayError(
                429,
                "capacity_exhausted",
                "all eligible capacity is exhausted or rate limited; retry after reset",
                type_="rate_limit_error",
                meta=meta,
                retry_after_s=self._retry_after(st),
            )
        if errs:
            return GatewayError(
                502,
                "upstream_failed",
                "all eligible upstream attempts failed",
                type_="upstream_error",
                meta=meta,
            )
        return GatewayError(
            503,
            "no_eligible_route",
            "no authorized, eligible, healthy candidate satisfies this request",
            type_="no_route_error",
            meta=meta,
        )

    def _retry_after(self, st: RequestState) -> float | None:
        """Earliest known moment any rejected/failed candidate may have capacity again."""
        hints = [r.retry_in_s for r in (st.plan.rejected if st.plan else []) if r.retry_in_s]
        hints += [a.retry_in_s for a in st.attempts if a.retry_in_s]
        positive = [h for h in hints if h is not None and h > 0]
        if positive:
            return min(positive)
        return 1.0 if st.attempts else None

    # -- public API ------------------------------------------------------------------------------
    async def begin(self, st: RequestState) -> None:
        if st.project.kill_switch:
            raise GatewayError(
                503, "project_disabled", "this project's gateway is disabled by its kill switch"
            )
        with tracer.start_as_current_span("oasis.route.plan"):
            st.plan = await self._plan(st)

    async def complete(self, st: RequestState) -> ChatResult:
        assert st.plan is not None
        hops = self._max_hops(st.project)
        dispatched = 0
        for attempt_no, c in enumerate(st.plan.candidates):
            if dispatched > hops:
                break
            if self._skip(st, c) is not None:
                continue
            d = await self._prepare(st, c, attempt_no)
            if d is None:
                continue
            dispatched += 1
            with tracer.start_as_current_span("oasis.upstream.chat") as span:
                span.set_attribute("gen_ai.system", c.provider_id)
                span.set_attribute("gen_ai.request.model", c.upstream_model)
                try:
                    target = await self._target(c)
                    adapter = self.ctx.adapters.for_provider(target.provider_id, target.adapter_key)
                    cctx = await conns.connection_context(self.ctx, target)
                    req = _with_model(st.parsed.chat, c.upstream_model, stream=False)
                    result = await adapter.invoke(cctx, req)
                except UpstreamError as err:
                    span.set_attribute("error.type", err.category.value)
                    await self._on_failure(st, d, err)
                    if err.category in STOP_CATEGORIES:
                        break
                    continue
                except BaseException:
                    await ledger.release(self.ctx.db, d.reservation_id)
                    raise
                span.set_attribute("gen_ai.usage.input_tokens", result.usage.input_tokens)
                span.set_attribute("gen_ai.usage.output_tokens", result.usage.output_tokens)
            out_est = None if result.usage.provider_reported else _estimate_output(result.payload)
            await self._on_success(st, d, result.usage, result.observations, output_estimate=out_est)
            await self._decision(st, "success")
            return result
        failure = self._no_route(st)
        await self._decision(st, "failed")
        raise failure

    async def open_stream(self, st: RequestState) -> tuple[Dispatch, StreamHandle, StreamChunk | None]:
        """Open an upstream stream and read its first chunk (fallback still possible until this returns)."""
        assert st.plan is not None
        hops = self._max_hops(st.project)
        dispatched = 0
        for attempt_no, c in enumerate(st.plan.candidates):
            if dispatched > hops:
                break
            if self._skip(st, c) is not None:
                continue
            d = await self._prepare(st, c, attempt_no)
            if d is None:
                continue
            dispatched += 1
            handle: StreamHandle | None = None
            span = tracer.start_span("oasis.upstream.chat_stream")
            span.set_attribute("gen_ai.system", c.provider_id)
            span.set_attribute("gen_ai.request.model", c.upstream_model)
            try:
                target = await self._target(c)
                adapter = self.ctx.adapters.for_provider(target.provider_id, target.adapter_key)
                cctx = await conns.connection_context(self.ctx, target)
                handle = await adapter.stream(
                    cctx, _with_model(st.parsed.chat, c.upstream_model, stream=True)
                )
                first: StreamChunk | None
                try:
                    first = await handle.chunks.__anext__()
                except StopAsyncIteration:
                    first = None
                return d, handle, first
            except UpstreamError as err:
                span.set_attribute("error.type", err.category.value)
                if handle is not None:
                    await handle.close()
                await self._on_failure(st, d, err, streamed=True)
                if err.category in STOP_CATEGORIES:
                    break
                continue
            except BaseException:
                if handle is not None:
                    await handle.close()
                await ledger.release(self.ctx.db, d.reservation_id)
                raise
            finally:
                span.end()  # covers dispatch and first chunk (time to first token)
        err_out = self._no_route(st)
        await self._decision(st, "failed")
        raise err_out

    async def relay(
        self, st: RequestState, d: Dispatch, handle: StreamHandle, first: StreamChunk | None
    ) -> AsyncIterator[bytes]:
        """SSE relay. Settlement always runs, shielded from client-disconnect cancellation."""
        usage: Usage | None = None
        chars = 0
        outcome = "success"
        error: UpstreamError | None = None
        meta_sent = False
        try:
            chunk: StreamChunk | None = first
            iterator = handle.chunks
            while chunk is not None:
                chars += chunk.text_chars
                if chunk.usage is not None:
                    usage = chunk.usage
                data = chunk.data
                if data is not None:
                    is_usage_only = not data.get("choices") and data.get("usage") is not None
                    if not is_usage_only or st.parsed.include_usage_chunk:
                        if not meta_sent:
                            data = {
                                **data,
                                "oasis_route": {
                                    "provider": d.cand.provider_id,
                                    "model": d.cand.upstream_model,
                                },
                            }
                            meta_sent = True
                        yield f"data: {json.dumps(data, separators=(',', ':'))}\n\n".encode()
                try:
                    chunk = await iterator.__anext__()
                except StopAsyncIteration:
                    chunk = None
            yield b"data: [DONE]\n\n"
        except UpstreamError as exc:
            outcome, error = "error", exc
            payload = {
                "error": {"message": exc.message, "type": "upstream_error", "code": exc.category.value}
            }
            yield f"data: {json.dumps(payload)}\n\n".encode()
            yield b"data: [DONE]\n\n"
        except BaseException:
            outcome = "cancelled"
            raise
        finally:
            with anyio.CancelScope(shield=True):
                await handle.close()
                out_est = math.ceil(chars / 4) if chars else 0
                try:
                    if outcome == "success":
                        await self._on_success(
                            st,
                            d,
                            usage,
                            handle.observations,
                            output_estimate=None if usage else out_est,
                            streamed=True,
                        )
                        await self._decision(st, "success")
                    elif outcome == "error" and error is not None:
                        # Bytes already reached the client: charge what was produced; record health failure.
                        await ledger.settle(self.ctx.db, d.reservation_id, usage, output_estimate=out_est)
                        await breaker.record_failure(
                            self.ctx.db,
                            self.ctx.settings,
                            d.cand.connection_id,
                            d.cand.upstream_model,
                            error.category,
                            error.retry_after_s,
                        )
                        st.attempts.append(
                            Attempt(
                                d.cand.provider_id,
                                d.cand.upstream_model,
                                d.cand.connection_id,
                                "error",
                                error.category.value,
                                None,
                                error.message,
                            )
                        )
                        st.served = d.cand
                        await self._decision(st, "stream_error")
                    else:
                        await ledger.settle(self.ctx.db, d.reservation_id, usage, output_estimate=out_est)
                        if d.probe:
                            await breaker.release_probe(
                                self.ctx.db, d.cand.connection_id, d.cand.upstream_model
                            )
                        st.attempts.append(
                            Attempt(
                                d.cand.provider_id,
                                d.cand.upstream_model,
                                d.cand.connection_id,
                                "cancelled",
                                ErrorCategory.CANCELLED.value,
                            )
                        )
                        st.served = d.cand
                        await self._usage(
                            st,
                            d.cand,
                            st.parsed.options.input_tokens,
                            out_est,
                            False,
                            "cancelled",
                            ErrorCategory.CANCELLED.value,
                            0,
                            True,
                        )
                        await self._decision(st, "cancelled")
                except Exception:  # pragma: no cover - accounting must never crash the response path
                    log.exception("stream settlement failed", extra={"request_id": st.request_id})


def _with_model(req: ChatRequest, upstream_model: str, *, stream: bool) -> ChatRequest:
    from dataclasses import replace

    return replace(req, model=upstream_model, stream=stream)


def _estimate_output(payload: dict[str, Any]) -> int:
    chars = 0
    for ch in payload.get("choices") or []:
        msg = ch.get("message") or {}
        chars += len(str(msg.get("content") or "")) + len(str(msg.get("tool_calls") or ""))
    return math.ceil(chars / 4)


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex}"


# ------------------------------------------------------------------------------------------- idempotency
@dataclass
class IdemHit:
    status: int
    body: dict[str, Any]


async def idem_begin(ctx: AppContext, project_id: str, key: str, request_hash: str) -> IdemHit | None:
    """Reserve an idempotency key.

    Returns a stored response to replay, or None if this caller now owns the key.
    """
    if len(key) > 200:
        raise GatewayError(
            400, "invalid_idempotency_key", "Idempotency-Key too long", type_="invalid_request_error"
        )
    try:
        async with ctx.db.transaction() as s:
            s.add(IdempotencyRecord(project_id=project_id, key=key, request_hash=request_hash))
        return None
    except IntegrityError:
        pass
    async with ctx.db.session() as s:
        rec = (
            await s.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.project_id == project_id, IdempotencyRecord.key == key
                )
            )
        ).scalar_one()
    if rec.request_hash != request_hash:
        raise GatewayError(
            409,
            "idempotency_key_reused",
            "Idempotency-Key was used with a different request body",
            type_="invalid_request_error",
        )
    if rec.response is None or rec.status_code is None:
        raise GatewayError(
            409, "idempotency_in_progress", "a request with this Idempotency-Key is in progress"
        )
    return IdemHit(rec.status_code, rec.response)


async def idem_finish(
    ctx: AppContext, project_id: str, key: str, status: int, body: dict[str, Any] | None
) -> None:
    async with ctx.db.transaction() as s:
        rec = (
            await s.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.project_id == project_id, IdempotencyRecord.key == key
                )
            )
        ).scalar_one_or_none()
        if rec is None:
            return
        if body is None:
            await s.delete(rec)  # transient failure: allow the client to retry with the same key
        else:
            rec.status_code = status
            rec.response = body
