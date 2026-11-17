"""Official provider OAuth (PKCE) connections: one click, no API key handling by the user."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select

from oasis_route.context import AppContext
from oasis_route.db.models import AuditEvent, Connection, Credential, OAuthFlow, utcnow
from oasis_route.worker import runner
from tests.helpers import auth, bootstrap, chat, create_user


class FakeOpenRouter:
    """Implements OpenRouter's documented PKCE exchange and enough of its API to validate and serve."""

    def __init__(self) -> None:
        self.challenges: dict[str, str] = {}  # code -> challenge the user authorized with
        self.exchanges = 0
        self.issued: list[str] = []

    def authorize(self, url: str) -> str:
        """Simulates the user approving on openrouter.ai; returns the one-time code."""
        q = parse_qs(urlsplit(url).query)
        assert q["code_challenge_method"] == ["S256"]
        code = f"code-{len(self.challenges)}"
        self.challenges[code] = q["code_challenge"][0]
        return code

    async def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "openrouter.ai"
        path = request.url.path
        if path == "/api/v1/auth/keys" and request.method == "POST":
            self.exchanges += 1
            body = json.loads(request.content)
            challenge = self.challenges.pop(body.get("code", ""), None)  # single use
            digest = hashlib.sha256(str(body.get("code_verifier", "")).encode()).digest()
            if challenge is None or base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != challenge:
                return httpx.Response(403, json={"error": {"message": "invalid code"}})
            key = f"sk-or-v1-user-{len(self.issued)}"
            self.issued.append(key)
            return httpx.Response(200, json={"key": key, "user_id": "u_1"})
        bearer = request.headers.get("authorization", "")[7:]
        if bearer not in self.issued:
            return httpx.Response(401, json={"error": {"message": "No auth credentials found", "code": 401}})
        if path == "/api/v1/models":
            return httpx.Response(
                200,
                json={"data": [{"id": "meta-llama/llama-3.3-70b-instruct:free", "context_length": 131072}]},
            )
        if path == "/api/v1/chat/completions":
            payload = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "id": "gen-1",
                    "object": "chat.completion",
                    "created": 1,
                    "model": payload["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
                },
            )
        return httpx.Response(404)


@pytest.fixture
def openrouter(ctx: AppContext) -> FakeOpenRouter:
    fake = FakeOpenRouter()
    ctx.upstream_transport = httpx.MockTransport(fake.handler)

    async def resolver(host: str, port: int) -> list[str]:
        return ["104.18.2.115"]  # public address: no private-network exception needed

    ctx.resolver = resolver
    return fake


async def _user(client: httpx.AsyncClient) -> tuple[str, str]:
    admin = (await bootstrap(client))["api_key"]["secret"]
    u = await create_user(client, admin, "dev@example.com", declared_age=30, country="BD")
    client.headers["x-test-admin"] = admin  # lets a test create a second user; never sent upstream
    return u["api_key"]["secret"], u["project"]["id"]


async def _start(client: httpx.AsyncClient, key: str, pid: str, **body: Any) -> httpx.Response:
    return await client.post(
        f"/api/v1/projects/{pid}/connections/oauth/start",
        json={"provider_id": "openrouter", "accept_terms": True, **body},
        headers=auth(key),
    )


async def test_redirect_flow_connects_validates_and_serves(
    client: httpx.AsyncClient, ctx: AppContext, openrouter: FakeOpenRouter
) -> None:
    key, pid = await _user(client)
    providers = (await client.get("/api/v1/providers", headers=auth(key))).json()
    assert {p["id"] for p in providers if p["oauth_available"]} == {"openrouter"}

    r = await _start(client, key, pid)
    assert r.status_code == 201, r.text
    start = r.json()
    url = start["authorization_url"]
    assert url.startswith("https://openrouter.ai/auth?")
    q = parse_qs(urlsplit(url).query)
    assert q["callback_url"] == ["http://testserver/api/v1/oauth/callback"]
    assert "code_verifier" not in url and len(q["state"][0]) >= 40

    code = openrouter.authorize(url)
    page = await client.get("/api/v1/oauth/callback", params={"code": code, "state": q["state"][0]})
    assert page.status_code == 200 and "Connected" in page.text, page.text
    assert (
        page.headers["x-frame-options"] == "DENY"
        and "frame-ancestors 'none'" in page.headers["content-security-policy"]
    )

    flow = (
        await client.get(f"/api/v1/projects/{pid}/connections/oauth/{start['flow_id']}", headers=auth(key))
    ).json()
    assert flow["status"] == "completed" and flow["connection_id"]
    conns = (await client.get(f"/api/v1/projects/{pid}/connections", headers=auth(key))).json()
    assert [(c["provider_id"], c["auth_method"], c["status"]) for c in conns] == [
        ("openrouter", "oauth", "active")
    ]
    assert openrouter.issued[0] not in json.dumps(conns)

    st = (await client.get(f"/api/v1/projects/{pid}/status", headers=auth(key))).json()
    assert st["status"] == "Ready", st
    resp = await chat(client, key)
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-oasis-route-provider"] == "openrouter"

    async with ctx.db.session() as s:
        f = await s.get(OAuthFlow, start["flow_id"])
        assert f is not None and f.verifier_credential_id is None  # verifier shredded after use
        actions = set((await s.execute(select(AuditEvent.action))).scalars())
    assert {"connection.oauth_started", "connection.created"} <= actions

    # The state is single use: replaying the callback cannot create a second connection.
    replay = await client.get("/api/v1/oauth/callback", params={"code": code, "state": q["state"][0]})
    assert replay.status_code == 422 and "already completed" in replay.text
    assert openrouter.exchanges == 1


