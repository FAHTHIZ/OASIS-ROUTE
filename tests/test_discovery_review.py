"""Discovery pipeline and verification workflow (threat T6, P5 acceptance)."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import select, update

from oasis_route.context import AppContext
from oasis_route.db.models import ChangeEvent, Notification, Offer, Source, utcnow
from oasis_route.discovery import service as disc
from tests.conftest import FakeWeb
from tests.helpers import auth, bootstrap, chat, connect_mock, create_user

DOCS = "https://docs.mock.test/limits"
ROBOTS = "https://docs.mock.test/robots.txt"
PAGE_V1 = """<html><body><h1>Mock free tier</h1>
<p>The free tier allows 60 requests per minute and 1,000 requests per day for mock-large.</p>
<p>See the Terms of Service.</p></body></html>"""


async def _setup(client: httpx.AsyncClient, web: FakeWeb) -> dict[str, Any]:
    admin = await bootstrap(client)
    a = admin["api_key"]["secret"]
    u = await create_user(client, a, "dev@example.com", declared_age=30, country="BD")
    web.page(ROBOTS, "User-agent: *\nAllow: /\n", content_type="text/plain")
    web.page(DOCS, PAGE_V1, etag='"v1"')
    return {"admin": a, "admin_user": admin, "key": u["api_key"]["secret"], "pid": u["project"]["id"]}


async def _approved_source(client: httpx.AsyncClient, g: dict[str, Any], url: str = DOCS) -> str:
    r = await client.post(
        "/api/v1/sources",
        json={"url": url, "source_class": "model_catalogue", "provider_id": "mock"},
        headers=auth(g["admin"]),
    )
    assert r.status_code == 201, r.text
    sid = str(r.json()["id"])
    assert r.json()["review_status"] == "pending"
    r = await client.post(
        f"/api/v1/admin/sources/{sid}/review",
        json={"status": "approved", "terms_notes": "robots allows; docs licence permits excerpts"},
        headers=auth(g["admin"]),
    )
    assert r.status_code == 200, r.text
    return sid


async def test_pending_sources_are_never_fetched(
    client: httpx.AsyncClient, ctx: AppContext, web: FakeWeb
) -> None:
    g = await _setup(client, web)
    r = await client.post(
        "/api/v1/sources", json={"url": DOCS, "notes": "found this"}, headers=auth(g["key"])
    )
    assert r.status_code == 201
    assert r.json()["source_class"] == "user_submitted"  # users can only submit leads
    sid = r.json()["id"]
    res = await disc.run_source(ctx, sid, force=True)
    assert res.status == "not_approved:pending"
    assert web.requests == []
    r = await client.post(f"/api/v1/admin/sources/{sid}/run", headers=auth(g["key"]))
    assert r.status_code == 403
    # Users only see their own submissions.
    assert len((await client.get("/api/v1/sources", headers=auth(g["key"]))).json()) == 1
    # Approval requires a recorded terms/robots review.
    r = await client.post(
        f"/api/v1/admin/sources/{sid}/review", json={"status": "approved"}, headers=auth(g["admin"])
    )
    assert r.status_code == 422


async def test_official_sources_require_official_domain(client: httpx.AsyncClient, web: FakeWeb) -> None:
    g = await _setup(client, web)
    r = await client.post(
        "/api/v1/sources",
        json={
            "url": "https://openai-free-keys.test/x",
            "source_class": "official_docs",
            "provider_id": "openai",
        },
        headers=auth(g["admin"]),
    )
    assert r.status_code == 422
    r = await client.post(
        "/api/v1/sources",
        json={
            "url": "https://platform.openai.com/docs/guides/rate-limits",
            "source_class": "official_docs",
            "provider_id": "openai",
        },
        headers=auth(g["admin"]),
    )
    assert r.status_code == 201


async def test_discovery_creates_candidates_with_evidence(
    client: httpx.AsyncClient, ctx: AppContext, web: FakeWeb
) -> None:
    g = await _setup(client, web)
    sid = await _approved_source(client, g)
    r = await client.post(f"/api/v1/admin/sources/{sid}/run", headers=auth(g["admin"]))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "first_fetch" and len(r.json()["offers"]) == 1
    oid = r.json()["offers"][0]
    detail = (await client.get(f"/api/v1/offers/{oid}", headers=auth(g["admin"]))).json()
    assert detail["state"] == "needs_review"
    assert {(q["limit"], q["period"]) for q in detail["quota_claims"]} == {(60, "minute"), (1000, "day")}
    assert detail["evidence"] and all(len(e["sha256"]) == 64 for e in detail["evidence"])
    assert {e["claim_type"] for e in detail["evidence"]} >= {"quota", "model", "terms"}
    assert [t["to_state"] for t in detail["transitions"]] == ["needs_review"]
    assert detail["transitions"][0]["actor"].startswith("system:")
    # Not visible to regular users until reviewed.
    assert (await client.get("/api/v1/offers", headers=auth(g["key"]))).json() == []
    assert (await client.get(f"/api/v1/offers/{oid}", headers=auth(g["key"]))).status_code == 404

    # Conditional re-fetch: 304 -> no change, no new evidence.
    res = await disc.run_source(ctx, sid, force=True)
    assert res.status == "not_modified"
    assert web.requests[-1].headers["if-none-match"] == '"v1"'


async def test_review_state_machine_and_activation(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any], web: FakeWeb
) -> None:
    g = await _setup(client, web)
    sid = await _approved_source(client, g)
    oid = (await client.post(f"/api/v1/admin/sources/{sid}/run", headers=auth(g["admin"]))).json()["offers"][
        0
    ]
    detail = (await client.get(f"/api/v1/offers/{oid}", headers=auth(g["admin"]))).json()
    ev_ids = [e["id"] for e in detail["evidence"]]

    async def go(to: str, **kw: Any) -> httpx.Response:
        return await client.post(
            f"/api/v1/admin/offers/{oid}/transition", json={"to_state": to, **kw}, headers=auth(g["admin"])
        )

    # Users cannot review.
    r = await client.post(
        f"/api/v1/admin/offers/{oid}/transition",
        json={"to_state": "verified_eligible"},
        headers=auth(g["key"]),
    )
    assert r.status_code == 403
    # Skipping states is impossible.
    assert (await go("active", rationale="skip everything please")).status_code == 409
    # Verification needs rationale, evidence and terms URL (mock has none recorded yet).
    assert (await go("verified_eligible", rationale="short")).status_code == 422
    assert (
        await go("verified_eligible", rationale="checked the docs page", evidence_ids=ev_ids)
    ).status_code == 422
    r = await client.patch(
        f"/api/v1/admin/offers/{oid}",
        json={"terms_url": "https://docs.mock.test/terms", "eligibility": {"automated_access": True}},
        headers=auth(g["admin"]),
    )
    assert r.status_code == 200, r.text
    assert (
        await go("verified_eligible", rationale="checked the docs page", evidence_ids=["nope"])
    ).status_code == 422
    r = await go("verified_eligible", rationale="checked the docs page and terms", evidence_ids=ev_ids)
    assert r.status_code == 200, r.text
    # Reviewed facts are frozen.
    r = await client.patch(f"/api/v1/admin/offers/{oid}", json={"title": "x"}, headers=auth(g["admin"]))
    assert r.status_code == 409
    assert (await go("connectable")).status_code == 200
    # Activation needs an authorized, validated connection.
    r = await go("active")
    assert r.status_code == 403 and "authorized" in r.json()["error"]["message"]
    # Now visible to users; the user connects their own account against the offer.
    assert [o["id"] for o in (await client.get("/api/v1/offers", headers=auth(g["key"]))).json()] == [oid]
    await connect_mock(client, g["key"], g["pid"], offer_id=oid)
    async with ctx.db.session() as s:
        offer = await s.get(Offer, oid)
        assert offer is not None and offer.state == "active"
    assert (await chat(client, g["key"])).status_code == 200


async def test_material_change_suspends_reviewed_offer_and_notifies(
    client: httpx.AsyncClient, ctx: AppContext, upstreams: dict[str, Any], web: FakeWeb
) -> None:
    g = await _setup(client, web)
    sid = await _approved_source(client, g)
    oid = (await client.post(f"/api/v1/admin/sources/{sid}/run", headers=auth(g["admin"]))).json()["offers"][
        0
    ]
    async with ctx.db.transaction() as s:
        await s.execute(
            update(Offer).where(Offer.id == oid).values(state="connectable", terms_url="https://t")
        )
    await connect_mock(client, g["key"], g["pid"], offer_id=oid)
    assert (await chat(client, g["key"])).status_code == 200

    # Quota-only change: offer stays usable but is flagged for review.
    web.page(DOCS, PAGE_V1.replace("60 requests", "30 requests"), etag='"v2"')
    res = await disc.run_source(ctx, sid, force=True)
    assert res.status == "changed"
    async with ctx.db.session() as s:
        offer = await s.get(Offer, oid)
        assert offer is not None and offer.state == "active"
        ce = (await s.execute(select(ChangeEvent).where(ChangeEvent.severity == "warning"))).scalars().all()
        assert ce and json.loads(ce[-1].summary)["quota"]["new"]

    # Eligibility change: offer suspended; routing stops using it; project notified.
    web.page(DOCS, PAGE_V1 + "<p>You must be at least 21 years old.</p>", etag='"v3"')
    await disc.run_source(ctx, sid, force=True)
    async with ctx.db.session() as s:
        offer = await s.get(Offer, oid)
        assert offer is not None and offer.state == "suspended"
        kinds = {n.kind for n in (await s.execute(select(Notification))).scalars()}
        assert "offer.suspended" in kinds
    r = await chat(client, g["key"])
    assert r.status_code == 503, r.text


async def test_prohibited_practice_is_ineligible(
    client: httpx.AsyncClient, ctx: AppContext, web: FakeWeb
) -> None:
    g = await _setup(client, web)
    web.page(DOCS, PAGE_V1 + "<p>Pro tip: create new accounts to get more free credits each month.</p>")
    sid = await _approved_source(client, g)
    oid = (await client.post(f"/api/v1/admin/sources/{sid}/run", headers=auth(g["admin"]))).json()["offers"][
        0
    ]
    detail = (await client.get(f"/api/v1/offers/{oid}", headers=auth(g["admin"]))).json()
    assert detail["state"] == "ineligible"
    assert detail["eligibility"]["requires_account_cycling"] is True
    r = await client.post(
        f"/api/v1/admin/offers/{oid}/transition",
        json={"to_state": "needs_review", "rationale": "second look"},
        headers=auth(g["admin"]),
    )
    assert r.status_code == 200
    ev = [e["id"] for e in detail["evidence"]]
    await client.patch(
        f"/api/v1/admin/offers/{oid}", json={"terms_url": "https://t.test"}, headers=auth(g["admin"])
    )
    r = await client.post(
        f"/api/v1/admin/offers/{oid}/transition",
        json={"to_state": "verified_eligible", "rationale": "trying to verify anyway", "evidence_ids": ev},
        headers=auth(g["admin"]),
    )
    assert r.status_code == 403  # a reviewer cannot verify an offer that depends on a prohibited practice


async def test_robots_and_domain_budget_respected(
    client: httpx.AsyncClient, ctx: AppContext, web: FakeWeb
) -> None:
    g = await _setup(client, web)
    web.page(ROBOTS, "User-agent: *\nDisallow: /limits\nCrawl-delay: 30\n", content_type="text/plain")
    sid = await _approved_source(client, g)
    res = await disc.run_source(ctx, sid, force=True)
    assert res.status == "disallowed_by_robots"
    assert web.fetched(DOCS) == 0
    async with ctx.db.session() as s:
        src = await s.get(Source, sid)
        assert src is not None and src.failure_count == 1 and src.next_fetch_at is not None

    # Two pages on the same domain: the second waits for max(budget, crawl-delay).
    web.page(ROBOTS, "User-agent: *\nAllow: /\nCrawl-delay: 30\n", content_type="text/plain")
    from oasis_route.discovery.fetcher import fetcher_for

    f = fetcher_for(ctx)
    f._domains.clear()
    web.page("https://docs.mock.test/a", "<p>a</p>")
    web.page("https://docs.mock.test/b", "<p>b</p>")
    await f.fetch("https://docs.mock.test/a", min_interval_s=10)
    await f.fetch("https://docs.mock.test/b", min_interval_s=10)
    assert web.sleeps and abs(web.sleeps[-1] - 30) < 0.01
    assert all(r.headers["user-agent"].startswith("OasisRouteDiscovery") for r in web.requests)


async def test_expiry_and_staleness_jobs(client: httpx.AsyncClient, ctx: AppContext, web: FakeWeb) -> None:
    g = await _setup(client, web)
    sid = await _approved_source(client, g)
    oid = (await client.post(f"/api/v1/admin/sources/{sid}/run", headers=auth(g["admin"]))).json()["offers"][
        0
    ]
    past = utcnow() - timedelta(days=1)
    async with ctx.db.transaction() as s:
        await s.execute(update(Offer).where(Offer.id == oid).values(state="connectable", expires_at=past))
    assert (await disc.expire_and_stale_offers(ctx))["expired"] == 1
    async with ctx.db.transaction() as s:
        o = await s.get(Offer, oid)
        assert o is not None and o.state == "expired"
        o.state, o.expires_at = "active", None
        o.next_review_at = utcnow() - timedelta(days=ctx.settings.offer_stale_after_days + 1)
    assert (await disc.expire_and_stale_offers(ctx))["stale"] == 1


async def test_user_submission_limit(client: httpx.AsyncClient, ctx: AppContext, web: FakeWeb) -> None:
    g = await _setup(client, web)
    ctx.settings.discovery_max_pending_submissions_per_user = 2
    for i in range(2):
        r = await client.post("/api/v1/sources", json={"url": f"https://x.test/{i}"}, headers=auth(g["key"]))
        assert r.status_code == 201
    r = await client.post("/api/v1/sources", json={"url": "https://x.test/3"}, headers=auth(g["key"]))
    assert r.status_code == 429


FEED = "https://docs.mock.test/releases.atom"
ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Mock releases</title>
<entry><title>New free tier</title><updated>2026-11-20T00:00:00Z</updated>
<content type="html">&lt;p&gt;The free tier allows 30 requests per minute for mock-large.&lt;/p&gt;
</content></entry>
</feed>"""


