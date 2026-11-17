"""Offer verification state machine.

``DISCOVERED -> NEEDS_REVIEW -> VERIFIED_ELIGIBLE -> CONNECTABLE -> ACTIVE`` plus REJECTED, INELIGIBLE,
EXPIRED, SUSPENDED, STALE, REVOKED.

Hard rules (threat T6):
- Automated actors (``system:*``) can only move offers *towards caution*: to NEEDS_REVIEW, INELIGIBLE,
  SUSPENDED, STALE, or EXPIRED. They can never verify, make connectable, or activate an offer.
- VERIFIED_ELIGIBLE requires a human admin, a rationale, linked evidence, a terms URL, and no
  prohibited-practice flags.
- ACTIVE additionally requires at least one active, authorized connection that references the offer.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oasis_route.db.models import Connection, Evidence, Offer, OfferTransition, User, utcnow
from oasis_route.domain.enums import ConnectionStatus, OfferState, Role
from oasis_route.domain.errors import Conflict, Forbidden, PolicyViolation, ValidationFailed
from oasis_route.identity import audit

S = OfferState
ALLOWED: dict[OfferState, set[OfferState]] = {
    S.DISCOVERED: {S.NEEDS_REVIEW, S.REJECTED, S.INELIGIBLE},
    S.NEEDS_REVIEW: {S.VERIFIED_ELIGIBLE, S.REJECTED, S.INELIGIBLE, S.EXPIRED},
    S.VERIFIED_ELIGIBLE: {
        S.CONNECTABLE,
        S.NEEDS_REVIEW,
        S.SUSPENDED,
        S.INELIGIBLE,
        S.EXPIRED,
        S.STALE,
        S.REVOKED,
    },
    S.CONNECTABLE: {S.ACTIVE, S.NEEDS_REVIEW, S.SUSPENDED, S.INELIGIBLE, S.EXPIRED, S.STALE, S.REVOKED},
    S.ACTIVE: {S.CONNECTABLE, S.NEEDS_REVIEW, S.SUSPENDED, S.INELIGIBLE, S.EXPIRED, S.STALE, S.REVOKED},
    S.SUSPENDED: {S.NEEDS_REVIEW, S.INELIGIBLE, S.REVOKED, S.EXPIRED},
    S.STALE: {S.NEEDS_REVIEW, S.EXPIRED, S.REVOKED, S.INELIGIBLE},
    S.EXPIRED: {S.NEEDS_REVIEW},
    S.REJECTED: {S.NEEDS_REVIEW},
    S.INELIGIBLE: {S.NEEDS_REVIEW},
    S.REVOKED: set(),
}
SYSTEM_ALLOWED_TARGETS = {S.NEEDS_REVIEW, S.INELIGIBLE, S.SUSPENDED, S.STALE, S.EXPIRED, S.CONNECTABLE}
# CONNECTABLE is allowed for system only as a demotion from ACTIVE (last connection removed); see below.
LIVE_STATES = {S.VERIFIED_ELIGIBLE, S.CONNECTABLE, S.ACTIVE}
REVIEW_INTERVAL = timedelta(days=30)


def is_system(actor: str) -> bool:
    return actor.startswith("system:")


async def transition(
    session: AsyncSession,
    offer: Offer,
    to: OfferState,
    *,
    actor: str,
    rationale: str = "",
    evidence_ids: list[str] | None = None,
    reviewer: User | None = None,
) -> OfferTransition:
    """Apply one checked transition, recording an OfferTransition row and an audit event."""
    frm = OfferState(offer.state)
    if to == frm:
        raise Conflict(f"offer already {to.value}")
    if to not in ALLOWED[frm]:
        raise Conflict(f"transition {frm.value} -> {to.value} is not allowed")
    evidence_ids = list(evidence_ids or [])
    if is_system(actor):
        if to not in SYSTEM_ALLOWED_TARGETS or (to is S.CONNECTABLE and frm is not S.ACTIVE):
            raise Forbidden(f"automated actors cannot move offers to {to.value}")
    else:
        if reviewer is None or reviewer.role != Role.ADMIN.value or reviewer.id != actor:
            raise Forbidden("only an instance admin can review offers")
        if to in (S.VERIFIED_ELIGIBLE, S.REJECTED, S.INELIGIBLE, S.REVOKED) and len(rationale.strip()) < 10:
            raise ValidationFailed("a rationale (>= 10 characters) is required for this decision")

    if to is S.VERIFIED_ELIGIBLE:
        await _require_verifiable(session, offer, evidence_ids)
    if to is S.CONNECTABLE and frm is S.VERIFIED_ELIGIBLE and not (offer.onboarding_url or offer.terms_url):
        raise ValidationFailed("a connectable offer needs an onboarding or terms URL")
    if to is S.ACTIVE:
        n = (
            await session.execute(
                select(func.count())
                .select_from(Connection)
                .where(Connection.offer_id == offer.id, Connection.status == ConnectionStatus.ACTIVE.value)
            )
        ).scalar_one()
        if not n:
            raise PolicyViolation("an offer becomes active only through an authorized, validated connection")
    if to is S.NEEDS_REVIEW or to in LIVE_STATES:
        offer.next_review_at = utcnow() + REVIEW_INTERVAL

    offer.state = to.value
    t = OfferTransition(
        offer_id=offer.id,
        from_state=frm.value,
        to_state=to.value,
        actor=actor,
        rationale=rationale[:4000],
        evidence_ids=evidence_ids,
    )
    session.add(t)
    await audit.record(
        session,
        "offer.transition",
        actor_user_id=None if is_system(actor) else actor,
        target_type="offer",
        target_id=offer.id,
        details={"from": frm.value, "to": to.value, "actor": actor, "evidence_ids": evidence_ids},
    )
    await session.flush()
    return t


async def _require_verifiable(session: AsyncSession, offer: Offer, evidence_ids: list[str]) -> None:
    elig: dict[str, Any] = offer.eligibility or {}
    if elig.get("requires_prohibited_practice") or elig.get("requires_account_cycling"):
        raise PolicyViolation("offer depends on a prohibited practice and cannot be verified")
    if not offer.terms_url:
        raise ValidationFailed("record the provider terms URL before verifying")
    if not evidence_ids:
        raise ValidationFailed("cite at least one evidence record supporting the verification")
    rows = (await session.execute(select(Evidence).where(Evidence.id.in_(evidence_ids)))).scalars().all()
    if len(rows) != len(set(evidence_ids)) or any(e.offer_id != offer.id for e in rows):
        raise ValidationFailed("evidence must exist and belong to this offer")
    if offer.expires_at is not None and offer.expires_at <= utcnow():
        raise ValidationFailed("offer has already expired")


async def on_connection_validated(session: AsyncSession, offer_id: str) -> None:
    """Promote CONNECTABLE -> ACTIVE once an authorized connection validates (system, verified path)."""
    offer = await session.get(Offer, offer_id)
    if offer is None or offer.state != S.CONNECTABLE.value:
        return
    offer.state = S.ACTIVE.value
    session.add(
        OfferTransition(
            offer_id=offer.id,
            from_state=S.CONNECTABLE.value,
            to_state=S.ACTIVE.value,
            actor="system:connections",
            rationale="first authorized connection validated",
        )
    )
    await audit.record(
        session,
        "offer.transition",
        target_type="offer",
        target_id=offer.id,
        details={"from": "connectable", "to": "active", "actor": "system:connections"},
    )
