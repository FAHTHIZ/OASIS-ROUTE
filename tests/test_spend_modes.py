"""Spend modes: zero_budget fails clearly, bounded_autonomy asks first, continuity labels paid fallback."""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import select, update

from oasis_route.context import AppContext
from oasis_route.db.models import AuditEvent, Model, PaidApproval, Project, Provider
from tests.helpers import auth, chat, gateway_setup

PRICE = {"prompt_usd_per_mtok": 1.0, "completion_usd_per_mtok": 2.0, "source": "provider_model_list"}


async def _remote(ctx: AppContext) -> None:
    """Make the mocks look like billable remote providers with a known price."""
    async with ctx.db.transaction() as s:
        await s.execute(update(Provider).where(Provider.id.in_(["mock", "mock_alt"])).values(regions=["us"]))
        await s.execute(
            update(Model).where(Model.provider_id.in_(["mock", "mock_alt"])).values(pricing=PRICE)
        )


async def _billing(client: httpx.AsyncClient, g: dict[str, Any], conn: str, mode: str, **extra: Any) -> None:
    r = await client.patch(
        f"/api/v1/projects/{g['pid']}/connections/{g[conn]['id']}",
        json={"billing_mode": mode, **extra},
        headers=auth(g["key"]),
    )
    assert r.status_code == 200, r.text


async def _policy(
    client: httpx.AsyncClient, g: dict[str, Any], policy: str, ack: bool = False
) -> dict[str, Any]:
    r = await client.patch(
        f"/api/v1/projects/{g['pid']}",
        json={"spend_policy": policy, "acknowledge_paid_usage": ack},
        headers=auth(g["key"]),
    )
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


async def test_zero_budget_alias_fails_clearly_without_approval(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: Any
) -> None:
    g = await gateway_setup(client, two_providers=False)
    await _remote(ctx)
    await _billing(client, g, "conn_a", "paid")
    assert (await _policy(client, g, "zero_budget"))["spend_policy"] == "free_only"
    r = await chat(client, g["key"])
    assert r.status_code >= 400 and r.status_code != 402, r.text
    meta = r.json()["oasis_route"]
    assert "approval" not in meta and meta["spend"]["mode"] == "zero_budget"
    assert meta["rejections"] == {"paid_route_blocked": 3}
    assert upstreams["mock-a.test"].calls == []


async def test_bounded_autonomy_asks_once_then_labels_paid_route(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: Any
) -> None:
    g = await gateway_setup(client, two_providers=False)
    await _remote(ctx)
    await _billing(client, g, "conn_a", "paid")
    await _policy(client, g, "bounded_autonomy")  # no paid consent needed: it never spends unasked

    r = await chat(client, g["key"], "mock-small")
    assert r.status_code == 402, r.text
    body = r.json()
    assert body["error"]["code"] == "paid_approval_required"
    assert upstreams["mock-a.test"].calls == []  # nothing dispatched, nothing charged
    opt = body["oasis_route"]["approval"]["options"][0]
    assert (opt["provider"], opt["model"]) == ("mock", "mock-small")
    assert opt["cost_status"] == "priced" and opt["estimated_cost_usd"] > 0 and opt["price_verified"]
    assert "no free route" in opt["reason"] and opt["approval_token"].startswith("oapr_")

    ok = await chat(client, g["key"], "mock-small", oasis_route={"approve_paid": opt["approval_token"]})
    assert ok.status_code == 200, ok.text
    assert ok.headers["x-oasis-route-paid"] == "true"
    assert ok.headers["x-oasis-route-spend-mode"] == "bounded_autonomy"
    spend = ok.json()["oasis_route"]["spend"]
    assert spend["paid"] and spend["label"] == "PAID FALLBACK"
    assert spend["authorized_by"] == "single_use_approval" and spend["estimated_cost_usd"] > 0

    again = await chat(client, g["key"], "mock-small", oasis_route={"approve_paid": opt["approval_token"]})
    assert again.status_code == 403 and again.json()["error"]["code"] == "approval_used"
    bogus = await chat(client, g["key"], "mock-small", oasis_route={"approve_paid": "oapr_nope"})
    assert bogus.status_code == 403 and bogus.json()["error"]["code"] == "approval_unknown"

    async with ctx.db.session() as s:
        actions = set((await s.execute(select(AuditEvent.action))).scalars())
        rows = list((await s.execute(select(PaidApproval))).scalars())
    assert {"spend.paid_approval_requested", "spend.paid_approval_used"} <= actions
    assert all(len(r.token_hash) == 64 and "oapr_" not in r.token_hash for r in rows)


async def test_bounded_autonomy_prefers_free_without_asking(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: Any
) -> None:
    g = await gateway_setup(client)
    await _remote(ctx)
    await _billing(client, g, "conn_a", "paid", priority=100)
    await _billing(client, g, "conn_b", "free_tier")
    await _policy(client, g, "bounded_autonomy")
    r = await chat(client, g["key"])
    assert r.status_code == 200, r.text
    assert r.headers["x-oasis-route-provider"] == "mock_alt" and r.headers["x-oasis-route-paid"] == "false"


async def test_continuity_prefers_free_and_labels_paid_fallback(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: Any
) -> None:
    g = await gateway_setup(client)
    await _remote(ctx)
    await _billing(client, g, "conn_a", "paid", priority=100)  # higher priority, but paid
    await _billing(client, g, "conn_b", "free_tier")
    assert (await _policy(client, g, "continuity", ack=True))["spend_policy"] == "allow_paid"
    r = await chat(client, g["key"], "mock-large")
    assert r.status_code == 200 and r.headers["x-oasis-route-provider"] == "mock_alt", r.text

    upstreams["mock-b.test"].fail_next("mock-large", 429, retry_after=30)
    r = await chat(client, g["key"], "mock-large")
    assert r.status_code == 200, r.text
    meta = r.json()["oasis_route"]
    assert meta["provider"] == "mock" and meta["spend"]["paid"] and meta["spend"]["label"] == "PAID FALLBACK"
    assert meta["spend"]["authorized_by"] == "project_spend_policy"
    assert "RATE_LIMITED" in meta["spend"]["reason"] or "QUOTA" in meta["spend"]["reason"]
    cd = meta["cooldowns"][0]
    assert cd["provider"] == "mock_alt" and cd["retry_in_s"] == 30
    assert meta["attempts"][0]["retry_source"] == "provider"
    # The cooled-down free route is not hammered on the next request.
    calls = len(upstreams["mock-b.test"].calls)
    r = await chat(client, g["key"], "mock-large")
    assert r.status_code == 200 and len(upstreams["mock-b.test"].calls) == calls


async def test_unknown_stored_policy_fails_closed(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: Any
) -> None:
    g = await gateway_setup(client, two_providers=False)
    await _remote(ctx)
    await _billing(client, g, "conn_a", "paid")
    async with ctx.db.transaction() as s:
        await s.execute(update(Project).where(Project.id == g["pid"]).values(spend_policy="weird"))
    r = await chat(client, g["key"])
    assert r.status_code >= 400 and upstreams["mock-a.test"].calls == []
