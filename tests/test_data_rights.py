"""Account data rights: complete secret-free export, and deletion that destroys provider credentials."""

from __future__ import annotations

import json

import httpx
from sqlalchemy import func, select

from oasis_route.context import AppContext
from oasis_route.db.models import (
    ApiKey,
    AuditEvent,
    Connection,
    Credential,
    Project,
    QuotaPool,
    UsageRecord,
    User,
)
from tests.helpers import auth, bootstrap, chat, create_user, gateway_setup


async def _count(ctx: AppContext, model: type, *where: object) -> int:
    async with ctx.db.session() as s:
        return int((await s.execute(select(func.count()).select_from(model).where(*where))).scalar_one())


async def test_project_export_is_complete_and_secret_free(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: object
) -> None:
    g = await gateway_setup(client)
    key, pid = g["key"], g["pid"]
    assert (await chat(client, key)).status_code == 200

    r = await client.get(f"/api/v1/projects/{pid}/export", headers=auth(key))
    assert r.status_code == 200, r.text
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"
    data = r.json()
    assert data["format"] == "oasis-route-export/1"
    assert data["project"]["id"] == pid
    assert {c["provider_id"] for c in data["connections"]} == {"mock", "mock_alt"}
    assert len(data["oasis_keys"]) >= 1
    assert data["usage_records"] and data["route_decisions"]
    assert data["members"][0]["role"] == "owner"
    assert any(e["action"] == "connection.created" for e in data["audit_events"]) or data["audit_events"]

    raw = r.text
    # No secret or secret-derived material leaves the server.
    assert key not in raw
    assert "mock-key" not in raw  # the upstream provider credential
    for k in data["oasis_keys"]:
        assert "key_hash" not in k
    for c in data["connections"]:
        assert "credential_id" not in c
    async with ctx.db.session() as s:
        hashes = (await s.execute(select(ApiKey.key_hash))).scalars().all()
    assert not any(h in raw for h in hashes)
    # The export itself is audited.
    assert await _count(ctx, AuditEvent, AuditEvent.action == "project.exported") == 1


async def test_export_and_delete_are_tenant_isolated(client: httpx.AsyncClient) -> None:
    admin = await bootstrap(client)
    alice = await create_user(client, admin["api_key"]["secret"], "alice@example.com")
    bob = await create_user(client, admin["api_key"]["secret"], "bob@example.com")
    a_key = alice["api_key"]["secret"]
    b_pid, b_uid = bob["project"]["id"], bob["user"]["id"]
    assert (await client.get(f"/api/v1/projects/{b_pid}/export", headers=auth(a_key))).status_code == 404
    r = await client.request(
        "DELETE", f"/api/v1/projects/{b_pid}", json={"confirm": b_pid}, headers=auth(a_key)
    )
    assert r.status_code == 404
    assert (await client.get(f"/api/v1/users/{b_uid}/export", headers=auth(a_key))).status_code == 404
    r = await client.request("DELETE", f"/api/v1/users/{b_uid}", json={"confirm": b_uid}, headers=auth(a_key))
    assert r.status_code == 404


async def test_export_requires_manage_scope(client: httpx.AsyncClient) -> None:
    admin = await bootstrap(client)
    u = await create_user(client, admin["api_key"]["secret"], "dev@example.com")
    key, pid = u["api_key"]["secret"], u["project"]["id"]
    r = await client.post(f"/api/v1/projects/{pid}/keys", json={"scopes": ["read"]}, headers=auth(key))
    read_only = r.json()["secret"]
    assert (await client.get(f"/api/v1/projects/{pid}/export", headers=auth(read_only))).status_code == 403


async def test_delete_project_destroys_credentials_and_revokes_keys(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: object
) -> None:
    g = await gateway_setup(client)
    key, pid = g["key"], g["pid"]
    assert (await chat(client, key)).status_code == 200
    assert await _count(ctx, Credential) == 2

    r = await client.request("DELETE", f"/api/v1/projects/{pid}", json={"confirm": "nope"}, headers=auth(key))
    assert r.status_code == 422
    assert await _count(ctx, Project, Project.id == pid) == 1

    r = await client.request("DELETE", f"/api/v1/projects/{pid}", json={"confirm": pid}, headers=auth(key))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] is True and body["vault_entries_destroyed"] == 2 and body["connections"] == 2

    assert await _count(ctx, Credential) == 0
    for model in (Connection, ApiKey, UsageRecord, QuotaPool):
        assert await _count(ctx, model, model.project_id == pid) == 0  # type: ignore[attr-defined]
    # The project's Oasis Key no longer authenticates.
    assert (await chat(client, key)).status_code == 401
    # The deletion is on the audit trail (audit events are retained).
    async with ctx.db.session() as s:
        ev = (await s.execute(select(AuditEvent).where(AuditEvent.action == "project.deleted"))).scalar_one()
    assert ev.project_id == pid and ev.details["vault_entries_destroyed"] == 2


async def test_user_export_and_self_delete(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: object
) -> None:
    g = await gateway_setup(client)
    key, pid = g["key"], g["pid"]
    me = (await client.get("/api/v1/me", headers=auth(key))).json()
    uid = me["user"]["id"]

    r = await client.get(f"/api/v1/users/{uid}/export", headers=auth(key))
    assert r.status_code == 200
    data = r.json()
    assert data["user"]["email"] == "dev@example.com"
    assert [p["project"]["id"] for p in data["owned_projects"]] == [pid]
    assert "mock-key" not in json.dumps(data)

    r = await client.request("DELETE", f"/api/v1/users/{uid}", json={"confirm": uid}, headers=auth(key))
    assert r.status_code == 200, r.text
    assert r.json()["projects_deleted"] == 1
    assert await _count(ctx, User, User.id == uid) == 0
    assert await _count(ctx, Credential) == 0
    assert (await client.get("/api/v1/me", headers=auth(key))).status_code == 401
    # The administrator is unaffected.
    assert (await client.get("/api/v1/me", headers=auth(g["admin"]))).status_code == 200


async def test_user_delete_refuses_last_admin_and_shared_projects(client: httpx.AsyncClient) -> None:
    admin = await bootstrap(client)
    a_key, a_uid = admin["api_key"]["secret"], admin["user"]["id"]
    r = await client.request("DELETE", f"/api/v1/users/{a_uid}", json={"confirm": a_uid}, headers=auth(a_key))
    assert r.status_code == 409 and "last active administrator" in r.json()["error"]["message"]

    owner = await create_user(client, a_key, "owner@example.com")
    await create_user(client, a_key, "member@example.com")
    o_key, o_pid, o_uid = owner["api_key"]["secret"], owner["project"]["id"], owner["user"]["id"]
    r = await client.post(
        f"/api/v1/projects/{o_pid}/members", json={"email": "member@example.com"}, headers=auth(o_key)
    )
    assert r.status_code == 201
    r = await client.request("DELETE", f"/api/v1/users/{o_uid}", json={"confirm": o_uid}, headers=auth(o_key))
    assert r.status_code == 409 and r.json()["error"]["details"]["project_id"] == o_pid

    # An administrator can delete another user once nothing shared is in the way.
    other = await create_user(client, a_key, "other@example.com")
    ou = other["user"]["id"]
    r = await client.request("DELETE", f"/api/v1/users/{ou}", json={"confirm": ou}, headers=auth(a_key))
    assert r.status_code == 200 and r.json()["projects_deleted"] == 1
