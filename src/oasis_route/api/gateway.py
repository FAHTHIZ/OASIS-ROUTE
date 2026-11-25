"""Unified gateway: OpenAI ``POST /v1/chat/completions`` and ``POST /v1/responses``, Anthropic
``POST /v1/messages``, ``GET /v1/models``.

Authenticate with an Oasis Key: ``Authorization: Bearer $OASIS_API_KEY`` (or ``x-api-key``).
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select

from oasis_route.adapters.base import estimate_tokens_from_messages
from oasis_route.api.deps import Principal, get_ctx, require_scope
from oasis_route.context import AppContext
from oasis_route.db.models import Connection, Model
from oasis_route.domain.enums import ApiKeyScope, ConnectionStatus, Task
from oasis_route.gateway import anthropic_compat as ac
from oasis_route.gateway import responses_compat as rc
from oasis_route.gateway.engine import (
    Engine,
    GatewayError,
    RequestState,
    idem_begin,
    idem_finish,
    parse_body,
)
from oasis_route.registry.imported import UNROUTABLE_LIFECYCLES
from oasis_route.routing.priors import model_prior
from oasis_route.routing.router import quality_of

router = APIRouter(prefix="/v1", tags=["gateway"])


def _headers(st: RequestState) -> dict[str, str]:
    m = st.meta()
    return {
        "x-oasis-route-provider": str(m["provider"] or ""),
        "x-oasis-route-model": str(m["model"] or ""),
        "x-oasis-route-fallback": "true" if m["fallback"] else "false",
        "x-oasis-route-downgraded": "true" if m["downgraded"] else "false",
        "x-oasis-route-attempts": str(len(m["attempts"])),
        "x-oasis-route-spend-mode": str(m["spend"]["mode"]),
        "x-oasis-route-paid": "true" if m["spend"]["paid"] else "false",
    }


def _error_response(err: GatewayError, request_id: str) -> JSONResponse:
    body = err.body()
    body["oasis_route"] = {**err.meta, "request_id": request_id}
    headers = {}
    if err.retry_after_s is not None:
        headers["retry-after"] = str(max(1, int(err.retry_after_s + 0.999)))
    return JSONResponse(status_code=err.status, content=body, headers=headers)


@router.post("/chat/completions", response_model=None)
async def chat_completions(
    request: Request,
    p: Principal = Depends(require_scope(ApiKeyScope.INFERENCE)),
    ctx: AppContext = Depends(get_ctx),
) -> Response:
    request_id: str = request.state.request_id
    try:
        raw = await request.body()
        if len(raw) > ctx.settings.max_request_bytes:
            raise GatewayError(
                413, "payload_too_large", "request body too large", type_="invalid_request_error"
            )
        try:
            body = json.loads(raw or b"null")
        except ValueError as exc:
            raise GatewayError(
                400, "invalid_json", "request body is not valid JSON", type_="invalid_request_error"
            ) from exc
        parsed = parse_body(body, p.project, ctx.settings.default_max_output_tokens)
    except GatewayError as err:
        return _error_response(err, request_id)

    idem_key = request.headers.get("idempotency-key")
    st = RequestState(request_id=request_id, project=p.project, api_key_id=p.key.id, parsed=parsed)
    engine = Engine(ctx)

    if parsed.chat.stream:
        if idem_key:
            return _error_response(
                GatewayError(
                    400,
                    "unsupported",
                    "Idempotency-Key is not supported for streaming requests",
                    type_="invalid_request_error",
                ),
                request_id,
            )
        try:
            await engine.begin(st)
            d, handle, first = await engine.open_stream(st)
        except GatewayError as err:
            return _error_response(err, request_id)
        st.served = d.cand  # for headers; attempts so far explain any pre-stream fallback
        headers = {**_headers(st), "cache-control": "no-cache", "x-accel-buffering": "no"}
        return StreamingResponse(
            engine.relay(st, d, handle, first), media_type="text/event-stream", headers=headers
        )

    if idem_key:
        try:
            hit = await idem_begin(ctx, p.project.id, idem_key, parsed.raw_hash)
        except GatewayError as err:
            return _error_response(err, request_id)
        if hit is not None:
            return JSONResponse(
                status_code=hit.status, content=hit.body, headers={"idempotent-replayed": "true"}
            )
    try:
        await engine.begin(st)
        result = await engine.complete(st)
    except GatewayError as err:
        if idem_key:
            # Deterministic client errors are stored; capacity/upstream failures free the key for retry.
            await idem_finish(
                ctx, p.project.id, idem_key, err.status, err.body() if err.status < 429 else None
            )
        return _error_response(err, request_id)
    except BaseException:
        if idem_key:
            await idem_finish(ctx, p.project.id, idem_key, 500, None)
        raise
    payload: dict[str, Any] = dict(result.payload)
    payload["oasis_route"] = st.meta()
    if idem_key:
        await idem_finish(ctx, p.project.id, idem_key, 200, payload)
    return JSONResponse(content=payload, headers=_headers(st))


def _anthropic_error(
    status: int,
    message: str,
    code: str | None,
    retry_after_s: float | None,
    meta: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> JSONResponse:
    headers = {}
    if retry_after_s is not None:
        headers["retry-after"] = str(max(1, int(retry_after_s + 0.999)))
    body = ac.error_body(status, message, code)
    if meta:  # route metadata (rejections, attempts, approval options) alongside the Anthropic error shape
        body["oasis_route"] = {**meta, "request_id": request_id or meta.get("request_id")}
    return JSONResponse(status_code=status, content=body, headers=headers)


async def _map_requested_model(ctx: AppContext, name: str) -> str:
    """Anthropic clients send Claude model names; route them as ``auto`` unless such a model is connected."""
    if not name.startswith("claude"):
        return name
    async with ctx.db.session() as session:
        found = (
            await session.execute(
                select(Model.id)
                .where(Model.upstream_id == name, Model.lifecycle.notin_(UNROUTABLE_LIFECYCLES))
                .limit(1)
            )
        ).first()
    return name if found is not None else "auto"


async def _read_json(request: Request, ctx: AppContext) -> Any:
    raw = await request.body()
    if len(raw) > ctx.settings.max_request_bytes:
        raise GatewayError(413, "payload_too_large", "request body too large", type_="invalid_request_error")
    try:
        return json.loads(raw or b"null")
    except ValueError as exc:
        raise GatewayError(
            400, "invalid_json", "request body is not valid JSON", type_="invalid_request_error"
        ) from exc


@router.post("/messages", response_model=None)
async def anthropic_messages(
    request: Request,
    p: Principal = Depends(require_scope(ApiKeyScope.INFERENCE)),
    ctx: AppContext = Depends(get_ctx),
) -> Response:
    """Anthropic Messages API, routed through the same engine as ``/v1/chat/completions``."""
    request_id: str = request.state.request_id
    try:
        body = await _read_json(request, ctx)
        try:
            chat_body = ac.to_chat_body(body)
        except ac.TranslationError as exc:
            raise GatewayError(400, "invalid_request", str(exc), type_="invalid_request_error") from exc
        requested = chat_body["model"]
        chat_body["model"] = await _map_requested_model(ctx, requested)
        parsed = parse_body(chat_body, p.project, ctx.settings.default_max_output_tokens)
    except GatewayError as err:
        return _anthropic_error(err.status, err.message, err.code, err.retry_after_s)

    st = RequestState(request_id=request_id, project=p.project, api_key_id=p.key.id, parsed=parsed)
    engine = Engine(ctx)
    try:
        await engine.begin(st)
        if parsed.chat.stream:
            d, handle, first = await engine.open_stream(st)
            st.served = d.cand
            headers = {**_headers(st), "cache-control": "no-cache", "x-accel-buffering": "no"}
            stream = ac.translate_stream(
                engine.relay(st, d, handle, first), requested, parsed.options.input_tokens
            )
            return StreamingResponse(stream, media_type="text/event-stream", headers=headers)
        result = await engine.complete(st)
    except GatewayError as err:
        return _anthropic_error(err.status, err.message, err.code, err.retry_after_s, err.meta, request_id)
    message = ac.to_message(dict(result.payload), requested)
    message["oasis_route"] = st.meta()
    return JSONResponse(content=message, headers=_headers(st))


@router.post("/responses", response_model=None)
async def openai_responses(
    request: Request,
    p: Principal = Depends(require_scope(ApiKeyScope.INFERENCE)),
    ctx: AppContext = Depends(get_ctx),
) -> Response:
    """OpenAI Responses API (stateless), routed through the same engine as ``/v1/chat/completions``."""
    request_id: str = request.state.request_id
    try:
        body = await _read_json(request, ctx)
        try:
            chat_body = rc.to_chat_body(body)
        except rc.TranslationError as exc:
            raise GatewayError(400, "invalid_request", str(exc), type_="invalid_request_error") from exc
        parsed = parse_body(chat_body, p.project, ctx.settings.default_max_output_tokens)
    except GatewayError as err:
        return _error_response(err, request_id)

    st = RequestState(request_id=request_id, project=p.project, api_key_id=p.key.id, parsed=parsed)
    engine = Engine(ctx)
    try:
        await engine.begin(st)
        if parsed.chat.stream:
            d, handle, first = await engine.open_stream(st)
            st.served = d.cand
            headers = {**_headers(st), "cache-control": "no-cache", "x-accel-buffering": "no"}
            stream = rc.translate_stream(
                engine.relay(st, d, handle, first), body, parsed.options.input_tokens
            )
            return StreamingResponse(stream, media_type="text/event-stream", headers=headers)
        result = await engine.complete(st)
    except GatewayError as err:
        return _error_response(err, request_id)
    resp = rc.to_response(dict(result.payload), body)
    resp["oasis_route"] = st.meta()
    return JSONResponse(content=resp, headers=_headers(st))


@router.post("/messages/count_tokens")
async def anthropic_count_tokens(
    request: Request,
    p: Principal = Depends(require_scope(ApiKeyScope.INFERENCE)),
    ctx: AppContext = Depends(get_ctx),
) -> JSONResponse:
    """Estimate input tokens for a Messages request (the same estimate the router uses for admission)."""
    try:
        body = await _read_json(request, ctx)
        chat_body = ac.to_chat_body(body)
    except GatewayError as err:
        return _anthropic_error(err.status, err.message, err.code, None)
    except ac.TranslationError as exc:
        return _anthropic_error(400, str(exc), "invalid_request", None)
    tokens = estimate_tokens_from_messages(chat_body["messages"], chat_body.get("tools"))
    return JSONResponse(content={"input_tokens": tokens})


@router.get("/models")
async def list_models(
    p: Principal = Depends(require_scope(ApiKeyScope.INFERENCE)), ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    """Models reachable through this project's active connections, plus the ``auto`` router model."""
    async with ctx.db.session() as s:
        conns = (
            (
                await s.execute(
                    select(Connection).where(
                        Connection.project_id == p.project.id,
                        Connection.status == ConnectionStatus.ACTIVE.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        allowed: dict[str, set[str] | None] = {}
        for c in conns:
            if not c.allowed_models:
                allowed[c.provider_id] = None
            elif allowed.get(c.provider_id, set()) is not None:
                allowed.setdefault(c.provider_id, set()).update(c.allowed_models)  # type: ignore[union-attr]
        models = (
            (await s.execute(select(Model).where(Model.provider_id.in_(list(allowed) or [""]))))
            .scalars()
            .all()
        )
    now = int(time.time())
    data: list[dict[str, Any]] = [
        {
            "id": "auto",
            "object": "model",
            "created": now,
            "owned_by": "oasis-route",
            "oasis_route": {"description": "strongest suitable eligible model, with failover"},
        }
    ]
    data += [
        {
            "id": f"auto/{t.value}",
            "object": "model",
            "created": now,
            "owned_by": "oasis-route",
            "oasis_route": {"description": f"auto, ranked by {t.value} quality evidence", "task": t.value},
        }
        for t in Task
        if t is not Task.GENERAL
    ]
    for m in sorted(models, key=lambda x: (x.provider_id, x.upstream_id)):
        allow = allowed.get(m.provider_id)
        if (allow is not None and m.upstream_id not in allow) or (m.capabilities or {}).get("chat") is False:
            continue
        if m.lifecycle in UNROUTABLE_LIFECYCLES:
            continue
        q, src = quality_of(m, Task.GENERAL)
        data.append(
            {
                "id": f"{m.provider_id}/{m.upstream_id}",
                "object": "model",
                "created": now,
                "owned_by": m.provider_id,
                "oasis_route": {
                    "upstream_id": m.upstream_id,
                    "context_window": m.context_window,
                    "capabilities": m.capabilities,
                    "quality_general": q,
                    "quality_source": src,
                    "quality_prior": model_prior(m.upstream_id) if q is None else None,
                    "lifecycle": m.lifecycle,
                },
            }
        )
    return {"object": "list", "data": data}
