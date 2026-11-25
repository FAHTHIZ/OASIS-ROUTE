"""Single-use paid-route approvals for the bounded-autonomy spend mode.

A bounded-autonomy project never dispatches to a paid (or unknown-cost) route on its own. When no free
route remains, the gateway answers ``402 paid_approval_required`` with up to three approval options,
each naming the provider, model, estimated worst-case cost and reason, and carrying a token. Resending
the same request with ``"oasis_route": {"approve_paid": "<token>"}`` admits exactly that route, once,
within the TTL, and only if the new worst-case estimate does not exceed the approved one.

Tokens are random; only their SHA-256 is stored. Consumption is a conditional ``UPDATE`` so two
concurrent requests cannot both use one approval.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from oasis_route.db.models import PaidApproval, utcnow

TOKEN_PREFIX = "oapr_"  # noqa: S105 - a public token prefix, not a secret
TTL = timedelta(minutes=15)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issue(
    session: AsyncSession,
    *,
    project_id: str,
    request_id: str,
    connection_id: str,
    provider_id: str,
    upstream_model: str,
    max_cost_usd_micros: int | None,
    cost_status: str,
    reason: str,
) -> tuple[str, PaidApproval]:
    token = TOKEN_PREFIX + secrets.token_urlsafe(24)
    row = PaidApproval(
        project_id=project_id,
        token_hash=_hash(token),
        request_id=request_id[:64],
        connection_id=connection_id,
        provider_id=provider_id,
        upstream_model=upstream_model[:200],
        max_cost_usd_micros=max_cost_usd_micros,
        cost_status=cost_status,
        reason=reason,
        expires_at=utcnow() + TTL,
    )
    session.add(row)
    await session.flush()
    return token, row


@dataclass(frozen=True)
class Lookup:
    approval: PaidApproval | None
    problem: str | None  # approval_unknown | approval_used | approval_expired | None


async def find(session: AsyncSession, project_id: str, token: str) -> Lookup:
    row = (
        await session.execute(
            select(PaidApproval).where(
                PaidApproval.token_hash == _hash(token), PaidApproval.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return Lookup(None, "approval_unknown")
    if row.used_at is not None:
        return Lookup(row, "approval_used")
    if row.expires_at <= utcnow():
        return Lookup(row, "approval_expired")
    return Lookup(row, None)


async def consume(session: AsyncSession, approval_id: str, request_id: str) -> bool:
    """Atomically mark an approval used. False if another request used it first or it expired."""
    now = utcnow()
    res = await session.execute(
        update(PaidApproval)
        .where(
            PaidApproval.id == approval_id,
            PaidApproval.used_at.is_(None),
            PaidApproval.expires_at > now,
        )
        .values(used_at=now, used_by_request_id=request_id[:64])
        .execution_options(synchronize_session=False)
    )
    return (getattr(res, "rowcount", 0) or 0) == 1


def option(token: str, row: PaidApproval, *, price_verified: bool) -> dict[str, Any]:
    micros = row.max_cost_usd_micros
    return {
        "provider": row.provider_id,
        "model": row.upstream_model,
        "connection_id": row.connection_id,
        "cost_status": row.cost_status,
        "estimated_cost_usd": None if micros is None else micros / 1_000_000,
        "estimated_cost_usd_micros": micros,
        "price_verified": price_verified,
        "reason": row.reason,
        "approval_token": token,
        "expires_at": row.expires_at.isoformat(),
        "how_to_approve": {"oasis_route": {"approve_paid": token}},
    }
