"""OpenAI-compatible gateway: requests, streaming, idempotency, auth, accounting."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from oasis_route.context import AppContext
from oasis_route.db.models import Project, QuotaPool, Reservation, RouteDecision, UsageRecord
from oasis_route.gateway.engine import Engine, RequestState, parse_body
from tests.helpers import auth, chat, gateway_setup

pytestmark = pytest.mark.usefixtures("upstreams")


async def _pool(client: httpx.AsyncClient, g: dict[str, Any], provider: str = "mock", **kw: Any) -> str:
    body = {"provider_id": provider, "unit": "requests", "period": "day", "limit_value": 100, **kw}
    r = await client.post(f"/api/v1/projects/{g['pid']}/quota-pools", json=body, headers=auth(g["key"]))
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def _sse(text: str) -> list[Any]:
    out: list[Any] = []
    for line in text.splitlines():
        if line.startswith("data: "):
            raw = line[6:]
            out.append(raw if raw == "[DONE]" else json.loads(raw))
    return out


async def test_non_stream_success_and_accounting(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client)
    pool_id = await _pool(client, g)
    r = await chat(client, g["key"], content="hello there")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "[mock-large] echo: hello there"
    assert body["oasis_route"]["provider"] == "mock" and body["oasis_route"]["fallback"] is False
    assert r.headers["x-oasis-route-provider"] == "mock"
    assert r.headers["x-oasis-route-attempts"] == "1"
    assert r.headers["x-request-id"]
    sent = upstreams["mock-a.test"].calls[0]
    assert sent["model"] == "mock-large" and "oasis_route" not in sent["body"]
    async with ctx.db.session() as s:
        p = await s.get(QuotaPool, pool_id)
        assert p is not None and p.used == 1 and p.reserved == 0
        u = (await s.execute(select(UsageRecord))).scalar_one()
        assert u.outcome == "success" and u.provider_reported and u.input_tokens > 0
        d = (await s.execute(select(RouteDecision))).scalar_one()
        assert d.served_model == "mock-large" and not d.fallback_used
        res = (await s.execute(select(Reservation))).scalar_one()
        assert res.status == "committed"


async def test_pinned_model_and_unknown_model(client: httpx.AsyncClient) -> None:
    g = await gateway_setup(client)
    r = await chat(client, g["key"], model="mock_alt/mock-medium")
    assert r.status_code == 200
    assert r.headers["x-oasis-route-provider"] == "mock_alt"
    assert r.headers["x-oasis-route-model"] == "mock-medium"
    r = await chat(client, g["key"], model="nope-9000")
    assert r.status_code == 404 and r.json()["error"]["code"] == "model_not_found"


async def test_streaming_success_settles_usage(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client)
    pool_id = await _pool(client, g, unit="total_tokens", limit_value=100_000)
    r = await chat(client, g["key"], content="stream me", stream=True)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _sse(r.text)
    assert events[-1] == "[DONE]"
    assert events[0]["oasis_route"]["provider"] == "mock"
    text = "".join(e["choices"][0]["delta"].get("content", "") for e in events[:-1] if e.get("choices"))
    assert text.strip() == "[mock-large] echo: stream me"
    assert not any(e != "[DONE]" and not e.get("choices") for e in events)  # usage chunk hidden
    assert upstreams["mock-a.test"].calls[0]["body"]["stream_options"]["include_usage"] is True
    async with ctx.db.session() as s:
        p = await s.get(QuotaPool, pool_id)
        assert p is not None and p.reserved == 0 and p.used > 0
        u = (await s.execute(select(UsageRecord))).scalar_one()
        assert u.outcome == "success" and u.provider_reported and u.streamed


async def test_stream_usage_chunk_passthrough(client: httpx.AsyncClient) -> None:
    g = await gateway_setup(client)
    r = await chat(client, g["key"], stream=True, stream_options={"include_usage": True})
    events = _sse(r.text)
    assert any(e != "[DONE]" and e.get("usage") and not e.get("choices") for e in events)


async def test_stream_failover_before_first_chunk(
    client: httpx.AsyncClient, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client)
    upstreams["mock-a.test"].fail_next("mock-large", 503)
    r = await chat(client, g["key"], stream=True)
    assert r.status_code == 200
    assert r.headers["x-oasis-route-provider"] == "mock_alt"
    assert r.headers["x-oasis-route-fallback"] == "true"
    assert _sse(r.text)[-1] == "[DONE]"


async def test_mid_stream_failure_emits_error_event(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client)
    upstreams["mock-a.test"].stream_fail_after_chunks = 1
    r = await chat(client, g["key"], stream=True)
    assert r.status_code == 200
    events = _sse(r.text)
    assert events[-1] == "[DONE]"
    assert any(e != "[DONE]" and "error" in e for e in events)
    assert len(upstreams["mock-b.test"].calls) == 0  # no silent mid-stream switch
    async with ctx.db.session() as s:
        d = (await s.execute(select(RouteDecision))).scalar_one()
        assert d.outcome == "stream_error"
        res = (await s.execute(select(Reservation))).scalar_one()
        assert res.status == "committed"


async def test_client_disconnect_settles_reservation(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client)
    await _pool(client, g, unit="total_tokens", limit_value=100_000)
    async with ctx.db.session() as s:
        project = await s.get(Project, g["pid"])
        assert project is not None
        s.expunge(project)
    body = {"model": "auto", "stream": True, "messages": [{"role": "user", "content": "a b c d e f"}]}
    parsed = parse_body(body, project, ctx.settings.default_max_output_tokens)
    st = RequestState(request_id="req_disconnect", project=project, api_key_id="k", parsed=parsed)
    engine = Engine(ctx)
    await engine.begin(st)
    d, handle, first = await engine.open_stream(st)
    gen = engine.relay(st, d, handle, first)
    assert (await gen.__anext__()).startswith(b"data: ")
    await gen.aclose()  # what Starlette does when the client goes away
    async with ctx.db.session() as s:
        res = (await s.execute(select(Reservation))).scalar_one()
        assert res.status == "committed"
        pools = (await s.execute(select(QuotaPool))).scalars().all()
        assert all(p.reserved == 0 for p in pools)
        u = (await s.execute(select(UsageRecord))).scalar_one()
        assert u.outcome == "cancelled"
        dec = (await s.execute(select(RouteDecision))).scalar_one()
        assert dec.outcome == "cancelled"


async def test_idempotency_replay_and_conflict(client: httpx.AsyncClient, upstreams: dict[str, Any]) -> None:
    g = await gateway_setup(client)
    h = {"idempotency-key": "abc-123"}
    r1 = await chat(client, g["key"], content="once", headers=h)
    assert r1.status_code == 200
    r2 = await chat(client, g["key"], content="once", headers=h)
    assert r2.status_code == 200 and r2.headers.get("idempotent-replayed") == "true"
    assert r2.json()["id"] == r1.json()["id"]
    assert len(upstreams["mock-a.test"].calls) == 1
    r3 = await chat(client, g["key"], content="different", headers=h)
    assert r3.status_code == 409
    r4 = await chat(client, g["key"], stream=True, headers=h)
    assert r4.status_code == 400


async def test_models_endpoint(client: httpx.AsyncClient) -> None:
    g = await gateway_setup(client)
    r = await client.get("/v1/models", headers=auth(g["key"]))
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()["data"]]
    assert ids[0] == "auto"
    assert "mock/mock-large" in ids and "mock_alt/mock-medium" in ids


async def test_auth_and_scopes(client: httpx.AsyncClient) -> None:
    g = await gateway_setup(client)
    r = await client.post("/v1/chat/completions", json={"model": "auto", "messages": []})
    assert r.status_code == 401
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "auto", "messages": [{"role": "user", "content": "x"}]},
        headers=auth("oasis_test_aaaaaaaaaaaa_notarealsecret"),
    )
    assert r.status_code == 401
    r = await client.post(
        f"/api/v1/projects/{g['pid']}/keys", json={"scopes": ["read"]}, headers=auth(g["key"])
    )
    assert r.status_code == 201
    ro = r.json()["secret"]
    r = await chat(client, ro)
    assert r.status_code == 403


async def test_invalid_bodies(client: httpx.AsyncClient) -> None:
    g = await gateway_setup(client)
    h = {**auth(g["key"]), "content-type": "application/json"}
    r = await client.post("/v1/chat/completions", content=b"{not json", headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_json"
    r = await client.post("/v1/chat/completions", json={"model": "auto"}, headers=auth(g["key"]))
    assert r.status_code == 400
    r = await chat(client, g["key"], n=2)
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"


async def test_local_pool_limit_enforced(client: httpx.AsyncClient, upstreams: dict[str, Any]) -> None:
    g = await gateway_setup(client, two_providers=False)
    await _pool(client, g, limit_value=1)
    assert (await chat(client, g["key"])).status_code == 200
    r = await chat(client, g["key"])
    assert r.status_code == 429, r.text
    assert r.json()["error"]["code"] == "capacity_exhausted"
    assert int(r.headers["retry-after"]) >= 1
    assert len(upstreams["mock-a.test"].calls) == 1


async def test_kill_switch(client: httpx.AsyncClient) -> None:
    g = await gateway_setup(client)
    r = await client.patch(f"/api/v1/projects/{g['pid']}", json={"kill_switch": True}, headers=auth(g["key"]))
    assert r.status_code == 200, r.text
    r = await chat(client, g["key"])
    assert r.status_code == 503 and r.json()["error"]["code"] == "project_disabled"
