"""One-step onboarding: paste keys (or a .env), get validated, free-first connections."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from oasis_route.connections import key_detect
from oasis_route.context import AppContext
from oasis_route.db.models import AuditEvent, Connection, Source
from tests.helpers import auth, bootstrap, create_user

GROQ = "gsk_" + "A1b2C3d4" * 6
GROQ_BAD = "gsk_" + "Z9y8X7w6" * 6
OPENROUTER = "sk-or-v1-" + "0123456789abcdef" * 4
ZAI = "0123456789abcdef0123456789abcdef.AbCdEfGhIjKlMnOp"
COHERE = "Q" * 40
OPENAI = "sk-proj-" + "x" * 40
AMBIGUOUS = "sk-" + "m" * 40


class FakeProviders:
    """Official endpoints of several providers, answering by host and bearer key."""

    good = {GROQ, OPENROUTER, ZAI, COHERE, OPENAI}

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = request.headers.get("authorization", "").removeprefix("Bearer ")
        if key not in self.good:
            return httpx.Response(401, json={"error": {"message": "invalid api key"}})
        host, path = request.url.host, request.url.path
        if host == "api.groq.com" and path == "/openai/v1/models":
            return httpx.Response(200, json={"data": [{"id": "llama-3.3-70b-versatile"}]})
        if host == "openrouter.ai" and path == "/api/v1/models":
            return httpx.Response(200, json={"data": [{"id": "qwen/qwen3-coder:free"}]})
        if host == "openrouter.ai" and path == "/api/v1/key":
            return httpx.Response(200, json={"data": {"is_free_tier": True, "usage": 0}})
        if host == "api.z.ai" and path == "/api/paas/v4/chat/completions":
            body = json.loads(request.content)
            assert body["model"] == "glm-4.5-flash" and body["max_tokens"] == 1
            return httpx.Response(
                200,
                json={
                    "id": "x",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "p"}}],
                },
            )
        if host == "api.cohere.com" and path == "/v1/models":
            assert request.url.params.get("endpoint") == "chat"
            return httpx.Response(
                200, json={"models": [{"name": "command-a-03-2025", "context_length": 256000}]}
            )
        if host == "api.openai.com" and path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gpt-5-mini"}]})
        return httpx.Response(404)


@pytest.fixture
def providers(ctx: AppContext) -> FakeProviders:
    fake = FakeProviders()
    ctx.upstream_transport = httpx.MockTransport(fake.handler)

    async def resolver(host: str, port: int) -> list[str]:
        return ["104.18.2.115"]

    ctx.resolver = resolver
    return fake


async def _user(client: httpx.AsyncClient) -> tuple[str, str]:
    admin = (await bootstrap(client))["api_key"]["secret"]
    u = await create_user(client, admin, "dev@example.com", declared_age=30, country="BD")
    return u["api_key"]["secret"], u["project"]["id"]


ENV = f"""
# my keys
export GROQ_API_KEY="{GROQ}"
OPENROUTER_API_KEY={OPENROUTER}
ZAI_API_KEY='{ZAI}'
CEREBRAS_API_KEY=csk-{"q" * 48}
DATABASE_URL=postgresql://user:hunter2hunter2hunter2@db/app
SECRET_PASSWORD=correct-horse-battery-staple-1234
{AMBIGUOUS}
"""


def test_parse_text_reads_only_provider_keys() -> None:
    parsed = key_detect.parse_text(ENV)
    got = {d.provider_id: d.method for d in parsed.keys}
    assert got == {
        "groq": "prefix",
        "openrouter": "prefix",
        "zai": "prefix",
        "cerebras": "prefix",
        None: "ambiguous",
    }
    assert parsed.ignored_lines == 2
    assert all("hunter2" not in d.api_key and "horse" not in d.api_key for d in parsed.keys)
    amb = next(d for d in parsed.keys if d.provider_id is None)
    assert amb.candidates == ["openai"] and amb.hint == "…" + AMBIGUOUS[-4:]


def test_detect_name_prefix_and_user_choice() -> None:
    assert key_detect.detect("A" * 32, env_name="MISTRAL_API_KEY").provider_id == "mistral"
    assert key_detect.detect("A" * 32).method == "ambiguous"
    # The key format wins over a misleading variable name, and says so.
    d = key_detect.detect(GROQ, env_name="OPENAI_API_KEY")
    assert d.provider_id == "groq" and "OPENAI_API_KEY" in d.note
    # An explicit choice wins over everything.
    assert key_detect.detect(AMBIGUOUS, provider_id="openai").provider_id == "openai"
    assert key_detect.detect("not a key at all").method == "unknown"


async def test_quick_connect_end_to_end(
    client: httpx.AsyncClient, ctx: AppContext, providers: FakeProviders
) -> None:
    key, pid = await _user(client)
    r = await client.post(
        f"/api/v1/projects/{pid}/connections/quick",
        json={
            "text": ENV,
            "keys": [{"api_key": COHERE, "provider_id": "cohere"}],
            "accept_terms": True,
            "free_tier": True,
        },
        headers=auth(key),
    )
    assert r.status_code == 200, r.text
    out = r.json()
    for secret in (GROQ, OPENROUTER, ZAI, COHERE, AMBIGUOUS, "hunter2"):
        assert secret not in r.text
    by = {x["provider_id"]: x for x in out["results"]}
    assert {p: by[p]["outcome"] for p in ("groq", "openrouter", "zai", "cohere", "cerebras")} == {
        "groq": "connected",
        "openrouter": "connected",
        "zai": "connected",
        "cohere": "connected",
        "cerebras": "invalid",
    }
    assert by[None]["outcome"] == "needs_provider" and by[None]["candidates"] == ["openai"]
    assert by["groq"]["billing_mode"] == "free_tier"
    assert by["openrouter"]["billing_mode"] == "free_tier" and by["openrouter"]["free_models"] == 1
    assert by["zai"]["free_models"] == 3
    assert by["cohere"]["models"] == 1
    assert out["connected"] == 4 and out["ignored_lines"] == 2

    # Keys only ever went to the provider that issued them.
    hosts = {(rq.url.host, rq.headers["authorization"].removeprefix("Bearer ")) for rq in providers.requests}
    assert {k for h, k in hosts if h == "api.groq.com"} == {GROQ}
    assert {k for h, k in hosts if h == "api.z.ai"} == {ZAI}

    async with ctx.db.session() as s:
        conns = list((await s.execute(select(Connection).where(Connection.project_id == pid))).scalars())
        assert {c.provider_id for c in conns} == {"groq", "openrouter", "zai", "cohere", "cerebras"}
        ev = (
            await s.execute(select(AuditEvent).where(AuditEvent.action == "connection.quick_connect"))
        ).scalar_one()
        assert ev.details["providers"] == ["cohere", "groq", "openrouter", "zai"]

    # A free-only project can now route to the zero-priced GLM models without any further setup.
    plan = await client.post(
        f"/api/v1/projects/{pid}/route-preview",
        json={},
        headers=auth(key),
    )
    assert plan.status_code == 200, plan.text
    cands = plan.json()["candidates"]
    zai = [c for c in cands if c["provider_id"] == "zai"]
    assert zai and {c["cost_basis"] for c in zai} == {"free_model"}
    assert {c["provider_id"] for c in cands} >= {"zai", "openrouter", "groq", "cohere"}

    # Pasting the same keys again is idempotent.
    again = await client.post(
        f"/api/v1/projects/{pid}/connections/quick",
        json={"text": f"GROQ_API_KEY={GROQ}", "accept_terms": True},
        headers=auth(key),
    )
    assert again.json()["results"][0]["outcome"] == "already_connected"

    # A different key for an already connected provider is a second account: refused.
    other = await client.post(
        f"/api/v1/projects/{pid}/connections/quick",
        json={"keys": [{"api_key": GROQ_BAD}], "accept_terms": True},
        headers=auth(key),
    )
    assert other.json()["results"][0]["outcome"] == "rejected"


async def test_quick_connect_schedules_discovery_for_new_providers(
    client: httpx.AsyncClient, ctx: AppContext, providers: FakeProviders
) -> None:
    from datetime import timedelta

    from oasis_route.db.models import utcnow
    from oasis_route.discovery.seeds import sync_seed_sources

    await sync_seed_sources(ctx.db)
    later = utcnow() + timedelta(days=1)
    async with ctx.db.transaction() as s:
        for src in (await s.execute(select(Source))).scalars():
            src.next_fetch_at = later
    key, pid = await _user(client)
    r = await client.post(
        f"/api/v1/projects/{pid}/connections/quick",
        json={"text": f"GROQ_API_KEY={GROQ}", "accept_terms": True},
        headers=auth(key),
    )
    assert r.json()["connected"] == 1
    async with ctx.db.session() as s:
        rows = list((await s.execute(select(Source))).scalars())
    due = {src.provider_id for src in rows if src.next_fetch_at is not None and src.next_fetch_at < later}
    assert due == {"groq"}


async def test_quick_connect_requires_consent_and_dry_run_stores_nothing(
    client: httpx.AsyncClient, ctx: AppContext, providers: FakeProviders
) -> None:
    key, pid = await _user(client)
    r = await client.post(f"/api/v1/projects/{pid}/connections/quick", json={"text": ENV}, headers=auth(key))
    assert r.status_code == 422 and "hold these provider accounts" in r.text
    dry = await client.post(
        f"/api/v1/projects/{pid}/connections/quick",
        json={"text": ENV, "dry_run": True},
        headers=auth(key),
    )
    assert dry.status_code == 200 and dry.json()["dry_run"] is True
    assert len(dry.json()["results"]) == 5 and GROQ not in dry.text
    assert providers.requests == []
    async with ctx.db.session() as s:
        assert not list((await s.execute(select(Connection))).scalars())


async def test_free_tier_declaration_never_applies_to_billed_providers(
    client: httpx.AsyncClient, providers: FakeProviders
) -> None:
    key, pid = await _user(client)
    r = await client.post(
        f"/api/v1/projects/{pid}/connections/quick",
        json={"keys": [{"api_key": OPENAI}], "accept_terms": True, "free_tier": True},
        headers=auth(key),
    )
    res: dict[str, Any] = r.json()["results"][0]
    assert res["outcome"] == "connected" and res["billing_mode"] == "paid"
    assert "free-only projects will not route" in res["message"]


async def test_dev_connect_env_connects_environment_keys_and_pauses_mocks(
    client: httpx.AsyncClient, ctx: AppContext, providers: FakeProviders, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oasis_route import cli
    from oasis_route.connections import service as conns
    from oasis_route.db.models import User

    admin = await bootstrap(client)
    pid = admin["project"]["id"]
    async with ctx.db.transaction() as s:
        user = (await s.execute(select(User))).scalar_one()
        mock = await conns.create_connection(
            ctx,
            s,
            project_id=pid,
            user=user,
            data=conns.ConnectionInput(provider_id="mock", secret={"api_key": "m"}, accept_terms=True),
        )
        mock.status = "active"
    for name in key_detect.ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GROQ_API_KEY", GROQ)
    monkeypatch.setenv("DATABASE_PASSWORD", "not-a-provider-key")
    await cli._dev_connect_env(ctx, pid, free_tier=True, age=30, country="bd")
    await cli._dev_connect_env(ctx, pid, free_tier=True)  # idempotent on restart
    async with ctx.db.session() as s:
        rows = {c.provider_id: c for c in (await s.execute(select(Connection))).scalars()}
        user = (await s.execute(select(User))).scalar_one()
    assert set(rows) == {"mock", "groq"} and rows["groq"].status == "active"
    assert rows["mock"].status == "suspended"  # `auto` never answers with mock output now
    assert (user.declared_age, user.country) == (30, "BD")


async def test_dry_run_reports_lines_and_choice_resolves_ambiguous_key(
    client: httpx.AsyncClient, ctx: AppContext, providers: FakeProviders
) -> None:
    key, pid = await _user(client)
    text = f"# my keys\n{AMBIGUOUS}\nGROQ_API_KEY={GROQ}\n"
    url = f"/api/v1/projects/{pid}/connections/quick"
    r = await client.post(url, json={"text": text, "dry_run": True}, headers=auth(key))
    rows = {x["line"]: x for x in r.json()["results"]}
    assert rows[2]["detected_by"] == "ambiguous" and "openai" in rows[2]["candidates"]
    assert rows[3]["provider_id"] == "groq"
    r = await client.post(
        url,
        json={"text": text, "accept_terms": True, "providers_by_line": {"2": "openai"}},
        headers=auth(key),
    )
    out = {x["provider_id"]: x for x in r.json()["results"]}
    assert set(out) == {"openai", "groq"} and out["openai"]["detected_by"] == "user", r.text
    # The chosen provider, and only it, was asked about the ambiguous key.
    asked = {
        q.url.host for q in providers.requests if q.headers.get("authorization") == f"Bearer {AMBIGUOUS}"
    }
    assert asked == {"api.openai.com"}
