"""Free-only spend policy: the router never silently uses capacity that could be billed."""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import select, update

from oasis_route.context import AppContext
from oasis_route.db.models import AuditEvent, Model, Provider
from oasis_route.routing.router import cost_basis
from tests.helpers import auth, chat, gateway_setup


async def _billable_mock(ctx: AppContext) -> None:
    """Make the local mock look like a real remote provider so the local exemption does not apply."""
    async with ctx.db.transaction() as s:
        await s.execute(update(Provider).where(Provider.id.in_(["mock", "mock_alt"])).values(regions=["us"]))


async def _preview(client: httpx.AsyncClient, g: dict[str, Any], **ext: Any) -> dict[str, Any]:
    r = await client.post(
        f"/api/v1/projects/{g['pid']}/route-preview", json={"oasis_route": ext}, headers=auth(g["key"])
    )
    assert r.status_code == 200, r.text
    data: dict[str, Any] = r.json()
    return data


async def test_new_projects_default_to_free_only(client: httpx.AsyncClient, upstreams: Any) -> None:
    g = await gateway_setup(client, two_providers=False)
    proj = (await client.get(f"/api/v1/projects/{g['pid']}", headers=auth(g["key"]))).json()
    assert proj["spend_policy"] == "free_only"
    plan = await _preview(client, g)
    assert plan["free_only"] is True
    assert {c["cost_basis"] for c in plan["candidates"]} == {"local"}  # dev mocks are free by construction


async def test_unknown_cost_is_blocked_until_declared_free(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: Any
) -> None:
    g = await gateway_setup(client, two_providers=False)
    await _billable_mock(ctx)
    plan = await _preview(client, g)
    assert plan["candidates"] == []
    assert {r["reason"] for r in plan["rejected"]} == {"cost_unknown"}
    r = await chat(client, g["key"])
    assert r.status_code >= 400
    # The error names the blocked provider and the command that unblocks it.
    message = r.json()["error"]["message"]
    assert "free-only spend policy" in message and "oasis-route billing mock free_tier" in message, message
    st = (await client.get(f"/api/v1/projects/{g['pid']}/status", headers=auth(g["key"]))).json()
    assert st["status"] == "Action required" and "Free-only" in st["summary"], st
    assert st["spend_policy"] == "free_only"

    # The account owner declares the upstream account free-tier (no billing attached): routable.
    cid = g["conn_a"]["id"]
    assert g["conn_a"]["billing_mode"] == "unknown"
    r = await client.patch(
        f"/api/v1/projects/{g['pid']}/connections/{cid}",
        json={"billing_mode": "free_tier"},
        headers=auth(g["key"]),
    )
    assert r.status_code == 200 and r.json()["billing_mode"] == "free_tier"
    plan = await _preview(client, g)
    assert plan["candidates"] and {c["cost_basis"] for c in plan["candidates"]} == {"free_tier_account"}
    assert (await chat(client, g["key"])).status_code == 200

    # Marked paid: explicitly blocked, with a distinct reason.
    await client.patch(
        f"/api/v1/projects/{g['pid']}/connections/{cid}",
        json={"billing_mode": "paid"},
        headers=auth(g["key"]),
    )
    plan = await _preview(client, g)
    assert plan["candidates"] == [] and {r["reason"] for r in plan["rejected"]} == {"paid_route_blocked"}


async def test_zero_priced_models_route_on_unknown_accounts(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: Any
) -> None:
    g = await gateway_setup(client, two_providers=False)
    await _billable_mock(ctx)
    async with ctx.db.transaction() as s:
        await s.execute(
            update(Model)
            .where(Model.provider_id == "mock", Model.upstream_id == "mock-small")
            .values(pricing={"free": True, "source": "test"})
        )
    plan = await _preview(client, g)
    assert [(c["upstream_model"], c["cost_basis"]) for c in plan["candidates"]] == [
        ("mock-small", "free_model")
    ]
    r = await chat(client, g["key"])
    assert r.status_code == 200 and r.headers["x-oasis-route-model"] == "mock-small"


async def test_allow_paid_needs_explicit_consent_and_is_audited(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: Any
) -> None:
    g = await gateway_setup(client, two_providers=False)
    await _billable_mock(ctx)
    url = f"/api/v1/projects/{g['pid']}"
    r = await client.patch(url, json={"spend_policy": "allow_paid"}, headers=auth(g["key"]))
    assert r.status_code == 422 and "acknowledge_paid_usage" in r.text
    r = await client.patch(
        url, json={"spend_policy": "allow_paid", "acknowledge_paid_usage": True}, headers=auth(g["key"])
    )
    assert r.status_code == 200 and r.json()["spend_policy"] == "allow_paid"
    async with ctx.db.session() as s:
        ev = (
            await s.execute(select(AuditEvent).where(AuditEvent.action == "project.spend_policy_changed"))
        ).scalar_one()
    assert ev.details == {"spend_policy": "allow_paid", "acknowledged_paid_usage": True}
    plan = await _preview(client, g)
    assert plan["free_only"] is False and plan["candidates"]
    assert {c["cost_basis"] for c in plan["candidates"]} == {"unknown"}

    # A single request can still tighten to free-only; it can never loosen a free-only project.
    plan = await _preview(client, g, free_only=True)
    assert plan["free_only"] is True and plan["candidates"] == []
    await client.patch(url, json={"spend_policy": "free_only"}, headers=auth(g["key"]))
    plan = await _preview(client, g, free_only=False)
    assert plan["free_only"] is True and plan["candidates"] == []


async def test_billed_only_providers_default_to_paid(client: httpx.AsyncClient, ctx: AppContext) -> None:
    from tests.helpers import bootstrap, create_user

    admin = (await bootstrap(client))["api_key"]["secret"]
    u = await create_user(client, admin, "dev@example.com", declared_age=30, country="BD")
    r = await client.post(
        f"/api/v1/projects/{u['project']['id']}/connections",
        json={"provider_id": "openai", "api_key": "sk-x", "accept_terms": True, "validate_now": False},
        headers=auth(u["api_key"]["secret"]),
    )
    assert r.status_code == 201 and r.json()["billing_mode"] == "paid"


async def test_existing_projects_keep_unrestricted_behaviour(ctx: AppContext) -> None:
    """Migration 0006 backfills existing rows with allow_paid, so upgraded installs behave as before."""
    from sqlalchemy import text

    async with ctx.db.session() as s:
        if ctx.db.engine.dialect.name == "postgresql":
            default = (
                await s.execute(
                    text(
                        "SELECT column_default FROM information_schema.columns "
                        "WHERE table_name = 'projects' AND column_name = 'spend_policy'"
                    )
                )
            ).scalar_one()
        else:
            cols = (await s.execute(text("PRAGMA table_info(projects)"))).all()
            default = next(c[4] for c in cols if c[1] == "spend_policy")
    default = str(default).split("::")[0]
    assert str(default).strip("'\"") == "allow_paid"


def test_cost_basis_precedence() -> None:
    from oasis_route.db.models import Connection

    prov = Provider(id="x", name="x", adapter_key="openai_compatible", regions=["us"])
    paid = Connection(billing_mode="paid")
    assert cost_basis(paid, prov, Model(pricing={"free": True})) == "free_model"
    assert cost_basis(paid, prov, Model(pricing={"free": None})) == "paid"
    assert cost_basis(Connection(billing_mode="unknown"), prov, Model(pricing={})) == "unknown"
    assert cost_basis(Connection(billing_mode="free_tier"), prov, Model(pricing={})) == "free_tier_account"
