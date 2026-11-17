"""HTTP routers."""

from __future__ import annotations

from fastapi import FastAPI


def register_routers(app: FastAPI) -> None:
    from oasis_route.api import admin_identity, discovery, gateway, registry_connections

    app.include_router(admin_identity.router)
    app.include_router(registry_connections.router)
    app.include_router(discovery.router)
    app.include_router(gateway.router)
