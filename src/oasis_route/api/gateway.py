"""OpenAI-compatible gateway: ``POST /v1/chat/completions`` and ``GET /v1/models``.

Authenticate with an Oasis Key: ``Authorization: Bearer $OASIS_API_KEY``.
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select

from oasis_route.api.deps import Principal, get_ctx, require_scope
from oasis_route.context import AppContext
from oasis_route.db.models import Connection, Model
from oasis_route.domain.enums import ApiKeyScope, ConnectionStatus, Task
from oasis_route.gateway.engine import (
    Engine,
    GatewayError,
    RequestState,
    idem_begin,
    idem_finish,
    parse_body,
)
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
    for m in sorted(models, key=lambda x: (x.provider_id, x.upstream_id)):
        allow = allowed.get(m.provider_id)
        if (allow is not None and m.upstream_id not in allow) or (m.capabilities or {}).get("chat") is False:
            continue
        if m.lifecycle == "removed":
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
