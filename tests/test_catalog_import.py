"""Imported OmniRoute catalogue: broad, provenance-tagged, and never mistaken for verified or free."""

from __future__ import annotations

from sqlalchemy import func, select

from oasis_route.config import Settings
from oasis_route.db.models import Endpoint, Evidence, Model, Offer, OfferTransition, Provider
from oasis_route.db.session import Database
from oasis_route.registry import imported
from oasis_route.registry.catalog import PROVIDERS, sync_catalog
from oasis_route.routing.router import RouteOptions, _capability_reason


def _settings(db_url: str) -> Settings:
    return Settings(environment="test", database_url=db_url, worker_mode="off", catalog_import_enabled=True)


def test_snapshot_has_provenance_and_breadth() -> None:
    src = imported.source()
    assert src["license"] == "MIT"
    assert len(src["commit"]) == 40
    assert src["verification"] == "advertised"
    provs = imported.providers()
    assert len(provs) >= 200
    assert sum(len(p.models) for p in provs) >= 1500
    for p in provs:
        assert p.status in imported.PROVIDER_STATUSES
        if p.status != "active":
            assert p.status_reason, p.id
        prov = p.raw["provenance"]
        assert prov["review_state"] == "unreviewed"
        assert prov["source_url"].startswith("https://github.com/")
        if p.status == "active":
            assert p.base_url and p.base_url.startswith(("https://", "http://"))


def test_imported_model_rows_are_never_free_or_routable() -> None:
    src = imported.source()
    for p in imported.providers():
        for m in p.models:
            row = imported.model_row(m, src)
            assert row["lifecycle"] == "catalog"
            assert "free" not in row["pricing"]
            if row["pricing"]:
                assert row["pricing"]["verified"] is False


def test_catalog_lifecycle_is_rejected_by_the_router() -> None:
    m = Model(provider_id="x", upstream_id="y", display_name="y", capabilities={}, lifecycle="catalog")
    assert _capability_reason(m, RouteOptions()) == "catalog_model_unconfirmed"


async def test_sync_imports_additively_and_idempotently(db_url: str) -> None:
    db = Database(db_url)
    try:
        first = await sync_catalog(db, _settings(db_url))
        assert first > 1500
        assert await sync_catalog(db, _settings(db_url)) == 0
        async with db.session() as s:
            n_prov = (await s.execute(select(func.count()).select_from(Provider))).scalar_one()
            assert n_prov >= 200
            # Curated seeds keep their reviewed facts; the import only annotates them.
            groq = await s.get(Provider, "groq")
            assert groq is not None and groq.notes.startswith(
                next(p for p in PROVIDERS if p.id == "groq").notes[:20]
            )
            assert groq.catalog.get("source") == "omniroute"
            # No imported model is free or routable.
            free = (await s.execute(select(Model).where(Model.lifecycle == "catalog"))).scalars().all()
            assert free and all("free" not in (m.pricing or {}) for m in free)
            # Blocked providers are visible with their reason and cannot be connected.
            blocked = (await s.execute(select(Provider).where(Provider.status == "blocked"))).scalars().all()
            assert blocked and all(b.catalog.get("status_reason") for b in blocked)
            assert all(not b.automated_access_permitted for b in blocked)
            # Imported providers get an endpoint and adapter key.
            eps = {e.provider_id for e in (await s.execute(select(Endpoint))).scalars()}
            active = [p for p in imported.providers() if p.status == "active" and p.base_url]
            assert all(p.id in eps for p in active)
            # Advertised free tiers become discovered offers with evidence and an audit transition.
            offers = (
                (await s.execute(select(Offer).where(Offer.dedupe_key.like("omniroute:%")))).scalars().all()
            )
            assert offers
            assert {o.state for o in offers} <= {"discovered", "ineligible"}
            assert all(o.confidence <= 0.2 for o in offers)
            ev = (
                (await s.execute(select(Evidence).where(Evidence.method == "catalog_import"))).scalars().all()
            )
            assert len(ev) == len(offers)
            tr = (
                (
                    await s.execute(
                        select(OfferTransition).where(OfferTransition.actor == "system:catalog_import")
                    )
                )
                .scalars()
                .all()
            )
            assert len(tr) == len(offers)
    finally:
        await db.dispose()


async def test_import_can_be_disabled(db_url: str) -> None:
    db = Database(db_url)
    try:
        s = _settings(db_url)
        s.catalog_import_enabled = False
        await sync_catalog(db, s)
        async with db.session() as ses:
            n = await ses.execute(select(func.count()).select_from(Model).where(Model.lifecycle == "catalog"))
            assert n.scalar_one() == 0
    finally:
        await db.dispose()
