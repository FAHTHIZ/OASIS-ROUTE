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

from oasis_route.context import AppContext
from oasis_route.db.models import (
    ChangeEvent,
    Connection,
    Evidence,
    Model,
    Offer,
    Provider,
    Source,
    User,
    utcnow,
)
from oasis_route.discovery import extraction, review
from oasis_route.discovery.fetcher import FetchError, fetcher_for
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
_SHARED_CLAIMS = {"min_age", "region", "expiry", "terms", "restriction", "prohibited", "model"}
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
        text = extraction.normalise(res.text, res.headers.get("content-type", "text/html"))
        new_hash = extraction.content_hash(text)
        if new_hash == src.content_hash:
            src.last_status = "unchanged"
            ctx.metrics.discovery_fetches.labels("unchanged").inc()
            return RunResult(source_id, "unchanged")
        old_hash, src.content_hash = src.content_hash, new_hash
        src.last_status = "changed" if old_hash else "first_fetch"
        ctx.metrics.discovery_fetches.labels("changed").inc()
        result = await _ingest(s, src, text, res.status, res.headers, old_hash, new_hash)
        return result


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
) -> RunResult:
    provider = await _resolve_provider(s, src)
    known = []
    if provider is not None:
        known = list(
            (await s.execute(select(Model.upstream_id).where(Model.provider_id == provider.id))).scalars()
        )
    ex = extraction.extract(text, known_models=known)
    weight = CLASS_WEIGHT.get(SourceClass(src.source_class), 0.3)
    result = RunResult(src.id, src.last_status or "changed", changed=True, claims=len(ex.claims))
    if provider is None or not ex.claims:
        s.add(
            ChangeEvent(
                source_id=src.id,
                old_hash=old_hash,
                new_hash=new_hash,
                severity="info",
                summary=json.dumps(
                    {
                        "note": "no provider match" if provider is None else "no claims found",
                        "claims": len(ex.claims),
                    }
                ),
                action_required=provider is None and bool(ex.claims),
            )
        )
        return result

    groups: dict[str, list[extraction.Claim]] = {}
    credit_claims = ex.of("credit")
    tier_claims = ex.of("quota") + ex.of("free_tier")
    shared = [c for c in ex.claims if c.claim_type in _SHARED_CLAIMS]
    if tier_claims:
        groups["free_tier"] = tier_claims + shared
    if credit_claims:
        groups["credits"] = credit_claims + shared
    if not groups:
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
                    method="automated_extraction",
                    confidence=round(c.confidence * weight, 3),
                    next_review_at=utcnow() + review.REVIEW_INTERVAL,
                )
            )
    await s.flush()
    return result


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
    return sorted(out, key=lambda q: (q["unit"], q["period"], q["limit"]))


def _strip_text(q: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return sorted((x["unit"], x["period"], x["limit"], x.get("model_scope", "*")) for x in q)


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
    expiry = [c.value["date"] for c in sub.of("expiry")]
    expires_at = datetime.fromisoformat(min(expiry) + "T23:59:59+00:00") if expiry else None
    confidence = round(max((c.confidence for c in claims), default=0.0) * weight, 3)
    dedupe = f"{provider.id}:{access_mode}"
    offer = (await s.execute(select(Offer).where(Offer.dedupe_key == dedupe))).scalar_one_or_none()
    actor = f"system:discovery:{src.id}"
    if offer is None:
        offer = Offer(
            provider_id=provider.id,
            title=f"{provider.name} {access_mode.replace('_', ' ')}",
            access_mode=access_mode,
            state=OfferState.DISCOVERED.value,
            dedupe_key=dedupe,
            quota_claims=quota,
            credit_type=ctype,
            eligibility=elig,
            terms_url=provider.terms_url,
            expires_at=expires_at,
            confidence=confidence,
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

    if offer.state in _TERMINAL and offer.state != OfferState.INELIGIBLE.value:
        return offer
    state = OfferState(offer.state)
    quota_changed = _strip_text(quota) != _strip_text(offer.quota_claims or [])
    # Only a *contradiction or new restriction* from the source counts; facts a reviewer added from other
    # evidence are not treated as removed just because this page does not repeat them.
    current = offer.eligibility or {}
    elig_changed = any(current.get(k) != v for k, v in elig.items() if k != "region_statements") or (
        sorted(elig.get("region_statements", [])) != sorted(current.get("region_statements", []))
        and bool(elig.get("region_statements"))
    )
    expiry_changed = expires_at is not None and offer.expires_at != expires_at
    if not (quota_changed or elig_changed or expiry_changed):
        offer.confidence = max(offer.confidence, confidence)
        return offer

    diff = {
        "event": "offer_claims_changed",
        "quota": {"old": offer.quota_claims, "new": quota} if quota_changed else None,
        "eligibility": {"old": offer.eligibility, "new": elig} if elig_changed else None,
        "expires_at": {"old": str(offer.expires_at), "new": str(expires_at)} if expiry_changed else None,
    }
    live = state in review.LIVE_STATES
    severity = (
        "critical"
        if (live and (elig_changed or elig.get("requires_prohibited_practice")))
        else ("warning" if live else "info")
    )
    s.add(
        ChangeEvent(
            source_id=src.id,
            offer_id=offer.id,
            old_hash=old_hash,
            new_hash=new_hash,
            severity=severity,
            summary=json.dumps(diff, default=str)[:20_000],
            action_required=True,
        )
    )
    if not live:
        # Not yet reviewed: refresh candidate facts in place.
        offer.quota_claims, offer.eligibility, offer.credit_type = quota, elig, ctype
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
