"""Discovery pipeline: source registry, polite fetching, change detection, evidence, candidate offers.

Discovery only ever produces *candidates*. Offers it creates start as DISCOVERED and are queued for
human review; changes to reviewed offers suspend them or flag them for re-review, and anything that looks
like it depends on a prohibited practice is marked INELIGIBLE. Nothing here can verify or activate an
offer (see :mod:`oasis_route.discovery.review`).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oasis_route.connections.access import access_for, access_path_for
from oasis_route.context import AppContext
from oasis_route.db.models import (
    ChangeEvent,
    Connection,
    Evidence,
    Model,
    Offer,
    Provider,
    ProviderCandidate,
    Source,
    User,
    utcnow,
)
from oasis_route.discovery import extraction, review, structured
from oasis_route.discovery.fetcher import FetchError, fetcher_for
from oasis_route.discovery.urls import canonicalize, extract_links, is_frontier_candidate
from oasis_route.domain.enums import OfferState, Role, SourceClass, SourceReviewStatus
from oasis_route.domain.errors import Conflict, Forbidden, RateLimited, ValidationFailed
from oasis_route.identity import audit
from oasis_route.notifications.service import notify
from oasis_route.security.ssrf import UnsafeUrlError, check_url_shape

log = logging.getLogger(__name__)

OFFICIAL_CLASSES = {
    SourceClass.OFFICIAL_DOCS,
    SourceClass.OFFICIAL_ANNOUNCEMENT,
    SourceClass.OFFICIAL_REPOSITORY,
}
CLASS_WEIGHT = {
    SourceClass.OFFICIAL_DOCS: 1.0,
    SourceClass.OFFICIAL_ANNOUNCEMENT: 0.9,
    SourceClass.OFFICIAL_REPOSITORY: 0.85,
    SourceClass.MODEL_CATALOGUE: 0.6,
    SourceClass.CURATED_DIRECTORY: 0.4,  # leads, not authoritative
    SourceClass.USER_SUBMITTED: 0.3,
}
_SHARED_CLAIMS = {
    "min_age",
    "region",
    "expiry",
    "terms",
    "restriction",
    "prohibited",
    "model",
    "billing",
    "usage_scope",
}
_TERMINAL = {OfferState.REVOKED.value, OfferState.REJECTED.value, OfferState.INELIGIBLE.value}


def domain_matches(host: str, domains: list[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d.lower() or host.endswith("." + d.lower()) for d in domains)


# --------------------------------------------------------------------------------------------- sources
@dataclass
class SourceInput:
    url: str
    source_class: SourceClass
    provider_id: str | None = None
    notes: str = ""
    url_scope: str | None = None


async def create_source(session: AsyncSession, actor: User, data: SourceInput, settings: Any) -> Source:
    try:
        host, _ = check_url_shape(data.url)
    except UnsafeUrlError as exc:
        raise ValidationFailed(f"source URL rejected: {exc}") from exc
    parts = urlsplit(data.url)
    if parts.fragment:
        raise ValidationFailed("source URL must not contain a fragment")
    data.url = canonicalize(data.url)
    if data.url_scope:
        data.url_scope = canonicalize(data.url_scope) if data.url_scope.count("/") > 2 else data.url_scope
    is_admin = actor.role == Role.ADMIN.value
    cls = data.source_class
    if not is_admin:
        cls = SourceClass.USER_SUBMITTED  # users can only submit leads
        pending = (
            (
                await session.execute(
                    select(Source).where(
                        Source.submitted_by == actor.id,
                        Source.review_status == SourceReviewStatus.PENDING.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(pending) >= settings.discovery_max_pending_submissions_per_user:
            raise RateLimited("too many pending source submissions; wait for review")
    provider: Provider | None = None
    if data.provider_id:
        provider = await session.get(Provider, data.provider_id)
        if provider is None:
            raise ValidationFailed("unknown provider")
    if cls in OFFICIAL_CLASSES and (
        provider is None or not domain_matches(host, list(provider.official_domains or []))
    ):
        raise ValidationFailed("official sources must be on one of the provider's official domains")
    if data.url_scope and not data.url.startswith(data.url_scope):
        raise ValidationFailed("url must be inside url_scope")
    existing = (await session.execute(select(Source).where(Source.url == data.url))).scalar_one_or_none()
    if existing is not None:
        raise Conflict("source already registered")
    src = Source(
        url=data.url,
        domain=host.lower(),
        provider_id=provider.id if provider else None,
        source_class=cls.value,
        review_status=SourceReviewStatus.PENDING.value,  # fetching disabled until terms/robots reviewed
        terms_notes=data.notes[:4000] if is_admin else "",
        url_scope=data.url_scope,
        submitted_by=actor.id,
    )
    session.add(src)
    await session.flush()
    await audit.record(
        session,
        "source.create",
        actor_user_id=actor.id,
        target_type="source",
        target_id=src.id,
        details={
            "url": src.url,
            "class": src.source_class,
            "user_notes": data.notes[:500] if not is_admin else "",
        },
    )
    return src


@dataclass
class SourceReview:
    status: SourceReviewStatus
    terms_notes: str
    crawl_interval_s: int | None = None
    min_interval_per_domain_s: float | None = None
    retention_days: int | None = None
    source_class: SourceClass | None = None
    provider_id: str | None = None


async def review_source(session: AsyncSession, admin: User, src: Source, data: SourceReview) -> Source:
    if admin.role != Role.ADMIN.value:
        raise Forbidden("only an instance admin can review sources")
    if data.status is SourceReviewStatus.APPROVED and len(data.terms_notes.strip()) < 10:
        raise ValidationFailed("record the terms/robots review (>= 10 characters) before approving")
    if data.provider_id is not None:
        if await session.get(Provider, data.provider_id) is None:
            raise ValidationFailed("unknown provider")
        src.provider_id = data.provider_id
    if data.source_class is not None:
        src.source_class = data.source_class.value
    if SourceClass(src.source_class) in OFFICIAL_CLASSES:
        prov = await session.get(Provider, src.provider_id) if src.provider_id else None
        if prov is None or not domain_matches(src.domain, list(prov.official_domains or [])):
            raise ValidationFailed("official sources must be on one of the provider's official domains")
    src.review_status = data.status.value
    src.terms_notes = data.terms_notes[:4000]
    if data.crawl_interval_s is not None:
        src.crawl_interval_s = max(3600, data.crawl_interval_s)
    if data.min_interval_per_domain_s is not None:
        src.min_interval_per_domain_s = max(1.0, data.min_interval_per_domain_s)
    if data.retention_days is not None:
        src.retention_days = max(1, data.retention_days)
    if data.status is SourceReviewStatus.APPROVED and src.next_fetch_at is None:
        src.next_fetch_at = utcnow()
    await audit.record(
        session,
        "source.review",
        actor_user_id=admin.id,
        target_type="source",
        target_id=src.id,
        details={"status": src.review_status},
    )
    return src


# --------------------------------------------------------------------------------------------- running
@dataclass
class RunResult:
    source_id: str
    status: str
    changed: bool = False
    offers: list[str] = field(default_factory=list)
    claims: int = 0
    discovered_sources: int = 0
    provider_candidates: int = 0


def _backoff(src: Source, now: datetime, reason: str) -> datetime:
    base = 3600.0 if reason.startswith(("http_401", "http_403", "http_429")) else 600.0
    delay = min(float(src.crawl_interval_s), base * (2 ** min(src.failure_count, 6)))
    return now + timedelta(seconds=delay)


async def run_source(ctx: AppContext, source_id: str, *, force: bool = False) -> RunResult:
    """Fetch one approved source if due and fold any change into evidence and candidate offers."""
    now = utcnow()
    async with ctx.db.session() as s:
        src = await s.get(Source, source_id)
        if src is None:
            raise ValidationFailed("unknown source")
        if src.review_status != SourceReviewStatus.APPROVED.value:
            return RunResult(source_id, f"not_approved:{src.review_status}")
        if not force and src.next_fetch_at is not None and src.next_fetch_at > now:
            return RunResult(source_id, "not_due")
        url, etag, lm, interval = src.url, src.etag, src.last_modified, src.min_interval_per_domain_s
        scope_host = src.domain
    fetcher = fetcher_for(ctx)
    try:
        res = await fetcher.fetch(
            url, etag=etag, last_modified=lm, min_interval_s=interval, scope_host=scope_host
        )
    except FetchError as exc:
        async with ctx.db.transaction() as s:
            src = await s.get(Source, source_id)
            assert src is not None
            src.failure_count += 1
            src.last_status = exc.reason[:100]
            src.last_fetched_at = now
            src.next_fetch_at = _backoff(src, now, exc.reason)
            if exc.reason in ("http_404", "http_410") and src.failure_count >= 3:
                await _source_gone(s, src)
        ctx.metrics.discovery_fetches.labels("error").inc()
        return RunResult(source_id, exc.reason)

    async with ctx.db.transaction() as s:
        src = await s.get(Source, source_id)
        assert src is not None
        src.last_fetched_at = now
        src.failure_count = 0
        src.next_fetch_at = now + timedelta(seconds=src.crawl_interval_s)
        src.etag = res.headers.get("etag") or src.etag
        src.last_modified = res.headers.get("last-modified") or src.last_modified
        if res.not_modified:
            src.last_status = "not_modified"
            ctx.metrics.discovery_fetches.labels("not_modified").inc()
            return RunResult(source_id, "not_modified")
        ctype = res.headers.get("content-type", "text/html")
        kind = structured.detect(src.url, ctype)
        src.extractor = kind
        if kind in ("openrouter_models", "hf_router_models", "json"):
            parsed = structured.structured_claims(kind, res.text)
            # Catalogue JSON churns (benchmarks, latency stats); only the facts we extract define a change.
            text = json.dumps(
                [[c.claim_type, sorted((k, str(v)) for k, v in c.value.items())] for c in parsed],
                sort_keys=True,
            )
            if kind == "json" and not parsed:
                text = structured.canonical_json(res.text) or extraction.normalise(res.text, ctype)
        else:
            parsed = []
            text = extraction.normalise(res.text, ctype)
        new_hash = extraction.content_hash(text)
        if new_hash == src.content_hash:
            src.last_status = "unchanged"
            await _touch_offers(s, src, now)
            ctx.metrics.discovery_fetches.labels("unchanged").inc()
            return RunResult(source_id, "unchanged")
        old_hash, src.content_hash = src.content_hash, new_hash
        src.last_status = "changed" if old_hash else "first_fetch"
        ctx.metrics.discovery_fetches.labels("changed").inc()
        if kind == "html":
            parsed = structured.table_claims(structured.parse_tables(res.text))
            text_for_claims = text
        else:
            text_for_claims = ""
        result = await _ingest(s, src, text_for_claims, res.status, res.headers, old_hash, new_hash, parsed)
        if kind == "html" and ctx.settings.discovery_frontier_enabled:
            result.discovered_sources = await _expand_frontier(s, ctx, src, res.text)
        return result


async def _touch_offers(s: AsyncSession, src: Source, now: datetime) -> None:
    """An unchanged fetch re-confirms what this source said: keep its offers' observation time fresh."""
    offer_ids = set(
        (await s.execute(select(Evidence.offer_id).where(Evidence.source_id == src.id).distinct())).scalars()
    ) - {None}
    for oid in offer_ids:
        offer = await s.get(Offer, oid)
        if offer is not None:
            offer.last_observed_at = now


