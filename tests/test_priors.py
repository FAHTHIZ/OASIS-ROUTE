"""Name-based priors order unrated models without ever counting as quality evidence."""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import update

from oasis_route.context import AppContext
from oasis_route.db.models import Model
from oasis_route.domain.enums import RoutingMode, Task
from oasis_route.routing.priors import PRIOR_SOURCE, model_prior, param_billions
from oasis_route.routing.router import Candidate, rank_key
from tests.helpers import auth, chat, gateway_setup


def test_param_parsing() -> None:
    assert param_billions("llama-3.3-70b-versatile") == 70
    assert param_billions("mixtral-8x7b-32768") == 56
    assert param_billions("qwen-3-235b-a22b-instruct-2507") == 235  # active-parameter suffix ignored
    assert param_billions("meta-llama/llama-3.2-1b-instruct") == 1
    assert param_billions("deepseek-r1") == 671  # known open-weight family
    assert param_billions("gemini-2.5-pro") is None


def test_prior_ordering_is_sensible() -> None:
    stronger_first = [
        ("gemini-2.5-pro", "gemini-2.5-flash"),
        ("gemini-2.5-flash", "gemini-2.5-flash-lite"),
        ("llama-3.3-70b-versatile", "llama-3.1-8b-instant"),
        ("llama-3.1-8b-instant", "meta-llama/llama-3.2-1b-instruct"),
        ("openai/gpt-oss-120b", "openai/gpt-oss-20b"),
        ("mistral-large-latest", "mistral-small-latest"),
        ("gpt-5", "gpt-5-nano"),
        ("deepseek/deepseek-r1:free", "deepseek-r1-distill-llama-70b"),
    ]
    for strong, weak in stronger_first:
        assert model_prior(strong) > model_prior(weak), (strong, weak)


def test_task_specialists_get_a_bump() -> None:
    assert model_prior("codestral-latest", Task.CODING) > model_prior("codestral-latest")
    assert model_prior("o3-mini", Task.REASONING) > model_prior("o3-mini")
    assert model_prior("gemini-2.5-pro", Task.CODING) == model_prior("gemini-2.5-pro")


def test_prior_is_bounded_and_deterministic() -> None:
    for name in ("", "x", "999999b", "nano-tiny-mini-1b", "ultra-opus-pro-max-10000b"):
        v = model_prior(name)
        assert 0.0 <= v <= 100.0
        assert v == model_prior(name)


def _cand(model: str, quality: float | None, prior: float | None) -> Candidate:
    return Candidate(
        connection_id="c",
        provider_id="p",
        adapter_key="openai_compatible",
        account_scope="a",
        multi_account_permitted=False,
        model_id=model,
        upstream_model=model,
        quality=quality,
        quality_source="x" if quality is not None else None,
        reliability=1.0,
        latency_ms=None,
        quota_fraction=None,
        reset_in_s=None,
        quota_metered=False,
        needs_probe=False,
        priority=0,
        preference_rank=0,
        prior=prior,
    )


def test_rated_always_ranks_before_unrated() -> None:
    rated_weak = _cand("rated-weak", 10.0, None)
    unrated_strong = _cand("unrated-strong", None, 99.0)
    unrated_weak = _cand("unrated-weak", None, 20.0)
    for mode in RoutingMode:
        order = sorted([unrated_weak, unrated_strong, rated_weak], key=lambda c: rank_key(c, mode))
        assert [c.model_id for c in order] == ["rated-weak", "unrated-strong", "unrated-weak"], mode


async def test_unrated_models_route_by_prior(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any]
) -> None:
    g = await gateway_setup(client)
    async with ctx.db.transaction() as s:
        await s.execute(update(Model).values(quality={}))
    r = await client.post(f"/api/v1/projects/{g['pid']}/route-preview", json={}, headers=auth(g["key"]))
    assert r.status_code == 200, r.text
    cands = r.json()["candidates"]
    order = [c["upstream_model"] for c in cands]
    assert order.index("mock-large") < order.index("mock-medium") < order.index("mock-small")
    assert all(c["quality"] is None and c["prior_source"] == PRIOR_SOURCE for c in cands)
    # A prior never satisfies a quality floor.
    r = await client.post(
        f"/api/v1/projects/{g['pid']}/route-preview",
        json={"oasis_route": {"min_quality": 1}},
        headers=auth(g["key"]),
    )
    assert r.json()["candidates"] == []
    r = await chat(client, g["key"])
    assert r.status_code == 200, r.text
    assert r.headers["x-oasis-route-model"] == "mock-large"
    models = (await client.get("/v1/models", headers=auth(g["key"]))).json()["data"]
    large = next(m for m in models if m["id"] == "mock/mock-large")
    assert large["oasis_route"]["quality_general"] is None
    assert large["oasis_route"]["quality_prior"] == model_prior("mock-large")
