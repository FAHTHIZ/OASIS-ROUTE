"""Discovery & verification API: sources, offers, evidence, review decisions, change events."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oasis_route.api.deps import Principal, get_ctx, get_session, require_admin, require_scope
from oasis_route.context import AppContext
from oasis_route.db.models import ChangeEvent, Evidence, Offer, OfferTransition, Source
from oasis_route.discovery import review
from oasis_route.discovery import service as disc
from oasis_route.domain.enums import ApiKeyScope, OfferState, SourceClass, SourceReviewStatus
from oasis_route.domain.errors import Conflict, NotFound, ValidationFailed
from oasis_route.identity import audit

router = APIRouter(prefix="/api/v1", tags=["discovery"])
PUBLIC_OFFER_STATES = {OfferState.CONNECTABLE.value, OfferState.ACTIVE.value}


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------------------------------------- sources
class SourceOut(ORM):
    id: str
    url: str
    domain: str
    provider_id: str | None
    source_class: str
    review_status: str
    terms_notes: str
    url_scope: str | None
    crawl_interval_s: int
    min_interval_per_domain_s: float
    retention_days: int
    last_fetched_at: datetime | None
    last_status: str | None
    next_fetch_at: datetime | None
    failure_count: int
    submitted_by: str | None
    created_at: datetime


class SourceIn(BaseModel):
    url: AnyHttpUrl
    source_class: SourceClass = SourceClass.USER_SUBMITTED
    provider_id: str | None = None
    notes: str = Field(default="", max_length=4000)
    url_scope: str | None = None


@router.post("/sources", response_model=SourceOut, status_code=201)
async def submit_source(
    body: SourceIn,
    p: Principal = Depends(require_scope(ApiKeyScope.READ)),
    ctx: AppContext = Depends(get_ctx),
    session: AsyncSession = Depends(get_session),
) -> SourceOut:
    """Admins register sources of any class; other users submit unverified leads for review."""
    src = await disc.create_source(
        session,
        p.user,
        disc.SourceInput(str(body.url), body.source_class, body.provider_id, body.notes, body.url_scope),
        ctx.settings,
    )
    return SourceOut.model_validate(src)


@router.get("/sources", response_model=list[SourceOut])
async def list_sources(
    p: Principal = Depends(require_scope(ApiKeyScope.READ)), session: AsyncSession = Depends(get_session)
) -> list[SourceOut]:
    q = select(Source).order_by(Source.created_at)
    if not p.is_admin:
        q = q.where(Source.submitted_by == p.user.id)
    return [SourceOut.model_validate(s) for s in (await session.execute(q)).scalars()]


class SourceReviewIn(BaseModel):
    status: SourceReviewStatus
    terms_notes: str = Field(default="", max_length=4000)
    crawl_interval_s: int | None = Field(default=None, ge=3600)
    min_interval_per_domain_s: float | None = Field(default=None, ge=1.0)
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    source_class: SourceClass | None = None
    provider_id: str | None = None


@router.post("/admin/sources/{source_id}/review", response_model=SourceOut)
async def review_source(
    source_id: str,
    body: SourceReviewIn,
    p: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> SourceOut:
    src = await session.get(Source, source_id)
    if src is None:
        raise NotFound("source not found")
    await disc.review_source(
        session,
        p.user,
        src,
        disc.SourceReview(
            body.status,
            body.terms_notes,
            body.crawl_interval_s,
            body.min_interval_per_domain_s,
            body.retention_days,
            body.source_class,
            body.provider_id,
        ),
    )
    return SourceOut.model_validate(src)


@router.post("/admin/sources/{source_id}/run")
async def run_source(
    source_id: str, _: Principal = Depends(require_admin), ctx: AppContext = Depends(get_ctx)
) -> dict[str, Any]:
    """Fetch now (still subject to approval, robots, per-domain budgets, and SSRF checks)."""
    r = await disc.run_source(ctx, source_id, force=True)
    return {
        "source_id": r.source_id,
        "status": r.status,
        "changed": r.changed,
        "offers": r.offers,
        "claims": r.claims,
    }


# ------------------------------------------------------------------------------------------- offers
class OfferOut(ORM):
    id: str
    provider_id: str
    title: str
    access_mode: str
    state: str
    quota_claims: list[Any]
    credit_type: str
    eligibility: dict[str, Any]
    terms_url: str | None
    onboarding_url: str | None
    expires_at: datetime | None
    confidence: float
    next_review_at: datetime | None
    created_at: datetime
    updated_at: datetime


class EvidenceOut(ORM):
    id: str
    source_id: str | None
    url: str
    publisher: str | None
    retrieved_at: datetime
    http_status: int | None
    excerpt: str
    sha256: str
    claim_type: str
    claim_value: dict[str, Any]
    method: str
    reviewer_id: str | None
    confidence: float
    next_review_at: datetime | None


class TransitionOut(ORM):
    id: str
    from_state: str | None
    to_state: str
    actor: str
    rationale: str
    evidence_ids: list[Any]
    created_at: datetime


class OfferDetail(OfferOut):
    evidence: list[EvidenceOut] = []
    transitions: list[TransitionOut] = []


@router.get("/offers", response_model=list[OfferOut])
async def list_offers(
    state: OfferState | None = Query(default=None),
    provider_id: str | None = Query(default=None),
    p: Principal = Depends(require_scope(ApiKeyScope.READ)),
    session: AsyncSession = Depends(get_session),
) -> list[OfferOut]:
    """Non-admins see only reviewed, connectable offers; admins see the whole review queue."""
    q = select(Offer).order_by(Offer.provider_id, Offer.title)
    if state is not None:
        q = q.where(Offer.state == state.value)
    if provider_id:
        q = q.where(Offer.provider_id == provider_id)
    if not p.is_admin:
        q = q.where(Offer.state.in_(PUBLIC_OFFER_STATES))
    return [OfferOut.model_validate(o) for o in (await session.execute(q)).scalars()]


@router.get("/offers/{offer_id}", response_model=OfferDetail)
async def get_offer(
    offer_id: str,
    p: Principal = Depends(require_scope(ApiKeyScope.READ)),
    session: AsyncSession = Depends(get_session),
) -> OfferDetail:
    o = await session.get(Offer, offer_id)
    if o is None or (not p.is_admin and o.state not in PUBLIC_OFFER_STATES):
        raise NotFound("offer not found")
    ev = (
        await session.execute(
            select(Evidence).where(Evidence.offer_id == o.id).order_by(Evidence.retrieved_at.desc())
        )
    ).scalars()
    tr = (
        await session.execute(
            select(OfferTransition)
            .where(OfferTransition.offer_id == o.id)
            .order_by(OfferTransition.created_at)
        )
    ).scalars()
    out = OfferDetail.model_validate(o)
    out.evidence = [EvidenceOut.model_validate(e) for e in ev]
    out.transitions = [TransitionOut.model_validate(t) for t in tr]
    return out


class OfferPatch(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    eligibility: dict[str, Any] | None = None
    quota_claims: list[dict[str, Any]] | None = None
    credit_type: str | None = Field(default=None, pattern="^(recurring|one_time|unknown)$")
    terms_url: AnyHttpUrl | None = None
    onboarding_url: AnyHttpUrl | None = None
    expires_at: datetime | None = None


_EDITABLE_STATES = {OfferState.DISCOVERED.value, OfferState.NEEDS_REVIEW.value}
_ELIG_ALLOWED = {
    "min_age",
    "countries_allowed",
    "countries_blocked",
    "commercial_use",
    "transferable",
    "poolable",
    "automated_access",
    "requires_account_cycling",
    "requires_prohibited_practice",
    "region_statements",
}


@router.patch("/admin/offers/{offer_id}", response_model=OfferOut)
async def edit_offer(
    offer_id: str,
    body: OfferPatch,
    p: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> OfferOut:
    """Reviewers correct candidate facts before deciding. Reviewed offers must go back to review first."""
    o = await session.get(Offer, offer_id)
    if o is None:
        raise NotFound("offer not found")
    if o.state not in _EDITABLE_STATES:
        raise Conflict("move the offer to needs_review before editing reviewed facts")
    data = body.model_dump(exclude_unset=True)
    if "eligibility" in data:
        unknown = set(data["eligibility"] or {}) - _ELIG_ALLOWED
        if unknown:
            raise ValidationFailed(f"unknown eligibility fields: {sorted(unknown)}")
    for k, v in data.items():
        setattr(o, k, str(v) if k in ("terms_url", "onboarding_url") and v is not None else v)
    await audit.record(
        session,
        "offer.edit",
        actor_user_id=p.user.id,
        target_type="offer",
        target_id=o.id,
        details={"fields": sorted(data)},
    )
    await session.flush()
    return OfferOut.model_validate(o)


class TransitionIn(BaseModel):
    to_state: OfferState
    rationale: str = Field(default="", max_length=4000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)


@router.post("/admin/offers/{offer_id}/transition", response_model=OfferOut)
async def transition_offer(
    offer_id: str,
    body: TransitionIn,
    p: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> OfferOut:
    o = await session.get(Offer, offer_id)
    if o is None:
        raise NotFound("offer not found")
    await review.transition(
        session,
        o,
        body.to_state,
        actor=p.user.id,
        rationale=body.rationale,
        evidence_ids=body.evidence_ids,
        reviewer=p.user,
    )
    if body.to_state is OfferState.VERIFIED_ELIGIBLE and body.evidence_ids:
        for e in (
            await session.execute(select(Evidence).where(Evidence.id.in_(body.evidence_ids)))
        ).scalars():
            e.reviewer_id, e.method = p.user.id, "human_review"
    return OfferOut.model_validate(o)


# ------------------------------------------------------------------------------------------- changes
class ChangeEventOut(ORM):
    id: str
    source_id: str | None
    offer_id: str | None
    old_hash: str | None
    new_hash: str | None
    severity: str
    summary: str
    action_required: bool
    created_at: datetime


@router.get("/admin/change-events", response_model=list[ChangeEventOut])
async def change_events(
    action_required: bool | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    _: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[ChangeEventOut]:
    q = select(ChangeEvent).order_by(ChangeEvent.created_at.desc()).limit(limit)
    if action_required is not None:
        q = q.where(ChangeEvent.action_required == action_required)
    return [ChangeEventOut.model_validate(c) for c in (await session.execute(q)).scalars()]
