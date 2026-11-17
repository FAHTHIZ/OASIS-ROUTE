"""HTTP routers."""

from __future__ import annotations

from fastapi import FastAPI


def register_routers(app: FastAPI) -> None:
    from oasis_route.api import admin_identity

    app.include_router(admin_identity.router)