async def _resolve_provider(session: AsyncSession, src: Source) -> Provider | None:
    if src.provider_id:
        return await session.get(Provider, src.provider_id)
    for p in (await session.execute(select(Provider))).scalars():
        if domain_matches(src.domain, list(p.official_domains or [])):
            return p
    return None


async def _ingest(
    s: AsyncSession,
    src: Source,
    text: str,
    status: int,
    headers: dict[str, str],
    old_hash: str | None,
    new_hash: str,
    structured_claims: list[extraction.Claim] | None = None,
) -> RunResult:
    provider = await _resolve_provider(s, src)
    known = []
    if provider is not None:
        known = list(
            (await s.execute(select(Model.upstream_id).where(Model.provider_id == provider.id))).scalars()
        )
    ex = extraction.extract(text, known_models=known) if text else extraction.Extraction()
    seen_models = {str(c.value.get("model")) for c in ex.of("model")}
    for c in structured_claims or []:
        if c.claim_type == "model" and str(c.value.get("model")) in seen_models:
            continue
        ex.claims.append(c)
    weight = CLASS_WEIGHT.get(SourceClass(src.source_class), 0.3)
    result = RunResult(src.id, src.last_status or "changed", changed=True, claims=len(ex.claims))
    listings = ex.of("provider_listing")
    if listings:
        result.provider_candidates = await _upsert_candidates(s, src, listings)
    if provider is not None and src.source_class == SourceClass.MODEL_CATALOGUE.value:
        await _sync_catalogue_models(s, provider, ex)
    offer_claims = [c for c in ex.claims if c.claim_type not in ("provider_listing", "catalogue")]
    if provider is None or not offer_claims:
        s.add(
            ChangeEvent(
                source_id=src.id,
                old_hash=old_hash,
                new_hash=new_hash,
                severity="info",
                summary=json.dumps(
                    {
                        "note": "no provider match" if provider is None else "no offer claims found",
                        "claims": len(ex.claims),
                        "provider_candidates": result.provider_candidates,
                        "catalogue": [c.value for c in ex.of("catalogue")],
                    }
                ),
                action_required=(provider is None and bool(offer_claims)) or bool(result.provider_candidates),
            )
        )
        return result

    groups: dict[str, list[extraction.Claim]] = {}
    credit_claims = ex.of("credit") + ex.of("trial")
    tier_claims = (
        ex.of("quota")
        + ex.of("free_tier")
        + [c for c in ex.of("model_quota") if c.value.get("tier") != "paid"]
        + [c for c in ex.of("model") if c.value.get("free") is True]
    )
    shared = [c for c in ex.claims if c.claim_type in _SHARED_CLAIMS and c.value.get("free") is not True]
    # Providers whose API is documented as billed publish rate limits for paying tiers. A passing "free"
    # in the prose is not evidence of a free tier there; only per-model free rows or free models are.
    strong_free = [c for c in ex.of("model_quota") if c.value.get("tier") == "free"] + [
        c for c in ex.of("model") if c.value.get("free") is True
    ]
    paid_provider = provider is not None and access_for(provider.id).default_billing_mode == "paid"
    if tier_claims and paid_provider and not strong_free:
        groups["rate_limits"] = [c for c in tier_claims if c.claim_type != "free_tier"] + shared
    elif tier_claims:
        groups["free_tier"] = tier_claims + shared
    if ex.of("credit"):
        groups["credits"] = credit_claims + shared
    if not groups and shared:
        groups["unspecified"] = shared

    for access_mode, claims in groups.items():
        offer = await _upsert_offer(s, src, provider, access_mode, claims, ex, old_hash, new_hash, weight)
        result.offers.append(offer.id)
        for c in claims:
            s.add(
                Evidence(
                    source_id=src.id,
                    offer_id=offer.id,
                    url=src.url,
                    publisher=provider.name
                    if SourceClass(src.source_class) in OFFICIAL_CLASSES
                    else src.domain,
                    http_status=status,
                    http_meta=headers,
                    excerpt=c.excerpt,
                    sha256=c.sha256,
                    claim_type=c.claim_type,
                    claim_value=c.value,
                    method="structured_extraction"
                    if c.claim_type in ("model_quota", "provider_listing", "catalogue")
                    or c.value.get("source")
                    else "automated_extraction",
                    confidence=round(c.confidence * weight, 3),
                    next_review_at=utcnow() + review.REVIEW_INTERVAL,
                )
            )
    await s.flush()
    return result


