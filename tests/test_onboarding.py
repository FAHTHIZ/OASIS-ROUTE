"""Turnkey onboarding: local detection, honest route classification, setup that never enables paid routes."""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import update

from oasis_route.context import AppContext
from oasis_route.db.models import Offer, Provider
from tests.helpers import auth, gateway_setup


def _serve_local(ctx: AppContext) -> None:
    """Answer every loopback port with the mock provider (as if Ollama, vLLM, ... were running)."""
    transport = ctx.upstream_transport
    assert transport is not None

    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers  # local presets never send a key
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200, json={"object": "list", "data": [{"id": "llama3.2", "object": "model"}]}
            )
        return httpx.Response(404)

    transport.apps["127.0.0.1"] = httpx.MockTransport(handler)  # type: ignore[attr-defined]


async def test_detection_is_off_without_private_upstreams(client: httpx.AsyncClient, ctx: AppContext) -> None:
    from tests.helpers import bootstrap

    admin = await bootstrap(client)
    key, pid = admin["api_key"]["secret"], admin["project"]["id"]
    ctx.settings.allow_private_upstreams = False
    r = await client.get(f"/api/v1/projects/{pid}/setup", headers=auth(key))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["local_servers"]["enabled"] is False
    assert out["endpoint"]["base_url"].endswith("/v1") and out["endpoint"]["model"] == "auto"


async def test_setup_detects_and_connects_local_servers(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: Any
) -> None:
    g = await gateway_setup(client, two_providers=False)
    _serve_local(ctx)
    dry = (await client.get(f"/api/v1/projects/{g['pid']}/setup", headers=auth(g["key"]))).json()
    found = {s["provider_id"] for s in dry["local_servers"]["servers"] if s["reachable"]}
    assert {"ollama", "vllm", "lm_studio"} <= found
    # The server's own port (8080) is never probed as llama.cpp.
    assert "llama_cpp" not in {s["provider_id"] for s in dry["local_servers"]["servers"]}

    r = await client.post(f"/api/v1/projects/{g['pid']}/setup", json={}, headers=auth(g["key"]))
    assert r.status_code == 200, r.text
    out = r.json()
    assert {c["provider_id"] for c in out["connected_local"]} == found
    routes = out["routes"]["routes"]
    local = [x for x in routes if x["provider_id"] == "ollama"]
    assert local and all(x["cost_class"] == "free" and x["free_evidence"] == "local" for x in local)
    assert all(x["state"] == "eligible" for x in local)
    # Spend policy untouched: setup never enables paid routes.
    proj = (await client.get(f"/api/v1/projects/{g['pid']}", headers=auth(g["key"]))).json()
    assert proj["spend_policy"] == "free_only"
    conns = (await client.get(f"/api/v1/projects/{g['pid']}/connections", headers=auth(g["key"]))).json()
    assert all(c["billing_mode"] != "paid" for c in conns)
    assert {c["priority"] for c in conns if c["provider_id"] == "ollama"} == {-10}
    # Idempotent: a second run connects nothing new.
    again = (await client.post(f"/api/v1/projects/{g['pid']}/setup", json={}, headers=auth(g["key"]))).json()
    assert again["connected_local"] == []


async def test_local_preset_rejects_unlisted_public_host(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: Any
) -> None:
    g = await gateway_setup(client, two_providers=False)

    async def public(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    ctx.resolver = public
    r = await client.post(
        f"/api/v1/projects/{g['pid']}/connections",
        json={"provider_id": "vllm", "base_url": "http://gpu.example.com/v1", "validate_now": False},
        headers=auth(g["key"]),
    )
    assert r.status_code == 403


async def test_routes_are_classified_honestly(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: Any
) -> None:
    g = await gateway_setup(client, two_providers=False)
    out = (await client.get(f"/api/v1/projects/{g['pid']}/routes", headers=auth(g["key"]))).json()
    assert out["summary"]["eligible_free"] > 0
    assert {x["cost_class"] for x in out["routes"]} == {"free"}
    # Same provider, but now billable with unknown cost: blocked under zero-budget, never shown as free.
    async with ctx.db.transaction() as s:
        await s.execute(update(Provider).where(Provider.id == "mock").values(regions=["us"]))
    out = (await client.get(f"/api/v1/projects/{g['pid']}/routes", headers=auth(g["key"]))).json()
    assert {x["cost_class"] for x in out["routes"]} == {"unknown"}
    assert {x["state"] for x in out["routes"]} == {"blocked"}
    assert {x["reason"] for x in out["routes"]} == {"cost_unknown"}
    assert out["summary"]["eligible_free"] == 0
    # Declared paid: still blocked, labelled paid.
    cid = g["conn_a"]["id"]
    await client.patch(
        f"/api/v1/projects/{g['pid']}/connections/{cid}",
        json={"billing_mode": "paid"},
        headers=auth(g["key"]),
    )
    out = (await client.get(f"/api/v1/projects/{g['pid']}/routes", headers=auth(g["key"]))).json()
    assert {(x["cost_class"], x["reason"]) for x in out["routes"]} == {("paid", "paid_route_blocked")}


async def test_connectable_lists_verified_and_advertised_separately(
    client: httpx.AsyncClient, ctx: AppContext
) -> None:
    from tests.helpers import bootstrap

    admin = await bootstrap(client)
    key, pid = admin["api_key"]["secret"], admin["project"]["id"]
    async with ctx.db.transaction() as s:
        s.add(Offer(provider_id="groq", title="Groq free", dedupe_key="t:groq", state="connectable"))
        s.add(Offer(provider_id="cerebras", title="Cerebras free", dedupe_key="t:cer", state="discovered"))
        s.add(Offer(provider_id="mistral", title="Rejected", dedupe_key="t:mis", state="rejected"))
    out = (await client.get(f"/api/v1/projects/{pid}/setup", headers=auth(key))).json()[
        "connectable_providers"
    ]
    assert [e["provider_id"] for e in out["verified"]] == ["groq"]
    assert out["verified"][0]["verification"] == "verified"
    assert [e["provider_id"] for e in out["advertised"]] == ["cerebras"]
    assert out["advertised"][0]["verification"] == "advertised"