def test_feed_text_parses_atom_rss_and_refuses_dtds() -> None:
    from oasis_route.discovery import structured

    assert structured.detect(FEED, "application/atom+xml") == "feed"
    assert structured.detect("https://x.test/blog", "application/rss+xml; charset=utf-8") == "feed"
    t = structured.feed_text(ATOM)
    assert "New free tier (2026-11-20T00:00:00Z)" in t and "30 requests per minute" in t
    rss = (
        "<rss><channel><item><title>Credits</title>"
        "<description>$5 free credit</description></item></channel></rss>"
    )
    assert "$5 free credit" in structured.feed_text(rss)
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><feed>&a;</feed>'
    assert structured.feed_text(bomb) == ""
    assert structured.feed_text("not xml <") == ""


async def test_feed_source_feeds_text_claims(
    client: httpx.AsyncClient, ctx: AppContext, web: FakeWeb
) -> None:
    g = await _setup(client, web)
    web.page(FEED, ATOM, content_type="application/atom+xml")
    sid = await _approved_source(client, g, FEED)
    res = await disc.run_source(ctx, sid, force=True)
    assert res.status == "first_fetch" and res.offers
    async with ctx.db.session() as s:
        src = await s.get(Source, sid)
        assert src is not None and src.extractor == "feed"


async def test_extractor_upgrade_reextracts_unchanged_pages(
    client: httpx.AsyncClient, ctx: AppContext, web: FakeWeb
) -> None:
    g = await _setup(client, web)
    sid = await _approved_source(client, g)
    assert (await disc.run_source(ctx, sid, force=True)).status == "first_fetch"
    assert (await disc.run_source(ctx, sid, force=True)).status == "not_modified"
    async with ctx.db.transaction() as s:
        src = await s.get(Source, sid)
        assert src is not None and src.extractor_version is not None
        src.extractor_version = "2000.1.1"  # stored by an older extractor release
    before = web.fetched(DOCS)
    res = await disc.run_source(ctx, sid, force=True)
    assert res.status == "reextracted"
    assert web.requests[-1].headers.get("if-none-match") is None  # unconditional refetch
    assert web.fetched(DOCS) == before + 1
    # Once re-extracted, the ordinary conditional fetch resumes.
    assert (await disc.run_source(ctx, sid, force=True)).status == "not_modified"
