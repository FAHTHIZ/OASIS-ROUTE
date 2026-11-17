import httpx

from tests.helpers import auth, bootstrap


async def test_key_lifecycle_audited_without_secrets(client: httpx.AsyncClient) -> None:
    data = await bootstrap(client)
    admin, pid = data["api_key"]["secret"], data["project"]["id"]
    r = await client.post(f"/api/v1/projects/{pid}/keys", json={"name": "svc"}, headers=auth(admin))
    created = r.json()
    await client.post(f"/api/v1/projects/{pid}/keys/{created['key']['id']}/rotate", headers=auth(admin))
    events = (await client.get(f"/api/v1/projects/{pid}/audit", headers=auth(admin))).json()
    actions = [e["action"] for e in events]
    assert "api_key.created" in actions and "api_key.rotated" in actions
    assert "instance.bootstrapped" in actions
    body = str(events)
    assert created["secret"] not in body and admin not in body