async def _upsert_candidates(s: AsyncSession, src: Source, listings: list[extraction.Claim]) -> int:
    """Providers seen in a public catalogue but missing from the registry become reviewable candidates."""
    known = set((await s.execute(select(Provider.id))).scalars())
    now = utcnow()
    created = 0
    for c in listings:
        slug = str(c.value.get("provider") or "")
        if not slug or slug in known:
            continue
        facts: dict[str, Any] = {
            k: c.value.get(k) for k in ("models", "live_models", "free_models", "source") if k in c.value
        }
        row = (
            await s.execute(select(ProviderCandidate).where(ProviderCandidate.slug == slug))
        ).scalar_one_or_none()
        if row is None:
            s.add(
                ProviderCandidate(
                    slug=slug,
                    name=str(c.value.get("provider_name") or slug)[:200],
                    via=str(c.value.get("via") or "")[:64],
                    source_id=src.id,
                    facts=facts,
                    last_seen_at=now,
                )
            )
            created += 1
        else:
            row.facts, row.last_seen_at = facts, now
    return created


async def _sync_catalogue_models(s: AsyncSession, provider: Provider, ex: extraction.Extraction) -> None:
    """Record zero-price models published in the provider's own public catalogue (model + pricing sync).

    Only ``free`` facts from a structured catalogue are written. A model that disappears from the free list
    has its ``free`` flag cleared to unknown, so a free-only project stops routing to it immediately.
    """
    now = utcnow().isoformat()
    free = {str(c.value["model"]): c.value for c in ex.of("model") if c.value.get("free") is True}
    if not free and not ex.of("catalogue"):
        return
    rows = {
        m.upstream_id: m
        for m in (await s.execute(select(Model).where(Model.provider_id == provider.id))).scalars()
    }
    for mid, v in free.items():
        m = rows.get(mid)
        if m is None:
            m = Model(
                provider_id=provider.id,
                upstream_id=mid,
                display_name=mid,
                aliases=[],
                capabilities={"chat": True, "streaming": True, "tools": bool(v.get("tools"))},
                context_window=v.get("context_window"),
                max_output_tokens=v.get("max_output_tokens"),
                lifecycle="active",
                quality={},
            )
            s.add(m)
        m.pricing = {"free": True, "source": v.get("source"), "observed_at": now, "expires": v.get("expires")}
    for mid, m in rows.items():
        p = m.pricing or {}
        if mid not in free and p.get("free") is True and p.get("source") == "openrouter_models_api":
            m.pricing = {**p, "free": None, "observed_at": now, "note": "no longer listed as free"}


