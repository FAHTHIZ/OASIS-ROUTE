"""FastAPI application factory."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from oasis_route import __version__
from oasis_route.config import Settings, get_settings
from oasis_route.context import AppContext
from oasis_route.db.session import Database
from oasis_route.domain.errors import AppError
from oasis_route.logging import configure_logging, get_logger, request_id_var

log = get_logger("oasis_route.app")
STATIC_DIR = Path(__file__).parent / "static"


def _error(
    status: int, code: str, message: str, request_id: str | None, details: object = None
) -> JSONResponse:
    body: dict[str, object] = {"error": {"code": code, "message": message, "request_id": request_id}}
    if details:
        body["error"]["details"] = details  # type: ignore[index]
    return JSONResponse(status_code=status, content=body)


def create_app(settings: Settings | None = None, ctx: AppContext | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    ctx = ctx or AppContext(settings=settings, db=Database(settings.database_url))

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        log.info("startup", extra={"version": __version__, "environment": settings.environment.value})
        yield
        await ctx.db.dispose()

    app = FastAPI(
        title="Oasis Route",
        version=__version__,
        description="Provider-neutral inference control plane. Gateway at /v1, admin API at /api/v1.",
        lifespan=lifespan,
    )
    app.state.ctx = ctx

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-request-id", "")
        rid = (
            incoming
            if incoming.isascii() and 8 <= len(incoming) <= 64 and incoming.replace("-", "").isalnum()
            else f"req_{uuid.uuid4().hex}"
        )
        token = request_id_var.set(rid)
        request.state.request_id = rid
        start = time.perf_counter()
        try:
            length = int(request.headers.get("content-length") or 0)
            if length > settings.max_request_bytes:
                response: Response = _error(413, "payload_too_large", "Request body too large", rid)
            else:
                response = await call_next(request)
        finally:
            request_id_var.reset(token)
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        ctx.metrics.http_requests.labels(request.method, path, str(response.status_code)).inc()
        response.headers["x-request-id"] = rid
        log.debug(
            "http",
            extra={
                "path": path,
                "status": response.status_code,
                "ms": round((time.perf_counter() - start) * 1000, 1),
            },
        )
        return response

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        resp = _error(
            exc.status_code, exc.code, exc.message, getattr(request.state, "request_id", None), exc.details
        )
        if exc.status_code == 429 and "retry_after_s" in exc.details:
            resp.headers["retry-after"] = str(int(float(exc.details["retry_after_s"])) + 1)
        return resp

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errs = [
            {"loc": list(e.get("loc", [])), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()
        ]
        return _error(
            422,
            "validation_failed",
            "Request validation failed",
            getattr(request.state, "request_id", None),
            errs,
        )

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        """Liveness: the process is running."""
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["health"])
    async def readyz() -> JSONResponse:
        """Readiness: database reachable and migrated."""
        try:
            async with ctx.db.session() as s:
                await s.execute(text("SELECT 1"))
                rev = (await s.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        except Exception as exc:
            log.warning("readiness check failed", extra={"error": type(exc).__name__})
            return JSONResponse(status_code=503, content={"status": "unavailable", "reason": "database"})
        return JSONResponse(content={"status": "ready", "schema_revision": rev})

    @app.get("/metrics", tags=["health"], include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(ctx.metrics.registry), media_type=CONTENT_TYPE_LATEST)

    from oasis_route.api import register_routers

    register_routers(app)

    if STATIC_DIR.exists():
        app.mount("/console", StaticFiles(directory=STATIC_DIR, html=True), name="console")

        @app.get("/", include_in_schema=False)
        async def root() -> RedirectResponse:
            return RedirectResponse("/console/")

    return app
