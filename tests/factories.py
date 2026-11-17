"""Direct-DB factories for unit tests that bypass the HTTP layer."""

from __future__ import annotations

from typing import Any

from oasis_route.db.models import Project, Provider, QuotaPool, User
from oasis_route.db.session import Database


async def project_with_provider(db: Database, provider_id: str = "mock") -> tuple[str, str]:
    async with db.transaction() as s:
        u = User(email=f"u-{provider_id}@example.com")
        s.add(u)
        await s.flush()
        p = Project(name="p", owner_id=u.id, data_policy={})
        s.add(p)
        if await s.get(Provider, provider_id) is None:
            s.add(Provider(id=provider_id, name=provider_id, adapter_key="openai_compatible"))
        await s.flush()
        return p.id, provider_id


async def pool(db: Database, project_id: str, provider_id: str, **kw: Any) -> QuotaPool:
    defaults: dict[str, Any] = {
        "unit": "requests",
        "period": "day",
        "limit_value": 10,
        "account_scope": "default",
        "model_scope": "*",
    }
    defaults.update(kw)
    async with db.transaction() as s:
        p = QuotaPool(project_id=project_id, provider_id=provider_id, used=0, reserved=0, **defaults)
        s.add(p)
        await s.flush()
        return p


async def reload(db: Database, pool_id: str) -> QuotaPool:
    async with db.session() as s:
        p = await s.get(QuotaPool, pool_id)
        assert p is not None
        return p