def _credit_type(claims: list[extraction.Claim]) -> str:
    types = {str(c.value.get("credit_type")) for c in claims if c.claim_type == "credit"}
    if "one_time" in types:
        return "one_time"
    if "recurring" in types:
        return "recurring"
    return "unknown" if types else "recurring"


def _quota_claims(claims: list[extraction.Claim]) -> list[dict[str, Any]]:
    out = [
        {
            "unit": c.value["unit"],
            "period": c.value["period"],
            "limit": c.value["limit"],
            "model_scope": "*",
            "text": c.excerpt[:200],
        }
        for c in claims
        if c.claim_type == "quota"
    ]
    out += [
        {
            "unit": c.value["unit"],
            "period": c.value["period"],
            "limit": c.value["limit"],
            "model_scope": c.value["model"],
            "tier": c.value.get("tier", "unverified"),
            "text": c.excerpt[:200],
        }
        for c in claims
        if c.claim_type == "model_quota"
    ]
    for c in claims:
        if c.claim_type == "credit":
            amount = int(float(c.value["amount_usd"]) * 1_000_000)
            period = "one_time" if c.value.get("credit_type") == "one_time" else "month"
            out.append(
                {
                    "unit": "credits_usd_micros",
                    "period": period,
                    "limit": amount,
                    "model_scope": "*",
                    "text": c.excerpt[:200],
                }
            )
    return sorted(out, key=lambda q: (q["model_scope"], q["unit"], q["period"], q["limit"]))


