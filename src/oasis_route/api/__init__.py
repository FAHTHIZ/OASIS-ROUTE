"""HTTP routers."""

from __future__ import annotations

from fastapi import FastAPI


def register_routers(app: FastAPI) -> None:
    """Attach all routers. Imported lazily to keep module import order simple."""
    return None
