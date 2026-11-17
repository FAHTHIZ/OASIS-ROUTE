"""Per-connection / per-model circuit breaker (docs/ROUTING_AND_QUOTAS.md "Health and failover").

States:
- ``healthy``  — routable.
- ``degraded`` — routable, but recent failures lower its reliability score.
- ``open``     — cooling down until ``open_until``; not routable.
- ``half_open`` — cooldown elapsed and exactly one probe request has been admitted (claimed atomically).

Cooldowns use bounded exponential backoff with jitter. A provider ``Retry-After`` sets the cooldown directly
(bounded by ``breaker_max_cooldown_s``) so we never hammer a provider that asked us to wait.

Records are keyed by (connection_id, model_scope). ``model_scope="*"`` represents connection-wide health
(auth, transport, provider outage); a concrete upstream model id represents model-level health.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from oasis_route.config import Settings
from oasis_route.db.models import HealthRecord, utcnow
from oasis_route.db.session import Database
from oasis_route.domain.enums import ErrorCategory, HealthState

CONNECTION_SCOPE = "*"
EWMA_ALPHA = 0.2
PROBE_LEASE_S = 60.0  # a probe holder that never reports back releases the probe after this

# Failures that count against health. Quota is handled by the ledger and auth by connection suspension;
# request-shape and policy errors say nothing about provider health.
_COUNTED = {
    ErrorCategory.PROVIDER_UNAVAILABLE,
    ErrorCategory.TIMEOUT,
    ErrorCategory.UNKNOWN_UPSTREAM,
    ErrorCategory.MODEL_UNAVAILABLE,
    ErrorCategory.RATE_LIMITED,
}
# Failures that open the circuit immediately rather than after N consecutive failures.
_OPEN_IMMEDIATELY = {ErrorCategory.RATE_LIMITED, ErrorCategory.MODEL_UNAVAILABLE}


def scope_for(category: ErrorCategory, upstream_model: str) -> str:
    return upstream_model if category is ErrorCategory.MODEL_UNAVAILABLE else CONNECTION_SCOPE


@dataclass(frozen=True)
class Availability:
    routable: bool
    needs_probe: bool = False  # routable only if a half-open probe is claimed
    reason: str | None = None
    retry_at: datetime | None = None
    reliability: float = 1.0  # 0..1, used for ranking


def cooldown_seconds(settings: Settings, level: int, retry_after_s: float | None) -> float:
    if retry_after_s is not None and retry_after_s > 0:
        return min(settings.breaker_max_cooldown_s, retry_after_s)
    base = settings.breaker_base_cooldown_s * (2 ** max(0, level - 1))
    jittered = base * random.uniform(0.8, 1.2)
    return float(min(settings.breaker_max_cooldown_s, jittered))


def reliability(rec: HealthRecord | None) -> float:
    """Laplace-smoothed success ratio, penalized by current consecutive failures."""
    if rec is None:
        return 0.9  # no history: slightly below a proven-healthy connection
    ratio = (rec.success_count + 1) / (rec.success_count + rec.failure_count + 2)
    return max(0.0, ratio - 0.1 * min(rec.consecutive_failures, 5))


def evaluate(records: list[HealthRecord], now: datetime) -> Availability:
    """Combine the connection-wide and model-specific records for one candidate."""
    rel = min((reliability(r) for r in records), default=reliability(None))
    needs_probe = False
    for r in records:
        if r.state == HealthState.OPEN.value:
            if r.open_until is not None and r.open_until > now:
                return Availability(False, reason="circuit_open", retry_at=r.open_until, reliability=rel)
            needs_probe = True
        elif r.state == HealthState.HALF_OPEN.value:
            if r.open_until is not None and r.open_until > now:
                return Availability(False, reason="probe_in_flight", retry_at=r.open_until, reliability=rel)
            needs_probe = True  # abandoned probe lease
    return Availability(True, needs_probe=needs_probe, reliability=rel)


async def load(session: AsyncSession, connection_ids: list[str]) -> dict[str, list[HealthRecord]]:
    if not connection_ids:
        return {}
    rows = (
        (await session.execute(select(HealthRecord).where(HealthRecord.connection_id.in_(connection_ids))))
        .scalars()
        .all()
    )
    out: dict[str, list[HealthRecord]] = {}
    for r in rows:
        out.setdefault(r.connection_id, []).append(r)
    return out


def records_for(
    by_conn: dict[str, list[HealthRecord]], connection_id: str, upstream_model: str
) -> list[HealthRecord]:
    return [r for r in by_conn.get(connection_id, []) if r.model_scope in (CONNECTION_SCOPE, upstream_model)]


async def claim_probe(
    db: Database, connection_id: str, upstream_model: str, now: datetime | None = None
) -> bool:
    """Atomically move every elapsed open/half-open record for this candidate to ``half_open``.

    Returns True only for the single caller that wins the conditional update(s).
    """
    now = now or utcnow()
    lease_until = now + timedelta(seconds=PROBE_LEASE_S)
    try:
        return await _claim(db, connection_id, upstream_model, now, lease_until)
    except _ProbeLost:
        return False


class _ProbeLost(Exception):
    pass


async def _claim(
    db: Database, connection_id: str, upstream_model: str, now: datetime, lease_until: datetime
) -> bool:
    async with db.transaction() as s:
        rows = (
            (
                await s.execute(
                    select(HealthRecord).where(
                        HealthRecord.connection_id == connection_id,
                        HealthRecord.model_scope.in_([CONNECTION_SCOPE, upstream_model]),
                        HealthRecord.state.in_([HealthState.OPEN.value, HealthState.HALF_OPEN.value]),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return True
        for r in rows:
            res = await s.execute(
                update(HealthRecord)
                .where(
                    HealthRecord.id == r.id,
                    HealthRecord.state.in_([HealthState.OPEN.value, HealthState.HALF_OPEN.value]),
                    or_(HealthRecord.open_until.is_(None), HealthRecord.open_until <= now),
                )
                .values(state=HealthState.HALF_OPEN.value, open_until=lease_until)
                .execution_options(synchronize_session=False)
            )
            if res.rowcount != 1:  # type: ignore[attr-defined]
                raise _ProbeLost  # rolls back any partial claim
    return True


async def _get_or_create(s: AsyncSession, connection_id: str, scope: str) -> HealthRecord:
    rec = (
        await s.execute(
            select(HealthRecord).where(
                and_(HealthRecord.connection_id == connection_id, HealthRecord.model_scope == scope)
            )
        )
    ).scalar_one_or_none()
    if rec is None:
        rec = HealthRecord(
            connection_id=connection_id,
            model_scope=scope,
            state=HealthState.HEALTHY.value,
            consecutive_failures=0,
            cooldown_level=0,
            success_count=0,
            failure_count=0,
        )
        s.add(rec)
        await s.flush()
    return rec


async def _with_record(db: Database, connection_id: str, scope: str, fn: object) -> HealthRecord:
    for _ in range(3):
        try:
            async with db.transaction() as s:
                rec = await _get_or_create(s, connection_id, scope)
                fn(rec)  # type: ignore[operator]
                return rec
        except IntegrityError:
            continue  # concurrent creation; retry against the winner's row
    raise RuntimeError("health record contention")


async def record_success(
    db: Database,
    connection_id: str,
    upstream_model: str,
    latency_ms: float | None,
    now: datetime | None = None,
) -> None:
    now = now or utcnow()

    def apply(rec: HealthRecord) -> None:
        rec.state = HealthState.HEALTHY.value
        rec.consecutive_failures = 0
        rec.cooldown_level = 0
        rec.open_until = None
        rec.success_count += 1
        rec.last_success_at = now
        if latency_ms is not None:
            rec.ewma_latency_ms = (
                latency_ms
                if rec.ewma_latency_ms is None
                else (1 - EWMA_ALPHA) * rec.ewma_latency_ms + EWMA_ALPHA * latency_ms
            )

    await _with_record(db, connection_id, CONNECTION_SCOPE, apply)
    # A success on a model proves the model-scope record healthy too, if one exists.
    async with db.transaction() as s:
        await s.execute(
            update(HealthRecord)
            .where(HealthRecord.connection_id == connection_id, HealthRecord.model_scope == upstream_model)
            .values(
                state=HealthState.HEALTHY.value, consecutive_failures=0, cooldown_level=0, open_until=None
            )
            .execution_options(synchronize_session=False)
        )


async def record_failure(
    db: Database,
    settings: Settings,
    connection_id: str,
    upstream_model: str,
    category: ErrorCategory,
    retry_after_s: float | None = None,
    now: datetime | None = None,
) -> HealthRecord | None:
    """Record a failed attempt. Returns the updated record, or None if the category doesn't affect health."""
    if category not in _COUNTED:
        return None
    now = now or utcnow()
    scope = scope_for(category, upstream_model)

    def apply(rec: HealthRecord) -> None:
        was_probe = rec.state == HealthState.HALF_OPEN.value
        rec.consecutive_failures += 1
        rec.failure_count += 1
        rec.last_failure_at = now
        rec.last_error_category = category.value
        if (
            was_probe
            or category in _OPEN_IMMEDIATELY
            or rec.consecutive_failures >= settings.breaker_failure_threshold
        ):
            rec.cooldown_level = min(rec.cooldown_level + 1, 20)
            rec.state = HealthState.OPEN.value
            rec.open_until = now + timedelta(
                seconds=cooldown_seconds(settings, rec.cooldown_level, retry_after_s)
            )
        else:
            rec.state = HealthState.DEGRADED.value

    return await _with_record(db, connection_id, scope, apply)


async def release_probe(db: Database, connection_id: str, upstream_model: str) -> None:
    """A probe that ended without a health verdict (e.g. client cancelled) returns the circuit to ``open``
    with an already-elapsed cooldown so another probe may be attempted."""
    async with db.transaction() as s:
        await s.execute(
            update(HealthRecord)
            .where(
                HealthRecord.connection_id == connection_id,
                HealthRecord.model_scope.in_([CONNECTION_SCOPE, upstream_model]),
                HealthRecord.state == HealthState.HALF_OPEN.value,
            )
            .values(state=HealthState.OPEN.value, open_until=utcnow())
            .execution_options(synchronize_session=False)
        )
