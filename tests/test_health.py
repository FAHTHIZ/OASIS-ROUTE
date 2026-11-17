import httpx


async def test_healthz(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.headers["x-request-id"].startswith("req_")


async def test_readyz_reports_schema(client: httpx.AsyncClient) -> None:
    r = await client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["schema_revision"]


async def test_request_id_propagates(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz", headers={"x-request-id": "abcdef12-3456"})
    assert r.headers["x-request-id"] == "abcdef12-3456"


async def test_metrics_exposed(client: httpx.AsyncClient) -> None:
    await client.get("/healthz")
    r = await client.get("/metrics")
    assert "oasis_http_requests_total" in r.text


async def test_oversized_body_rejected(client: httpx.AsyncClient) -> None:
    r = await client.post("/healthz", content=b"x" * 10, headers={"content-length": str(10**9)})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large"
