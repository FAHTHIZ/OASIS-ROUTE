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
from sqlalchemy.ext.asyncio import AsyncSession

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
    PaidApproval,
    Project,
    RequestReceipt,
    RouteDecision,
    UsageRecord,
    utcnow,
)
from oasis_route.domain.enums import ErrorCategory, RoutingMode, Task
from oasis_route.domain.errors import STOP_CATEGORIES, UpstreamError
from oasis_route.health import breaker
from oasis_route.identity import audit
from oasis_route.logging import get_logger
from oasis_route.quota import ledger
from oasis_route.routing.cost import CostEstimate
from oasis_route.routing.explain import explain
from oasis_route.routing.router import (
    SPEND_BOUNDED,
    SPEND_FREE_ONLY,
    SPEND_LABELS,
    Candidate,
    Rejection,
    RouteOptions,
    RoutePlan,
    build_plan,
    route_cost,
    spend_mode,
)
from oasis_route.spend import approvals, budgets, receipts
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
    approve_paid: str | None = None  # bounded_autonomy single-use approval token


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
    approve = ext.get("approve_paid")
    if approve is not None and not (isinstance(approve, str) and 0 < len(approve) <= 200):
        raise GatewayError(
            400,
            "invalid_request",
            "'oasis_route.approve_paid' must be a token string",
            type_="invalid_request_error",
        )
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return ParsedRequest(
        chat=chat,
        options=opts,
        include_usage_chunk=bool((body.get("stream_options") or {}).get("include_usage")),
        raw_hash=hashlib.sha256(canonical.encode()).hexdigest(),
        approve_paid=approve,
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
    retry_source: str | None = None  # provider (Retry-After / reset header) | cooldown (local breaker)

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "provider": self.provider,
            "model": self.model,
            "outcome": self.outcome,
            "category": self.category,
            "latency_ms": self.latency_ms,
        }
        if self.retry_in_s is not None:
            out["retry_in_s"] = round(self.retry_in_s, 1)
            out["retry_source"] = self.retry_source or "quota"
        return out


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
    actual_cost: CostEstimate | None = None
    approval_id: str | None = None
    reservation_ids: list[str] = field(default_factory=list)
    held_usd_micros: int = 0
    tokens: tuple[int, int] | None = None
    usage_source: str = "none"
    streamed: bool = False

    def spend(self) -> dict[str, Any]:
        """Spend mode and, when a paid route served, a clear label with cost estimate and reason."""
        plan, served = self.plan, self.served
        policy = plan.spend_mode if plan else SPEND_FREE_ONLY
        out: dict[str, Any] = {"mode": SPEND_LABELS.get(policy, policy), "policy": policy, "paid": False}
        if served is None or served.cost_status == "free":
            return out
        failed_free = [a for a in self.attempts if a.outcome != "success"]
        reason = (
            "free routes failed or were skipped: "
            + ", ".join(sorted({a.category or a.outcome for a in failed_free}))
            if failed_free
            else "no free route passed the routing gates"
        )
        micros = served.est_cost_usd_micros
        out.update(
            paid=True,
            label="PAID FALLBACK" if policy != SPEND_FREE_ONLY else "PAID",
            provider=served.provider_id,
            model=served.upstream_model,
            cost_status=served.cost_status,
            estimated_cost_usd=None if micros is None else micros / 1_000_000,
            price_verified=served.price_verified,
            authorized_by="single_use_approval" if self.approval_id else "project_spend_policy",
            reason=reason,
        )
        return out

    def explanation(self) -> dict[str, Any]:
        plan = self.plan
        desc = (
            plan.describe() if plan else {"candidates": [], "rejected": [], "mode": self.parsed.options.mode}
        )
        return explain(
            desc,
            pinned=bool(plan and plan.pinned),
            attempts=[a.public() for a in self.attempts],
            served=self.served.describe() if self.served else None,
            actual_cost=self.actual_cost,
            spend=self.spend(),
        )

    def meta(self) -> dict[str, Any]:
        served = self.served
        ex = self.explanation()
        sel = ex.get("selected") or {}
        return {
            "request_id": self.request_id,
            "provider": served.provider_id if served else None,
            "model": served.upstream_model if served else None,
            "fallback": self.fallback_used,
            "downgraded": bool(served and self.plan and self.plan.is_downgrade(served)),
            "attempts": [a.public() for a in self.attempts],
            "spend": self.spend(),
            "cooldowns": [
                {
                    "provider": a.provider,
                    "model": a.model,
                    "retry_in_s": round(a.retry_in_s, 1),
                    "category": a.category,
                }
                for a in self.attempts
                if a.retry_in_s is not None
            ],
            "route": {
                "strategy": ex["strategy"],
                "reason": sel.get("reason"),
                "cost_basis": sel.get("cost", {}).get("cost_basis"),
                "free": (ex.get("verification") or {}).get("free"),
                "estimated_cost_usd": (ex.get("estimated_cost") or {}).get("usd"),
                "actual_cost_usd": (ex.get("actual_cost") or {}).get("usd"),
                "fallback_chain": ex["fallback_chain"]["planned"][:5],
                "explain": f"/api/v1/projects/{self.project.id}/route-decisions/{self.request_id}",
            },
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
    held_usd_micros: int = 0  # worst-case cost held against project spend budgets


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
        approval: PaidApproval | None = None
        async with self.ctx.db.session() as s:
            project = await s.get(Project, st.project.id)
            assert project is not None
            st.project = project
            token = st.parsed.approve_paid
            if token:
                if spend_mode(project) != SPEND_BOUNDED or st.parsed.options.free_only:
                    raise GatewayError(
                        400,
                        "approval_not_applicable",
                        "paid approvals apply only to bounded_autonomy projects, not to free_only requests",
                        type_="spend_policy_error",
                    )
                found = await approvals.find(s, project.id, token)
                if found.problem or found.approval is None:
                    raise GatewayError(
                        403,
                        found.problem or "approval_unknown",
                        "the paid-route approval is unknown, already used or expired; nothing was charged",
                        type_="spend_policy_error",
                    )
                approval = found.approval
                st.parsed.options.approved_paid = (approval.connection_id, approval.upstream_model)
            plan = await build_plan(s, project, st.parsed.options, exclude=st.exclude)
        if approval is not None:
            await self._apply_approval(st, plan, approval)
        return plan

    async def _apply_approval(self, st: RequestState, plan: RoutePlan, approval: PaidApproval) -> None:
        key = (approval.connection_id, approval.upstream_model)
        cand = next((c for c in plan.candidates if (c.connection_id, c.upstream_model) == key), None)
        if cand is None:
            return  # the approved route did not pass the gates now; the plan explains why
        problem: str | None = None
        cap = approval.max_cost_usd_micros
        if cap is not None and (cand.est_cost_usd_micros is None or cand.est_cost_usd_micros > cap):
            problem = "approval_cost_exceeded"
        else:
            async with self.ctx.db.transaction() as s:
                if await approvals.consume(s, approval.id, st.request_id):
                    st.approval_id = approval.id
                    await audit.record(
                        s,
                        "spend.paid_approval_used",
                        actor_key_id=st.api_key_id,
                        project_id=st.project.id,
                        target_type="paid_approval",
                        target_id=approval.id,
                        details={
                            "request_id": st.request_id,
                            "provider": cand.provider_id,
                            "model": cand.upstream_model,
                            "estimated_cost_usd_micros": cand.est_cost_usd_micros,
                        },
                    )
                else:
                    problem = "approval_used"
        if problem:
            plan.candidates.remove(cand)
            plan.rejected.append(
                Rejection(cand.provider_id, cand.upstream_model, cand.connection_id, problem)
            )

    async def _failure(self, st: RequestState) -> GatewayError:
        """The terminal error. Under bounded_autonomy, offer single-use approvals for paid routes."""
        base = self._no_route(st)
        plan = st.plan
        if (
            plan is None
            or plan.spend_mode != SPEND_BOUNDED
            or not plan.approval_candidates
            or base.code == "model_not_found"
            or base.type == "provider_policy_error"
        ):
            return base
        why = ", ".join(sorted(base.meta.get("rejections", {}))) or "no free route"
        reason = f"no free route remained ({why})"
        options: list[dict[str, Any]] = []
        async with self.ctx.db.transaction() as s:
            for c in plan.approval_candidates:
                token, row = await approvals.issue(
                    s,
                    project_id=st.project.id,
                    request_id=st.request_id,
                    connection_id=c.connection_id,
                    provider_id=c.provider_id,
                    upstream_model=c.upstream_model,
                    max_cost_usd_micros=c.est_cost_usd_micros,
                    cost_status=c.cost_status,
                    reason=reason,
                )
                options.append(approvals.option(token, row, price_verified=c.price_verified))
            await audit.record(
                s,
                "spend.paid_approval_requested",
                actor_key_id=st.api_key_id,
                project_id=st.project.id,
                details={
                    "request_id": st.request_id,
                    "reason": reason,
                    "options": [
                        {k: o[k] for k in ("provider", "model", "cost_status", "estimated_cost_usd_micros")}
                        for o in options
                    ],
                },
            )
        meta = {
            **base.meta,
            "approval": {"spend_mode": "bounded_autonomy", "reason": reason, "options": options},
        }
        return GatewayError(
            402,
            "paid_approval_required",
            "No free route remains for this request and this project requires approval before any paid "
            "route is used. Nothing was charged. To proceed, resend the same request with "
            "oasis_route.approve_paid set to one of the approval tokens in oasis_route.approval.options.",
            type_="spend_policy_error",
            meta=meta,
        )

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
            project_budgets = await budgets.for_project(s, st.project.id)
        o = st.parsed.options
        # Worst-case USD cost of this attempt: free routes hold nothing; unknown cost is None (never zero).
        worst = 0 if c.cost_status == "free" else c.est_cost_usd_micros
        try:
            res = await ledger.reserve(
                self.ctx.db,
                idempotency_key=f"{st.request_id}:{attempt_no}",
                request_id=st.request_id,
                project_id=st.project.id,
                connection_id=c.connection_id,
                model_id=c.model_id,
                pools=pools,
                demand=ledger.Demand(
                    input_tokens=o.input_tokens, output_tokens=o.max_output_tokens, credits_micros=worst
                ),
                allow_unknown=st.project.allow_unknown_quota,
                ttl_s=self.ctx.settings.reservation_ttl_s,
                budgets=project_budgets,
                budget_micros=worst,
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
                    f"budget:{exc.reason}" if exc.unit == ledger.BUDGET_UNIT else f"quota:{exc.reason}",
                    retry_in_s=retry_in,
                )
            )
            return None
        self.ctx.metrics.reservations.labels("reserved").inc()
        st.reservation_ids.append(res.id)
        return Dispatch(
            c, res.id, [p.id for p in pools], time.perf_counter(), c.needs_probe, res.budget_usd_micros
        )

    async def _target(self, c: Candidate) -> conns.CallTarget:
        async with self.ctx.db.session() as s:
            conn = await s.get(Connection, c.connection_id)
            assert conn is not None
            return await conns.load_target(self.ctx, s, conn)

    @staticmethod
    def _cost(
        st: RequestState, c: Candidate, usage: Usage | None, output_estimate: int | None
    ) -> CostEstimate:
        """Actual cost from settled usage; without provider usage, the input estimate and measured output."""
        reported = bool(usage and usage.provider_reported)
        in_tok = usage.input_tokens if reported and usage else st.parsed.options.input_tokens
        if reported and usage:
            out_tok = usage.output_tokens
        elif output_estimate is not None:
            out_tok = output_estimate
        else:  # nothing measured: keep the worst case
            out_tok = st.parsed.options.max_output_tokens
        return route_cost(c.cost_basis, c.pricing, in_tok, out_tok)

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
        st.actual_cost = self._cost(st, c, usage, output_estimate)
        await ledger.settle(
            self.ctx.db,
            d.reservation_id,
            usage,
            output_estimate=output_estimate,
            cost_micros=st.actual_cost.usd_micros,
        )
        st.held_usd_micros = d.held_usd_micros
        st.streamed = streamed
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
        st.tokens = (in_tok, out_tok)
        st.usage_source = "provider" if reported else "estimate"
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
        retry_in = err.retry_after_s
        if retry_in is None and rec is not None and rec.open_until is not None:
            retry_in = max(0.0, (rec.open_until - utcnow()).total_seconds())
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
                retry_in,
                retry_source="provider"
                if err.retry_after_s is not None
                else ("cooldown" if retry_in else None),
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
                        explanation=st.explanation(),
                    )
                )
                s.add(await self._receipt(s, st, outcome))
        except IntegrityError:  # duplicate request id (client-supplied x-request-id reuse)
            log.warning("duplicate route decision", extra={"request_id": st.request_id})
        self.ctx.metrics.gateway_requests.labels(outcome).inc()
        if outcome == "success" and st.fallback_used:
            first_err = next((a.category for a in st.attempts if a.outcome != "success"), "unknown")
            self.ctx.metrics.fallbacks.labels(first_err or "unknown").inc()

    async def _receipt(self, s: AsyncSession, st: RequestState, outcome: str) -> RequestReceipt:
        served = st.served
        est = served.est_cost_usd_micros if served else None
        if served is not None and served.cost_status == "free":
            est = 0
        actual = st.actual_cost.usd_micros if st.actual_cost else None
        held = st.held_usd_micros
        rows = await budgets.for_project(s, st.project.id)
        now = utcnow()
        return receipts.build(
            request_id=st.request_id,
            project_id=st.project.id,
            api_key_id=st.api_key_id,
            outcome=outcome,
            spend_mode=st.spend()["mode"],
            provider_id=served.provider_id if served else None,
            upstream_model=served.upstream_model[:200] if served else None,
            connection_id=served.connection_id if served else None,
            cost_basis=served.cost_basis if served else None,
            cost_status=(st.actual_cost.status if st.actual_cost else served.cost_status) if served else None,
            price_verified=bool(served and served.price_verified),
            estimated_usd_micros=est,
            reserved_usd_micros=held,
            actual_usd_micros=actual,
            drift_usd_micros=(actual - held) if (actual is not None and held) else None,
            input_tokens=st.tokens[0] if st.tokens else None,
            output_tokens=st.tokens[1] if st.tokens else None,
            usage_source=st.usage_source,
            streamed=st.streamed,
            attempts=len(st.attempts),
            fallback_used=st.fallback_used,
            approval_id=st.approval_id,
            reservation_ids=list(st.reservation_ids),
            budgets_after=[
                {
                    "period": budgets.STORED.get(b.period, b.period),
                    "remaining_usd_micros": budgets.remaining(b, now),
                }
                for b in rows
            ],
        )

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
        failure = await self._failure(st)
        await self._decision(
            st, "approval_required" if failure.code == "paid_approval_required" else "failed"
        )
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
        err_out = await self._failure(st)
        await self._decision(
            st, "approval_required" if err_out.code == "paid_approval_required" else "failed"
        )
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
                                    "request_id": st.request_id,
                                    "provider": d.cand.provider_id,
                                    "model": d.cand.upstream_model,
                                    "fallback": st.fallback_used,
                                    "spend": st.spend(),
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
                        st.actual_cost = self._cost(st, d.cand, usage, out_est)
                        st.held_usd_micros, st.streamed = d.held_usd_micros, True
                        await ledger.settle(
                            self.ctx.db,
                            d.reservation_id,
                            usage,
                            output_estimate=out_est,
                            cost_micros=st.actual_cost.usd_micros,
                        )
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
                        st.actual_cost = self._cost(st, d.cand, usage, out_est)
                        st.held_usd_micros, st.streamed = d.held_usd_micros, True
                        await ledger.settle(
                            self.ctx.db,
                            d.reservation_id,
                            usage,
                            output_estimate=out_est,
                            cost_micros=st.actual_cost.usd_micros,
                        )
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
