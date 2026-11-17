from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import select

from oasis_route.context import AppContext
from oasis_route.db.models import AuditEvent, Credential, Model, Offer
from tests.helpers import auth, bootstrap, connect_mock, create_user


async def _user(
    client: httpx.AsyncClient, email: str = "alice@example.com", **kw: Any
) -> tuple[str, str, str]:
    admin = await bootstrap(client)
    u = await create_user(client, admin["api_key"]["secret"], email, **kw)
    return admin["api_key"]["secret"], u["api_key"]["secret"], u["project"]["id"]


async def test_connect_validate_and_sync_models(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    _, key, pid = await _user(client)
    c = await connect_mock(client, key, pid, api_key="mock-super-secret-value")
    assert c["status"] == "active" and c["has_credential"]
    assert c["validation"]["models_seen"] == 3
    assert "mock-super-secret-value" not in str(c)
    async with ctx.db.session() as s:
        cred = (await s.execute(select(Credential))).scalar_one()
        assert b"mock-super-secret-value" not in cred.ciphertext
        assert cred.aad == f"connection:{c['id']}"
        audits = (await s.execute(select(AuditEvent))).scalars().all()
        assert "mock-super-secret-value" not in str([a.details for a in audits])
    listed = (await client.get(f"/api/v1/projects/{pid}/connections", headers=auth(key))).json()
    assert [x["id"] for x in listed] == [c["id"]]


async def test_invalid_credential_suspends(client: httpx.AsyncClient, upstreams: dict[str, Any]) -> None:
    _, key, pid = await _user(client)
    c = await connect_mock(client, key, pid, api_key="mock-invalid")
    assert c["status"] == "suspended"
    assert c["validation"]["category"] == "AUTH_INVALID"
    # rotate to a good key -> active again, old credential destroyed
    r = await client.post(
        f"/api/v1/projects/{pid}/connections/{c['id']}/rotate-credential",
        json={"api_key": "mock-good"},
        headers=auth(key),
    )
    assert r.status_code == 200 and r.json()["status"] == "active"


async def test_terms_acceptance_required(client: httpx.AsyncClient) -> None:
    _, key, pid = await _user(client)
    r = await client.post(
        f"/api/v1/projects/{pid}/connections",
        json={"provider_id": "openai", "api_key": "sk-x", "validate_now": False},
        headers=auth(key),
    )
    assert r.status_code == 422


async def test_age_eligibility_enforced(client: httpx.AsyncClient) -> None:
    admin = await bootstrap(client)
    body = {"provider_id": "gemini", "api_key": "g-x", "accept_terms": True, "validate_now": False}
    for email, extra, expected, reason in (
        ("undeclared@example.com", {}, 403, "age_undeclared"),
        ("minor@example.com", {"declared_age": 16}, 403, "under_min_age"),
        ("adult@example.com", {"declared_age": 30}, 201, None),
    ):
        u = await create_user(client, admin["api_key"]["secret"], email, **extra)
        r = await client.post(
            f"/api/v1/projects/{u['project']['id']}/connections",
            json=body,
            headers=auth(u["api_key"]["secret"]),
        )
        assert r.status_code == expected, r.text
        if reason:
            assert reason in str(r.json()["error"]["details"]["reasons"])


async def test_country_restrictions(client: httpx.AsyncClient) -> None:
    admin, key, pid = await _user(client, country="BD")
    r = await client.patch(
        "/api/v1/admin/providers/groq", json={"allowed_countries": ["us", "gb"]}, headers=auth(admin)
    )
    assert r.status_code == 200 and r.json()["allowed_countries"] == ["US", "GB"]
    r = await client.post(
        f"/api/v1/projects/{pid}/connections",
        json={"provider_id": "groq", "api_key": "gsk", "accept_terms": True, "validate_now": False},
        headers=auth(key),
    )
    assert r.status_code == 403 and "country_not_supported" in str(r.json())


async def test_multi_account_rotation_blocked(client: httpx.AsyncClient) -> None:
    _, key, pid = await _user(client)
    base = {"provider_id": "openai", "api_key": "sk-a", "accept_terms": True, "validate_now": False}
    r = await client.post(f"/api/v1/projects/{pid}/connections", json=base, headers=auth(key))
    assert r.status_code == 201
    r = await client.post(
        f"/api/v1/projects/{pid}/connections",
        json={**base, "upstream_account_ref": "second"},
        headers=auth(key),
    )
    assert r.status_code == 403 and "multiple accounts" in r.json()["error"]["message"]
    r = await client.post(f"/api/v1/projects/{pid}/connections", json=base, headers=auth(key))
    assert r.status_code == 409


async def test_official_domain_and_ssrf_guards(client: httpx.AsyncClient, ctx: AppContext) -> None:
    _, key, pid = await _user(client)
    r = await client.post(
        f"/api/v1/projects/{pid}/connections",
        json={
            "provider_id": "openai",
            "api_key": "sk",
            "accept_terms": True,
            "base_url": "https://evil.example.com/v1",
            "validate_now": False,
        },
        headers=auth(key),
    )
    assert r.status_code == 403 and "official domains" in r.json()["error"]["message"]
    for url in ("http://169.254.169.254/v1", "https://10.0.0.5/v1", "https://localhost/v1"):
        r = await client.post(
            f"/api/v1/projects/{pid}/connections",
            json={"provider_id": "custom", "api_key": "x", "accept_terms": True, "base_url": url},
            headers=auth(key),
        )
        assert r.status_code == 403, url


async def test_offer_and_anonymous_gates(client: httpx.AsyncClient, ctx: AppContext) -> None:
    _, key, pid = await _user(client)
    async with ctx.db.transaction() as s:
        o = Offer(provider_id="openai", title="t", dedupe_key="openai:t", state="discovered")
        s.add(o)
        await s.flush()
        oid = o.id
    r = await client.post(
        f"/api/v1/projects/{pid}/connections",
        json={"provider_id": "openai", "api_key": "sk", "accept_terms": True, "offer_id": oid},
        headers=auth(key),
    )
    assert r.status_code == 403 and "offer_not_connectable" in str(r.json())
    r = await client.post(
        f"/api/v1/projects/{pid}/connections",
        json={"provider_id": "openai", "auth_method": "none", "accept_terms": True},
        headers=auth(key),
    )
    assert r.status_code == 403 and "anonymous_access_requires_active_offer" in str(r.json())


async def test_delete_destroys_credential(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    _, key, pid = await _user(client)
    c = await connect_mock(client, key, pid)
    r = await client.delete(f"/api/v1/projects/{pid}/connections/{c['id']}", headers=auth(key))
    assert r.status_code == 204
    async with ctx.db.session() as s:
        assert (await s.execute(select(Credential))).first() is None
    got = (await client.get(f"/api/v1/projects/{pid}/connections/{c['id']}", headers=auth(key))).json()
    assert got["status"] == "revoked" and not got["has_credential"]


async def test_connections_tenant_isolated(client: httpx.AsyncClient, upstreams: dict[str, Any]) -> None:
    admin = await bootstrap(client)
    a = await create_user(client, admin["api_key"]["secret"], "a@example.com")
    b = await create_user(client, admin["api_key"]["secret"], "b@example.com")
    c = await connect_mock(client, a["api_key"]["secret"], a["project"]["id"])
    bk = auth(b["api_key"]["secret"])
    apid = a["project"]["id"]
    assert (await client.get(f"/api/v1/projects/{apid}/connections", headers=bk)).status_code == 404
    assert (await client.get(f"/api/v1/projects/{apid}/connections/{c['id']}", headers=bk)).status_code == 404
    r = await client.get(f"/api/v1/projects/{b['project']['id']}/connections/{c['id']}", headers=bk)
    assert r.status_code == 404
    assert (
        await client.delete(f"/api/v1/projects/{apid}/connections/{c['id']}", headers=bk)
    ).status_code == 404


async def test_quota_pools_crud_and_shared_allocation(client: httpx.AsyncClient) -> None:
    _, key, pid = await _user(client)
    body = {"provider_id": "mock", "unit": "requests", "period": "day", "limit_value": 100}
    r = await client.post(f"/api/v1/projects/{pid}/quota-pools", json=body, headers=auth(key))
    assert r.status_code == 201 and r.json()["available"] == 100
    r2 = await client.post(f"/api/v1/projects/{pid}/quota-pools", json=body, headers=auth(key))
    assert r2.status_code == 409
    unknown = {"provider_id": "mock", "unit": "input_tokens", "period": "day"}
    r3 = await client.post(f"/api/v1/projects/{pid}/quota-pools", json=unknown, headers=auth(key))
    assert r3.status_code == 201 and r3.json()["limit_value"] is None and r3.json()["available"] is None
    r4 = await client.patch(
        f"/api/v1/projects/{pid}/quota-pools/{r.json()['id']}", json={"limit_value": 5}, headers=auth(key)
    )
    assert r4.json()["available"] == 5
    assert len((await client.get(f"/api/v1/projects/{pid}/quota-pools", headers=auth(key))).json()) == 2
    d = await client.delete(f"/api/v1/projects/{pid}/quota-pools/{r.json()['id']}", headers=auth(key))
    assert d.status_code == 204


async def test_model_quality_requires_evidence(client: httpx.AsyncClient, ctx: AppContext) -> None:
    admin, key, _ = await _user(client)
    async with ctx.db.session() as s:
        mid = (await s.execute(select(Model.id).where(Model.upstream_id == "mock-small"))).scalars().first()
    bad = {"quality": {"coding": {"score": 80}}}
    assert (
        await client.patch(f"/api/v1/admin/models/{mid}", json=bad, headers=auth(admin))
    ).status_code == 422
    good = {
        "quality": {
            "coding": {
                "score": 80,
                "source": "https://example.org/benchmark",
                "as_of": "2026-10-01",
                "methodology": "pass@1",
                "confidence": 0.7,
            }
        }
    }
    r = await client.patch(f"/api/v1/admin/models/{mid}", json=good, headers=auth(admin))
    assert r.status_code == 200 and r.json()["quality"]["coding"]["score"] == 80
    assert (
        await client.patch(f"/api/v1/admin/models/{mid}", json=good, headers=auth(key))
    ).status_code == 403
    r = await client.patch(
        "/api/v1/admin/providers/openai",
        json={"data_policy": {"trains_on_inputs": False}},
        headers=auth(admin),
    )
    assert r.status_code == 422
