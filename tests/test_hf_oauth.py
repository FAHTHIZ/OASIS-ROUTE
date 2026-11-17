"""Sign in with Hugging Face: standard OAuth 2.0 + PKCE public client, CIMD, refresh, billing status."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select

from oasis_route.connections import oauth
from oasis_route.connections import service as conns
from oasis_route.context import AppContext
from oasis_route.db.models import Connection, utcnow
from tests.helpers import auth, bootstrap, chat, create_user

PUBLIC = "https://route.example.com"


class FakeHF:
    def __init__(self) -> None:
        self.codes: dict[str, dict[str, str]] = {}
        self.tokens: list[str] = []
        self.refreshes = 0
        self.can_pay = False
        self.refresh_ok = True

    def authorize(self, url: str) -> tuple[str, str]:
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert q["response_type"] == "code" and q["code_challenge_method"] == "S256"
        assert set(q["scope"].split()) == {"openid", "profile", "read-billing", "inference-api"}
        code = f"hf-code-{len(self.codes)}"
        self.codes[code] = q
        return code, q["state"]

    def _issue(self) -> dict[str, Any]:
        tok = f"hf_oauth_{len(self.tokens)}"
        self.tokens.append(tok)
        return {"access_token": tok, "refresh_token": f"r-{tok}", "expires_in": 600, "token_type": "bearer"}

    async def handler(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "huggingface.co" and path == "/oauth/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            assert "client_secret" not in form  # public client
            if form["grant_type"] == "refresh_token":
                self.refreshes += 1
                if not self.refresh_ok or not form["refresh_token"].startswith("r-hf_oauth_"):
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json=self._issue())
            q = self.codes.pop(form.get("code", ""), None)
            digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
            ok = (
                q is not None
                and base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == q["code_challenge"]
                and form["redirect_uri"] == q["redirect_uri"]
                and form["client_id"] == q["client_id"]
            )
            return httpx.Response(200, json=self._issue()) if ok else httpx.Response(400, json={"error": "x"})
        bearer = request.headers.get("authorization", "")[7:]
        if bearer not in self.tokens:
            return httpx.Response(401, json={"error": "unauthorized"})
        if host == "huggingface.co" and path == "/api/whoami-v2":
            return httpx.Response(
                200, json={"type": "user", "name": "u", "canPay": self.can_pay, "isPro": False}
            )
        if host == "router.huggingface.co" and path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "org/model-a", "context_length": 32768}]})
        if host == "router.huggingface.co" and path == "/v1/chat/completions":
            return httpx.Response(
                200,
                json={
                    "id": "c",
                    "object": "chat.completion",
                    "created": 1,
                    "model": json.loads(request.content)["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
                },
            )
        return httpx.Response(404)


@pytest.fixture
def hf(ctx: AppContext) -> FakeHF:
    fake = FakeHF()
    ctx.upstream_transport = httpx.MockTransport(fake.handler)

    async def resolver(host: str, port: int) -> list[str]:
        return ["104.18.2.115"]

    ctx.resolver = resolver
    return fake


async def _user(client: httpx.AsyncClient) -> tuple[str, str]:
    admin = (await bootstrap(client))["api_key"]["secret"]
    u = await create_user(client, admin, "dev@example.com", declared_age=30, country="BD")
    return u["api_key"]["secret"], u["project"]["id"]


async def _start(client: httpx.AsyncClient, key: str, pid: str, **body: Any) -> httpx.Response:
    return await client.post(
        f"/api/v1/projects/{pid}/connections/oauth/start",
        json={"provider_id": "huggingface", "accept_terms": True, **body},
        headers=auth(key),
    )


async def _callback(
    client: httpx.AsyncClient, start: httpx.Response, code: str, state: str
) -> httpx.Response:
    """The binding cookie is Secure (https public_url); the plain-http test client must pass it explicitly."""
    binding = start.cookies.get(oauth.BINDING_COOKIE) or httpx.Cookies(start.cookies).get(
        oauth.BINDING_COOKIE
    )
    if binding is None:
        raw = start.headers.get("set-cookie", "")
        binding = raw.split(f"{oauth.BINDING_COOKIE}=", 1)[1].split(";", 1)[0]
    return await client.get(
        "/api/v1/oauth/callback",
        params={"code": code, "state": state},
        headers={"cookie": f"{oauth.BINDING_COOKIE}={binding}"},
    )


async def test_hf_sign_in_unavailable_without_client_or_https(
    client: httpx.AsyncClient, ctx: AppContext, hf: FakeHF
) -> None:
    key, pid = await _user(client)
    providers = (await client.get("/api/v1/providers", headers=auth(key))).json()
    assert not next(p for p in providers if p["id"] == "huggingface")["oauth_available"]
    r = await _start(client, key, pid)
    assert r.status_code == 422 and "OASIS_ROUTE_HF_OAUTH_CLIENT_ID" in r.text
    assert (await client.get("/.well-known/oauth-cimd")).status_code == 404


async def test_hf_cimd_sign_in_connects_free_account_and_routes(
    client: httpx.AsyncClient, ctx: AppContext, hf: FakeHF
) -> None:
    ctx.settings.public_url = PUBLIC
    key, pid = await _user(client)
    doc = (await client.get("/.well-known/oauth-cimd")).json()
    assert doc["client_id"] == f"{PUBLIC}/.well-known/oauth-cimd"
    assert doc["redirect_uris"] == [f"{PUBLIC}/api/v1/oauth/callback"]
    assert doc["token_endpoint_auth_method"] == "none"

    assert (await _start(client, key, pid, mode="manual")).status_code == 422  # HF needs a redirect
    r = await _start(client, key, pid)
    assert r.status_code == 201, r.text
    url = r.json()["authorization_url"]
    assert url.startswith("https://huggingface.co/oauth/authorize?")
    code, state = hf.authorize(url)
    page = await _callback(client, r, code, state)
    assert page.status_code == 200 and "Connected" in page.text, page.text

    c = (await client.get(f"/api/v1/projects/{pid}/connections", headers=auth(key))).json()[0]
    assert (c["provider_id"], c["auth_method"], c["status"]) == ("huggingface", "oauth", "active")
    # whoami-v2 canPay=false: no payment method, so usage cannot be billed -> free_tier.
    assert c["billing_mode"] == "free_tier" and c["account_status"]["payment_method"] is False
    assert hf.tokens[0] not in json.dumps(c)
    resp = await chat(client, key)
    assert resp.status_code == 200 and resp.headers["x-oasis-route-provider"] == "huggingface", resp.text

    # Refresh: an expiring token is renewed with the refresh_token grant, without a validation gap.
    async with ctx.db.transaction() as s:
        conn = await s.get(Connection, c["id"])
        assert conn is not None
        secret = conns.open_secret(ctx, conn, conn.credential)
        assert secret["refresh_token"] == f"r-{hf.tokens[0]}"
    assert await oauth.refresh_tokens(ctx) == {
        "due": 1,
        "refreshed": 1,
        "failed": 0,
    }  # 10 min < 30 min margin
    async with ctx.db.session() as s:
        conn = (await s.execute(select(Connection).where(Connection.id == c["id"]))).scalar_one()
        assert conn.status == "active"
        assert conns.open_secret(ctx, conn, conn.credential)["api_key"] == hf.tokens[1]
    assert (await chat(client, key)).status_code == 200

    # A rejected refresh suspends the connection (the owner reconnects); never retried in a loop.
    hf.refresh_ok = False
    assert (await oauth.refresh_tokens(ctx))["failed"] == 1
    async with ctx.db.session() as s:
        conn = (await s.execute(select(Connection).where(Connection.id == c["id"]))).scalar_one()
    assert conn.status == "suspended"
    assert (await oauth.refresh_tokens(ctx))["due"] == 0


async def test_hf_account_with_payment_method_is_not_free(
    client: httpx.AsyncClient, ctx: AppContext, hf: FakeHF
) -> None:
    ctx.settings.hf_oauth_client_id = "registered-app"
    hf.can_pay = True
    key, pid = await _user(client)
    r = await _start(client, key, pid)
    q = parse_qs(urlsplit(r.json()["authorization_url"]).query)
    assert q["client_id"] == ["registered-app"]
    code, state = hf.authorize(r.json()["authorization_url"])
    await _callback(client, r, code, state)
    c = (await client.get(f"/api/v1/projects/{pid}/connections", headers=auth(key))).json()[0]
    assert c["billing_mode"] == "unknown" and c["account_status"]["payment_method"] is True
    plan = (await client.post(f"/api/v1/projects/{pid}/route-preview", json={}, headers=auth(key))).json()
    assert plan["candidates"] == [] and {x["reason"] for x in plan["rejected"]} == {"cost_unknown"}


def test_refresh_margin_is_sane() -> None:
    assert timedelta(minutes=5) < oauth.REFRESH_MARGIN < timedelta(hours=8)
    assert utcnow()  # keep import used
