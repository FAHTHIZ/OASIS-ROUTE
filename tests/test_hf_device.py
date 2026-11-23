"""Hugging Face device-code sign-in (RFC 8628): code shown, user approves elsewhere, server polls."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from oasis_route.context import AppContext
from tests.helpers import auth
from tests.test_hf_oauth import PUBLIC, FakeHF, _start, _user


class FakeHFDevice(FakeHF):
    def __init__(self) -> None:
        super().__init__()
        self.approved = False
        self.denied = False
        self.slow_down_once = False
        self.verification_uri = "https://huggingface.co/oauth/device"
        self.device_requests: list[dict[str, str]] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()} if request.content else {}
        if request.url.host == "huggingface.co" and request.url.path == "/oauth/device":
            self.device_requests.append(form)
            return httpx.Response(
                200,
                json={
                    "device_code": "dev-123",
                    "user_code": "ABCD-1234",
                    "verification_uri": self.verification_uri,
                    "interval": 5,
                    "expires_in": 900,
                },
            )
        if request.url.path == "/oauth/token" and form.get("grant_type", "").endswith("device_code"):
            assert form["device_code"] == "dev-123" and "client_secret" not in form
            if self.slow_down_once:
                self.slow_down_once = False
                return httpx.Response(400, json={"error": "slow_down"})
            if self.denied:
                return httpx.Response(400, json={"error": "access_denied"})
            if not self.approved:
                return httpx.Response(400, json={"error": "authorization_pending"})
            return httpx.Response(200, json=self._issue())
        return await super().handler(request)


@pytest.fixture
def hfd(ctx: AppContext) -> FakeHFDevice:
    fake = FakeHFDevice()
    ctx.upstream_transport = httpx.MockTransport(fake.handler)

    async def resolver(host: str, port: int) -> list[str]:
        return ["104.18.2.115"]

    ctx.resolver = resolver
    ctx.settings.public_url = PUBLIC
    return fake


async def _poll(client: httpx.AsyncClient, key: str, pid: str, fid: str) -> dict[str, Any]:
    r = await client.post(f"/api/v1/projects/{pid}/connections/oauth/{fid}/poll", headers=auth(key))
    assert r.status_code == 200, r.text
    return dict(r.json())


async def test_device_sign_in_connects_after_approval(
    client: httpx.AsyncClient, ctx: AppContext, hfd: FakeHFDevice
) -> None:
    key, pid = await _user(client)
    hf = next(
        p
        for p in (await client.get("/api/v1/providers", headers=auth(key))).json()
        if p["id"] == "huggingface"
    )
    assert hf["oauth_device"] is True
    r = await _start(client, key, pid, mode="device")
    assert r.status_code == 201, r.text
    f = r.json()
    assert f["mode"] == "device" and f["user_code"] == "ABCD-1234" and "set-cookie" not in r.headers
    assert "dev-123" not in r.text  # the device code is a secret; only the user code is shown
    assert hfd.device_requests[0]["client_id"] == f"{PUBLIC}/.well-known/oauth-cimd"

    hfd.slow_down_once = True
    p = await _poll(client, key, pid, f["flow_id"])
    assert p["status"] == "pending" and p["interval"] == 10  # slow_down raises the interval
    assert (await _poll(client, key, pid, f["flow_id"]))["interval"] == 10  # and it sticks

    # A device flow cannot be completed by pasting a code.
    r = await client.post(
        f"/api/v1/projects/{pid}/connections/oauth/{f['flow_id']}/complete",
        json={"code": "x"},
        headers=auth(key),
    )
    assert r.status_code == 422

    hfd.approved = True
    done = await _poll(client, key, pid, f["flow_id"])
    assert done["status"] == "completed" and done["connection_id"], done
    c = (await client.get(f"/api/v1/projects/{pid}/connections", headers=auth(key))).json()[0]
    assert (c["provider_id"], c["auth_method"], c["status"]) == ("huggingface", "oauth", "active")
    # Further polls are idempotent and never mint a second connection.
    assert (await _poll(client, key, pid, f["flow_id"]))["status"] == "completed"
    assert len((await client.get(f"/api/v1/projects/{pid}/connections", headers=auth(key))).json()) == 1


async def test_device_sign_in_denied_and_foreign_hosts_refused(
    client: httpx.AsyncClient, ctx: AppContext, hfd: FakeHFDevice
) -> None:
    key, pid = await _user(client)
    f = (await _start(client, key, pid, mode="device")).json()
    hfd.denied = True
    out = await _poll(client, key, pid, f["flow_id"])
    assert out["status"] == "failed" and "declined" in (out["error"] or "")

    hfd.verification_uri = "https://evil.example/device"
    r = await _start(client, key, pid, mode="device")
    assert r.status_code == 502

    r = await client.post(
        f"/api/v1/projects/{pid}/connections/oauth/start",
        json={"provider_id": "openrouter", "accept_terms": True, "mode": "device"},
        headers=auth(key),
    )
    assert r.status_code == 422
