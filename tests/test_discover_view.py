"""The Discover catalogue: honest access requirements, verified-only offers for users, next actions."""

from __future__ import annotations

from typing import Any

import httpx

from oasis_route.context import AppContext
from oasis_route.db.models import Evidence, Offer
from tests.helpers import auth, gateway_setup


async def _discover(client: httpx.AsyncClient, key: str, **params: str) -> dict[str, Any]:
    r = await client.get("/api/v1/discover", params=params, headers=auth(key))
    assert r.status_code == 200, r.text
    data: dict[str, Any] = r.json()
    return data


def _by_id(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["id"]: p for p in data["providers"]}


async def _offers(ctx: AppContext) -> None:
    async with ctx.db.transaction() as s:
        live = Offer(
            provider_id="groq",
            title="Groq free tier",
            dedupe_key="groq:free",
            state="connectable",
            offer_class="free_tier",
            access_path="api_key",
            quota_claims=[
                {"limit": 14400, "unit": "requests", "period": "day", "model_scope": "llama-3.1-8b-instant"},
                {"limit": 30, "unit": "requests", "period": "minute", "model_scope": "*"},
            ],
            models=[{"model": "llama-3.1-8b-instant", "free": True}],
            billing={"card_required": False},
            links={"pricing": "https://groq.com/pricing"},
        )
        pending = Offer(
            provider_id="groq",
            title="Unreviewed credit",
            dedupe_key="groq:credit",
            state="needs_review",
            offer_class="trial_credit",
        )
        s.add_all([live, pending])
        await s.flush()
        s.add(
            Evidence(
                offer_id=live.id,
                url="https://console.groq.com/docs/rate-limits",
                sha256="0" * 64,
                claim_type="quota",
            )
        )


async def test_discover_shows_verified_offers_and_next_actions(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: Any
) -> None:
    g = await gateway_setup(client, two_providers=False)
    await _offers(ctx)
    data = await _discover(client, g["key"])

    st = data["status"]
    assert st["offers"]["verified"] == 1 and st["offers"]["pending_review"] == 1
    assert st["project"]["id"] == g["pid"] and st["project"]["spend_policy"] == "free_only"

    cards = _by_id(data)
    groq = cards["groq"]
    assert [o["title"] for o in groq["offers"]] == ["Groq free tier"]  # unreviewed claims are hidden
    assert groq["pending_review"] == 1
    offer = groq["offers"][0]
    assert offer["verified"] is True and offer["evidence"]["urls"] == [
        "https://console.groq.com/docs/rate-limits"
    ]
    assert offer["quotas"][0]["model_scope"] == "*"  # account-wide limits first
    assert offer["models"]["free"] == ["llama-3.1-8b-instant"]
    assert groq["card_required"] is False and groq["card_required_source"] == "evidence"
    assert groq["links"]["pricing"] == "https://groq.com/pricing"
    assert groq["next_action"]["kind"] == "connect_api_key"
    assert groq["next_action"]["url"].startswith("https://")

    # OpenRouter offers a real sign-in flow; the connected mock is reported as connected.
    assert cards["openrouter"]["next_action"] == {"kind": "connect_oauth", "label": "Sign in with OpenRouter"}
    assert cards["openrouter"]["access_path"] == "official_oauth"
    mock = cards["mock"]
    assert mock["connection"]["id"] == g["conn_a"]["id"]
    assert mock["next_action"]["kind"] in ("connected", "confirm_billing")

    # Hugging Face sign-in needs an https public URL or a registered app; over plain http it is not offered.
    assert cards["huggingface"]["oauth_available"] is False
    assert cards["huggingface"]["next_action"]["kind"] == "connect_api_key"


async def test_admin_sees_unreviewed_offers_and_tenants_are_isolated(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: Any
) -> None:
    g = await gateway_setup(client, two_providers=False)
    await _offers(ctx)
    admin = await _discover(client, g["admin"])
    titles = {o["title"]: o for o in _by_id(admin)["groq"]["offers"]}
    assert titles["Unreviewed credit"]["admin_review_needed"] is True
    assert titles["Unreviewed credit"]["verified"] is False

    # A user cannot look at someone else's project.
    admin_pid = admin["status"]["project"]["id"]
    r = await client.get("/api/v1/discover", params={"project_id": admin_pid}, headers=auth(g["key"]))
    assert r.status_code == 404

    assert (await client.get("/api/v1/discover")).status_code == 401