def _strip_text(q: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return sorted((x["unit"], x["period"], x["limit"], x.get("model_scope", "*")) for x in q)


def _models_from(claims: list[extraction.Claim], src_id: str) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for c in claims:
        if c.claim_type not in ("model", "model_quota"):
            continue
        mid = str(c.value.get("model") or "")[:200]
        if not mid:
            continue
        entry = out.setdefault(mid, {"model": mid, "free": None, "source_id": src_id})
        if c.value.get("free") is True:
            entry["free"] = True
        for k in ("context_window", "expires"):
            if c.value.get(k) is not None:
                entry[k] = c.value[k]
        if c.claim_type == "model_quota" and c.value.get("tier") == "free":
            entry["free"] = True
    return sorted(out.values(), key=lambda m: m["model"])


def _billing_from(claims: list[extraction.Claim]) -> dict[str, Any]:
    b: dict[str, Any] = {}
    for c in claims:
        if c.claim_type == "billing" and "card_required" in c.value:
            b["card_required"] = bool(c.value["card_required"])
        elif c.claim_type == "trial":
            b["trial_days"] = min(int(c.value["days"]), b.get("trial_days", 10**6))
        elif c.claim_type == "credit":
            b["credit_usd"] = max(float(c.value["amount_usd"]), b.get("credit_usd", 0.0))
            b["renewal"] = (
                "one_time"
                if c.value.get("credit_type") == "one_time"
                else c.value.get("credit_type", "unknown")
            )
        elif c.claim_type == "usage_scope" and c.value.get("evaluation_only"):
            b["evaluation_only_mentioned"] = True
    return b


def _offer_class(access_mode: str, billing: dict[str, Any], provider: Provider) -> str:
    if provider.id.startswith("mock"):
        return "local"
    if access_mode == "credits":
        return (
            "one_time_trial"
            if billing.get("renewal") == "one_time" or billing.get("trial_days")
            else ("recurring_free" if billing.get("renewal") == "recurring" else "promotional")
        )
    if access_mode == "free_tier":
        return "recurring_free"
    if access_mode == "rate_limits":
        return "paid"
    return "unknown"


def _oauth_available(provider_id: str) -> bool:
    """Whether the provider has an official sign-in flow (the per-deployment check is done at read time)."""
    from oasis_route.connections.oauth import SPECS  # local import: oauth depends on connections

    return provider_id in SPECS


def _merge_by_source(existing: list[Any], new: list[dict[str, Any]], src_id: str) -> list[dict[str, Any]]:
    """Each source owns its own entries; one source's fetch never erases facts another source provided."""
    kept = [
        dict(e) for e in existing or [] if isinstance(e, dict) and e.get("source_id") not in (src_id, None)
    ]
    return kept + [{**n, "source_id": src_id} for n in new]


def _own(entries: list[Any], src_id: str) -> list[dict[str, Any]]:
    return [e for e in entries or [] if isinstance(e, dict) and e.get("source_id") in (src_id, None)]


async def _upsert_offer(
    s: AsyncSession,
    src: Source,
    provider: Provider,
    access_mode: str,
    claims: list[extraction.Claim],
    ex: extraction.Extraction,
    old_hash: str | None,
    new_hash: str,
    weight: float,
) -> Offer:
    sub = extraction.Extraction(claims=claims)
    elig = extraction.eligibility_from(sub)
    quota = _quota_claims(claims)
    ctype = _credit_type(claims)
    models = _models_from(claims, src.id)
    billing = _billing_from(claims)
    expiry = [c.value["date"] for c in sub.of("expiry")]
    expires_at = datetime.fromisoformat(min(expiry) + "T23:59:59+00:00") if expiry else None
    confidence = round(max((c.confidence for c in claims), default=0.0) * weight, 3)
    dedupe = f"{provider.id}:{access_mode}"
    offer = (await s.execute(select(Offer).where(Offer.dedupe_key == dedupe))).scalar_one_or_none()
    actor = f"system:discovery:{src.id}"
    now = utcnow()
    info = access_for(provider.id)
    links = {
        k: v
        for k, v in {
            "console": info.console,
            "api_key": info.api_key,
            "signup": info.signup,
            "docs": info.docs,
            "pricing": info.pricing,
            "rate_limits": info.rate_limits,
            "terms": provider.terms_url,
        }.items()
        if v
    }
    if info.card_required is not None and "card_required" not in billing:
        billing["card_required_hint"] = info.card_required
    if offer is None:
        offer = Offer(
            provider_id=provider.id,
            title=f"{provider.name} {access_mode.replace('_', ' ')}",
            access_mode=access_mode,
            state=OfferState.DISCOVERED.value,
            dedupe_key=dedupe,
            quota_claims=[{**q, "source_id": src.id} for q in quota],
            credit_type=ctype,
            eligibility=elig,
            terms_url=provider.terms_url,
            onboarding_url=info.api_key or info.console,
            expires_at=expires_at,
            confidence=confidence,
            offer_class=_offer_class(access_mode, billing, provider),
            access_path=access_path_for(
                provider.id, provider.adapter_key, oauth_available=_oauth_available(provider.id)
            ),
            models=models,
            regions=[],
            billing=billing,
            links=links,
            first_observed_at=now,
            last_observed_at=now,
            extractor_version=structured.EXTRACTOR_VERSION,
        )
        s.add(offer)
        await s.flush()
        s.add(
            ChangeEvent(
                source_id=src.id,
                offer_id=offer.id,
                old_hash=old_hash,
                new_hash=new_hash,
                severity="info",
                summary=json.dumps({"event": "offer_discovered", "claims": len(claims)}),
                action_required=True,
            )
        )
        if elig.get("requires_prohibited_practice"):
            await review.transition(
                s,
                offer,
                OfferState.INELIGIBLE,
                actor=actor,
                rationale=f"prohibited practice indicated: {elig['requires_prohibited_practice']}",
            )
        else:
            await review.transition(
                s,
                offer,
                OfferState.NEEDS_REVIEW,
                actor=actor,
                rationale="automated extraction; awaiting review",
            )
        return offer

    offer.last_observed_at = now
    offer.extractor_version = structured.EXTRACTOR_VERSION
    if offer.first_observed_at is None:
        offer.first_observed_at = now
    if offer.state in _TERMINAL and offer.state != OfferState.INELIGIBLE.value:
        return offer
    state = OfferState(offer.state)
    quota_changed = _strip_text(quota) != _strip_text(_own(offer.quota_claims, src.id))
    models_changed = sorted((m["model"], m.get("free")) for m in models) != sorted(
        (m.get("model"), m.get("free")) for m in _own(offer.models, src.id)
    )
    # Only a *contradiction or new restriction* from the source counts; facts a reviewer added from other
    # evidence are not treated as removed just because this page does not repeat them.
    current = offer.eligibility or {}
    elig_changed = any(current.get(k) != v for k, v in elig.items() if k != "region_statements") or (
        sorted(elig.get("region_statements", [])) != sorted(current.get("region_statements", []))
        and bool(elig.get("region_statements"))
    )
    expiry_changed = expires_at is not None and offer.expires_at != expires_at
    if not (quota_changed or elig_changed or expiry_changed or models_changed):
        offer.confidence = max(offer.confidence, confidence)
        return offer

    diff = {
        "event": "offer_claims_changed",
        "quota": {"old": _own(offer.quota_claims, src.id), "new": quota} if quota_changed else None,
        "models": {
            "added": sorted(
                {m["model"] for m in models} - {m.get("model") for m in _own(offer.models, src.id)}
            ),
            "removed": sorted(
                {str(m.get("model")) for m in _own(offer.models, src.id)} - {m["model"] for m in models}
            ),
        }
        if models_changed
        else None,
        "eligibility": {"old": offer.eligibility, "new": elig} if elig_changed else None,
        "expires_at": {"old": str(offer.expires_at), "new": str(expires_at)} if expiry_changed else None,
    }
    live = state in review.LIVE_STATES
    only_models = models_changed and not (quota_changed or elig_changed or expiry_changed)
    severity = (
        "critical"
        if (live and (elig_changed or elig.get("requires_prohibited_practice")))
        else ("warning" if live and not only_models else "info")
    )
    s.add(
        ChangeEvent(
            source_id=src.id,
            offer_id=offer.id,
            old_hash=old_hash,
            new_hash=new_hash,
            severity=severity,
            summary=json.dumps(diff, default=str)[:20_000],
            action_required=not only_models,
        )
    )
    # Model availability is a catalogue fact (not a reviewed term): keep it current for every state.
    offer.models = _merge_by_source(offer.models, models, src.id)
    if not live:
        # Not yet reviewed: refresh candidate facts in place.
        offer.quota_claims = _merge_by_source(offer.quota_claims, quota, src.id)
        offer.eligibility = {**(offer.eligibility or {}), **elig}
        offer.credit_type = ctype
        offer.billing = {**(offer.billing or {}), **billing}
        offer.links = {**links, **(offer.links or {})}
        offer.expires_at = expires_at or offer.expires_at
        offer.confidence = max(offer.confidence, confidence)
        if elig.get("requires_prohibited_practice") and state is not OfferState.INELIGIBLE:
            await review.transition(
                s, offer, OfferState.INELIGIBLE, actor=actor, rationale="prohibited practice indicated"
            )
        elif state in (OfferState.STALE, OfferState.EXPIRED, OfferState.INELIGIBLE) and not elig.get(
            "requires_prohibited_practice"
        ):
            await review.transition(
                s, offer, OfferState.NEEDS_REVIEW, actor=actor, rationale="source changed; re-review"
            )
        return offer
    if only_models:
        return offer
    # Reviewed offer: never overwrite reviewed facts; be conservative.
    offer.next_review_at = utcnow()
    if elig.get("requires_prohibited_practice"):
        await review.transition(
            s,
            offer,
            OfferState.INELIGIBLE,
            actor=actor,
            rationale="source now indicates a prohibited practice",
        )
        await _notify_connected(
            s, offer, "offer.ineligible", "An offer you use was marked ineligible", "critical"
        )
    elif elig_changed:
        await review.transition(
            s, offer, OfferState.SUSPENDED, actor=actor, rationale="eligibility/terms changed at source"
        )
        await _notify_connected(
            s, offer, "offer.suspended", "An offer you use changed its eligibility or terms", "warning"
        )
    else:
        await _notify_connected(
            s, offer, "offer.changed", "Quota or expiry details changed for an offer you use", "info"
        )
    return offer


async def _expand_frontier(s: AsyncSession, ctx: AppContext, src: Source, raw_html: str) -> int:
    """Bounded link-following from an approved page to more pages on the same provider's official domains.

    A followed page inherits the parent's approval only when it is on one of the provider's official
    domains, its path looks like access/limits/pricing/models documentation, and the depth and per-provider
    budgets allow it. Everything else is ignored (no off-domain leads are auto-approved). Inherited sources
    are fetched through the same fetcher, so robots.txt and rate limits still apply to each one.
    """
    settings = ctx.settings
    if src.depth >= settings.discovery_frontier_max_depth or src.provider_id is None:
        return 0
    provider = await s.get(Provider, src.provider_id)
    if provider is None or SourceClass(src.source_class) not in OFFICIAL_CLASSES:
        return 0
    domains = list(provider.official_domains or [])
    count = len(
        list(
            (
                await s.execute(
                    select(Source.id).where(Source.provider_id == provider.id, Source.origin == "frontier")
                )
            ).scalars()
        )
    )
    budget = min(
        settings.discovery_frontier_links_per_fetch, settings.discovery_frontier_max_per_provider - count
    )
    if budget <= 0:
        return 0
    created = 0
    for link in extract_links(raw_html, src.url):
        if created >= budget:
            break
        host = (urlsplit(link).hostname or "").lower()
        if link == src.url or not domain_matches(host, domains) or not is_frontier_candidate(link):
            continue
        try:
            check_url_shape(link)
        except UnsafeUrlError:
            continue
        if (await s.execute(select(Source.id).where(Source.url == link))).first() is not None:
            continue
        child = Source(
            url=link,
            domain=host,
            provider_id=provider.id,
            source_class=src.source_class,
            review_status=SourceReviewStatus.APPROVED.value,
            terms_notes=(
                f"Inherited approval from parent source {src.url} (same provider, official domain, depth "
                f"{src.depth + 1}). robots.txt is re-checked before every fetch."
            )[:4000],
            crawl_interval_s=max(src.crawl_interval_s, 86_400),
            min_interval_per_domain_s=src.min_interval_per_domain_s,
            retention_days=src.retention_days,
            origin="frontier",
            depth=src.depth + 1,
            parent_id=src.id,
            next_fetch_at=utcnow(),
        )
        s.add(child)
        await s.flush()
        created += 1
    if created:
        s.add(
            ChangeEvent(
                source_id=src.id,
                severity="info",
                summary=json.dumps({"event": "frontier_expanded", "new_sources": created}),
                action_required=False,
            )
        )
    return created


async def _notify_connected(s: AsyncSession, offer: Offer, kind: str, title: str, severity: str) -> None:
    projects = set(
        (await s.execute(select(Connection.project_id).where(Connection.offer_id == offer.id))).scalars()
    )
    for pid in projects:
        await notify(
            s,
            kind=kind,
            title=title,
            body=f"{offer.title}: now '{offer.state}'. Review the offer before relying on it.",
            severity=severity,
            project_id=pid,
            facts={"offer_id": offer.id, "state": offer.state},
            dedupe_key=f"{kind}:{offer.id}:{offer.state}",
        )


async def _source_gone(s: AsyncSession, src: Source) -> None:
    """A source keeps returning 404/410: offers supported only by it become STALE."""
    offer_ids = set(
        (await s.execute(select(Evidence.offer_id).where(Evidence.source_id == src.id))).scalars()
    ) - {None}
    for oid in offer_ids:
        other = (
            await s.execute(
                select(Evidence.id).where(Evidence.offer_id == oid, Evidence.source_id != src.id).limit(1)
            )
        ).first()
        offer = await s.get(Offer, oid)
        if offer is None or other is not None or OfferState(offer.state) not in review.LIVE_STATES:
            continue
        await review.transition(
            s,
            offer,
            OfferState.STALE,
            actor=f"system:discovery:{src.id}",
            rationale="source no longer available",
        )
        await _notify_connected(s, offer, "offer.stale", "An offer's source page disappeared", "warning")
    s.add(
        ChangeEvent(
            source_id=src.id,
            severity="warning",
            summary=json.dumps({"event": "source_gone", "status": src.last_status}),
            action_required=True,
        )
    )


# --------------------------------------------------------------------------------------------- jobs
async def run_due_sources(ctx: AppContext) -> list[RunResult]:
    now = utcnow()
    async with ctx.db.session() as s:
        ids = list(
            (
                await s.execute(
                    select(Source.id)
                    .where(
                        Source.review_status == SourceReviewStatus.APPROVED.value,
                        or_(Source.next_fetch_at.is_(None), Source.next_fetch_at <= now),
                    )
                    .order_by(Source.next_fetch_at)
                    .limit(ctx.settings.discovery_sources_per_tick)
                )
            ).scalars()
        )
    results = []
    for sid in ids:
        try:
            results.append(await run_source(ctx, sid))
        except Exception:  # pragma: no cover - one bad source must not stop the tick
            log.exception("discovery run failed", extra={"source_id": sid})
    return results


async def expire_and_stale_offers(ctx: AppContext) -> dict[str, int]:
    now = utcnow()
    counts = {"expired": 0, "stale": 0}
    stale_cutoff = now - timedelta(days=ctx.settings.offer_stale_after_days)
    async with ctx.db.transaction() as s:
        offers = (
            (
                await s.execute(
                    select(Offer).where(
                        Offer.state.notin_(
                            [OfferState.EXPIRED.value, OfferState.REVOKED.value, OfferState.REJECTED.value]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        for o in offers:
            state = OfferState(o.state)
            if (
                o.expires_at is not None
                and o.expires_at <= now
                and OfferState.EXPIRED in review.ALLOWED[state]
            ):
                await review.transition(
                    s, o, OfferState.EXPIRED, actor="system:expiry", rationale="offer expired"
                )
                await _notify_connected(s, o, "offer.expired", "An offer you use has expired", "warning")
                counts["expired"] += 1
            elif (
                state in review.LIVE_STATES
                and o.next_review_at is not None
                and o.next_review_at < stale_cutoff
            ):
                await review.transition(
                    s, o, OfferState.STALE, actor="system:review", rationale="not re-verified in time"
                )
                await _notify_connected(
                    s, o, "offer.stale", "An offer you use needs re-verification", "warning"
                )
                counts["stale"] += 1
    return counts


async def prune_evidence(ctx: AppContext) -> int:
    """Honour source retention: drop old evidence that does not support a reviewed offer."""
    now = utcnow()
    removed = 0
    async with ctx.db.transaction() as s:
        live_offers = select(Offer.id).where(Offer.state.in_([x.value for x in review.LIVE_STATES]))
        for src in (await s.execute(select(Source))).scalars():
            cutoff = now - timedelta(days=src.retention_days)
            res = await s.execute(
                delete(Evidence).where(
                    Evidence.source_id == src.id,
                    Evidence.retrieved_at < cutoff,
                    or_(Evidence.offer_id.is_(None), Evidence.offer_id.notin_(live_offers)),
                )
            )
            removed += int(getattr(res, "rowcount", 0) or 0)
    return removed
