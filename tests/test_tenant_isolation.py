import httpx

from tests.helpers import auth, bootstrap, create_user


async def test_tenant_cannot_see_other_project(client: httpx.AsyncClient) -> None:
    admin = await bootstrap(client)
    alice = await create_user(client, admin["api_key"]["secret"], "alice@example.com")
    bob = await create_user(client, admin["api_key"]["secret"], "bob@example.com")
    a_key, a_pid = alice["api_key"]["secret"], alice["project"]["id"]
    b_key, b_pid = bob["api_key"]["secret"], bob["project"]["id"]

    for path in (
        f"/api/v1/projects/{b_pid}",
        f"/api/v1/projects/{b_pid}/keys",
        f"/api/v1/projects/{b_pid}/audit",
    ):
        r = await client.get(path, headers=auth(a_key))
        assert r.status_code == 404, path
    r = await client.post(f"/api/v1/projects/{b_pid}/keys", json={}, headers=auth(a_key))
    assert r.status_code == 404
    r = await client.patch(f"/api/v1/projects/{b_pid}", json={"kill_switch": True}, headers=auth(a_key))
    assert r.status_code == 404

    projects = (await client.get("/api/v1/projects", headers=auth(a_key))).json()
    assert [p["id"] for p in projects] == [a_pid]
    # Admin sees everything.
    assert (
        await client.get(f"/api/v1/projects/{b_pid}", headers=auth(admin["api_key"]["secret"]))
    ).status_code == 200
    # Non-admins cannot list users or create users.
    assert (await client.get("/api/v1/users", headers=auth(b_key))).status_code == 403
