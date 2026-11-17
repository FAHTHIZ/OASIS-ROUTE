"""HTTP routers."""

from __future__ import annotations

from fastapi import FastAPI


def register_routers(app: FastAPI) -> None:
    from oasis_route.api import (
        admin_identity,
        automation,
        capacity,
        discovery,
        gateway,
        mail,
        oauth,
        registry_connections,
    )

    app.include_router(admin_identity.router)
    app.include_router(registry_connections.router)
    app.include_router(discovery.router)
    app.include_router(capacity.router)
    app.include_router(mail.router)
    app.include_router(automation.router)
    app.include_router(oauth.router)
    app.include_router(gateway.router)
