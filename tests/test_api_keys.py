import httpx

from oasis_route.security.api_keys import hash_key, issue_key, parse_prefix, verify_key
from tests.helpers import auth, bootstrap


def test_issue_and_verify() -> None:
    k = issue_key(b"pepper")
    assert k.plaintext.startswith("or_live_")
    assert parse_prefix(k.plaintext) == k.prefix
    assert verify_key(k.plaintext, k.key_hash, b"pepper")
    assert not verify_key(k.plaintext + "x", k.key_hash, b"pepper")
    assert not verify_key(k.plaintext, k.key_hash, b"other-pepper")
    assert k.key_hash == hash_key(k.plaintext, b"pepper")
    assert k.plaintext not in k.key_hash


def test_parse_prefix_rejects_garbage() -> None:
    assert parse_prefix("sk-abc") is None
    assert parse_prefix("or_live_short_x") is None


async def test_bootstrap_once_only(client: httpx.AsyncClient) -> None:
    data = await bootstrap(client)
    assert data["api_key"]["secret"].startswith("or_live_")
    r = await client.post(
        "/api/v1/bootstrap", json={"token": "bootstrap-test-token", "email": "x@example.com"}
    )
    assert r.status_code == 409


async def test_bootstrap_wrong_token(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/v1/bootstrap", json={"token": "nope", "email": "x@example.com"})
    assert r.status_code == 401


async def test_secret_shown_once_and_not_listed(client: httpx.AsyncClient) -> None:
    data = await bootstrap(client)
    secret = data["api_key"]["secret"]
    pid = data["project"]["id"]
    r = await client.get(f"/api/v1/projects/{pid}/keys", headers=auth(secret))
    assert r.status_code == 200
    assert secret not in r.text
    assert "key_hash" not in r.text


async def test_revoke_and_rotate(client: httpx.AsyncClient) -> None:
    data = await bootstrap(client)
    admin = data["api_key"]["secret"]
    pid = data["project"]["id"]
    r = await client.post(
        f"/api/v1/projects/{pid}/keys",
        json={"name": "app", "scopes": ["inference", "read"]},
        headers=auth(admin),
    )
    assert r.status_code == 201
    k1 = r.json()
    assert (await client.get("/api/v1/me", headers=auth(k1["secret"]))).status_code == 200

    r = await client.post(f"/api/v1/projects/{pid}/keys/{k1['key']['id']}/rotate", headers=auth(admin))
    assert r.status_code == 200
    k2 = r.json()
    assert k2["key"]["rotated_from_id"] == k1["key"]["id"]
    assert (await client.get("/api/v1/me", headers=auth(k1["secret"]))).status_code == 401
    assert (await client.get("/api/v1/me", headers=auth(k2["secret"]))).status_code == 200

    r = await client.delete(f"/api/v1/projects/{pid}/keys/{k2['key']['id']}", headers=auth(admin))
    assert r.status_code == 200 and r.json()["revoked_at"]
    assert (await client.get("/api/v1/me", headers=auth(k2["secret"]))).status_code == 401


async def test_scope_enforced(client: httpx.AsyncClient) -> None:
    data = await bootstrap(client)
    admin = data["api_key"]["secret"]
    pid = data["project"]["id"]
    r = await client.post(f"/api/v1/projects/{pid}/keys", json={"scopes": ["inference"]}, headers=auth(admin))
    inf = r.json()["secret"]
    r = await client.post(f"/api/v1/projects/{pid}/keys", json={}, headers=auth(inf))
    assert r.status_code == 403
    r = await client.get("/api/v1/me", headers=auth(inf))
    assert r.status_code == 403


async def test_missing_and_malformed_keys(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/me")).status_code == 401
    assert (await client.get("/api/v1/me", headers=auth("or_live_aaaaaaaaaaaa_zzz"))).status_code == 401
    r = await client.get("/api/v1/me", headers=auth("garbage"))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


async def test_rate_limit_per_key(client: httpx.AsyncClient) -> None:
    data = await bootstrap(client)
    admin = data["api_key"]["secret"]
    pid = data["project"]["id"]
    r = await client.patch(f"/api/v1/projects/{pid}", json={"rate_limit_rpm": 2}, headers=auth(admin))
    assert r.status_code == 200
    statuses = [(await client.get("/api/v1/me", headers=auth(admin))).status_code for _ in range(4)]
    assert 429 in statuses
    r = await client.get("/api/v1/me", headers=auth(admin))
    assert r.status_code == 429 and "retry-after" in r.headers