async def test_manual_flow_for_headless_installs(
    client: httpx.AsyncClient, openrouter: FakeOpenRouter
) -> None:
    key, pid = await _user(client)
    start = (await _start(client, key, pid, mode="manual")).json()
    q = parse_qs(urlsplit(start["authorization_url"]).query)
    assert "callback_url" not in q and "state" not in q
    code = openrouter.authorize(start["authorization_url"])
    r = await client.post(
        f"/api/v1/projects/{pid}/connections/oauth/{start['flow_id']}/complete",
        json={"code": code},
        headers=auth(key),
    )
    assert r.status_code == 200, r.text
    assert r.json()["validation"]["status"] == "active"


async def test_bad_code_fails_cleanly_and_can_be_retried(
    client: httpx.AsyncClient, openrouter: FakeOpenRouter
) -> None:
    key, pid = await _user(client)
    start = (await _start(client, key, pid, mode="manual")).json()
    r = await client.post(
        f"/api/v1/projects/{pid}/connections/oauth/{start['flow_id']}/complete",
        json={"code": "forged"},
        headers=auth(key),
    )
    assert r.status_code == 502 and r.json()["error"]["code"] == "oauth_failed"
    flow = (
        await client.get(f"/api/v1/projects/{pid}/connections/oauth/{start['flow_id']}", headers=auth(key))
    ).json()
    assert flow["status"] == "failed"
    assert (await client.get(f"/api/v1/projects/{pid}/connections", headers=auth(key))).json() == []
    # A fresh attempt works.
    again = (await _start(client, key, pid, mode="manual")).json()
    code = openrouter.authorize(again["authorization_url"])
    ok = await client.post(
        f"/api/v1/projects/{pid}/connections/oauth/{again['flow_id']}/complete",
        json={"code": code},
        headers=auth(key),
    )
    assert ok.status_code == 200, ok.text


async def test_guards(client: httpx.AsyncClient, ctx: AppContext, openrouter: FakeOpenRouter) -> None:
    key, pid = await _user(client)
    # Consent is explicit.
    assert (await _start(client, key, pid, accept_terms=False)).status_code == 422
    # Only providers with an official OAuth flow.
    r = await _start(client, key, pid, provider_id="openai")
    assert r.status_code == 422 and r.json()["error"]["details"]["oauth_providers"] == ["openrouter"]
    # OAuth connections cannot be forged through the API-key endpoint.
    r = await client.post(
        f"/api/v1/projects/{pid}/connections",
        json={"provider_id": "openrouter", "auth_method": "oauth", "api_key": "sk-x", "accept_terms": True},
        headers=auth(key),
    )
    assert r.status_code == 422
    # Unknown or garbage state.
    page = await client.get("/api/v1/oauth/callback", params={"code": "c", "state": "nope"})
    assert page.status_code == 404
    denied = await client.get("/api/v1/oauth/callback", params={"error": "access_denied"})
    assert denied.status_code == 400

    # Another user cannot inspect or complete someone else's flow.
    start = (await _start(client, key, pid, mode="manual")).json()
    other = await create_user(client, client.headers["x-test-admin"], "other@example.com", declared_age=30)
    for method, suffix, body in (("GET", "", None), ("POST", "/complete", {"code": "x"})):
        r = await client.request(
            method,
            f"/api/v1/projects/{pid}/connections/oauth/{start['flow_id']}{suffix}",
            json=body,
            headers=auth(other["api_key"]["secret"]),
        )
        assert r.status_code in (403, 404), r.text

    # Expired flows are refused and their sealed verifier is destroyed by the retention job.
    async with ctx.db.transaction() as s:
        f = await s.get(OAuthFlow, start["flow_id"])
        assert f is not None
        f.expires_at = utcnow() - timedelta(seconds=1)
        cred_id = f.verifier_credential_id
    code = openrouter.authorize(start["authorization_url"])
    r = await client.post(
        f"/api/v1/projects/{pid}/connections/oauth/{start['flow_id']}/complete",
        json={"code": code},
        headers=auth(key),
    )
    assert r.status_code == 422 and "expired" in r.text
    async with ctx.db.session() as s:
        assert await s.get(Credential, cred_id) is None
    out = await runner._retention(ctx)
    assert "oauth_flows_expired" in out


async def test_concurrent_callbacks_exchange_once(
    client: httpx.AsyncClient, ctx: AppContext, openrouter: FakeOpenRouter
) -> None:
    key, pid = await _user(client)
    start = (await _start(client, key, pid)).json()
    q = parse_qs(urlsplit(start["authorization_url"]).query)
    code = openrouter.authorize(start["authorization_url"])
    pages = await asyncio.gather(
        *[
            client.get("/api/v1/oauth/callback", params={"code": code, "state": q["state"][0]})
            for _ in range(4)
        ]
    )
    assert sorted(p.status_code for p in pages).count(200) == 1
    async with ctx.db.session() as s:
        n = len((await s.execute(select(Connection).where(Connection.project_id == pid))).scalars().all())
    assert n == 1 and openrouter.exchanges == 1


async def test_public_url_overrides_callback(
    client: httpx.AsyncClient, ctx: AppContext, openrouter: FakeOpenRouter
) -> None:
    ctx.settings.public_url = "https://route.example.com/"
    key, pid = await _user(client)
    start = (await _start(client, key, pid)).json()
    q = parse_qs(urlsplit(start["authorization_url"]).query)
    assert q["callback_url"] == ["https://route.example.com/api/v1/oauth/callback"]
