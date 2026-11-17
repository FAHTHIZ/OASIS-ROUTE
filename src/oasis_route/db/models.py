"""SQLAlchemy ORM models — the authoritative schema.

Conventions: string UUID primary keys, timezone-aware UTC timestamps, JSON for structured metadata.
Secrets are never stored in plaintext columns; see :mod:`oasis_route.security.vault`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware datetime that round-trips as UTC on every backend (SQLite drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {datetime: UTCDateTime, dict[str, Any]: JSON, list[Any]: JSON}


def _id() -> Mapped[str]:
    return mapped_column(String(36), primary_key=True, default=new_id)


def _created() -> Mapped[datetime]:
    return mapped_column(UTCDateTime, default=utcnow, nullable=False)


# --------------------------------------------------------------------------------------------- identity
class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = _id()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(20), default="member")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Self-declared eligibility attributes used for provider audience/region checks.
    country: Mapped[str | None] = mapped_column(String(2))
    declared_age: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created()


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = _id()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    routing_mode: Mapped[str] = mapped_column(String(30), default="strict_quality")
    # {require_no_training, require_zero_retention, allowed_regions, provider_allowlist, provider_denylist}
    data_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    max_fallback_hops: Mapped[int | None] = mapped_column(Integer)
    allow_downgrade: Mapped[bool] = mapped_column(Boolean, default=True)
    # Unknown quota is never treated as unlimited. When False (default), pools with unknown limits are not
    # reservable; when True, they are reservable but reported as "unknown" capacity.
    allow_unknown_quota: Mapped[bool] = mapped_column(Boolean, default=False)
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)
    rate_limit_rpm: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created()


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (UniqueConstraint("project_id", "user_id"),)
    id: Mapped[str] = _id()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="developer")


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = _id()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), default="")
    prefix: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    scopes: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = _created()
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    rotated_from_id: Mapped[str | None] = mapped_column(String(36))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_project_ts", "project_id", "created_at"),)
    id: Mapped[str] = _id()
    created_at: Mapped[datetime] = _created()
    actor_user_id: Mapped[str | None] = mapped_column(String(36))
    actor_key_id: Mapped[str | None] = mapped_column(String(36))
    project_id: Mapped[str | None] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(50))
    target_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


# --------------------------------------------------------------------------------------------- registry
class Provider(Base):
    __tablename__ = "providers"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # slug, e.g. "openai"
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    adapter_key: Mapped[str] = mapped_column(String(64), nullable=False)
    official_domains: Mapped[list[Any]] = mapped_column(JSON, default=list)
    terms_url: Mapped[str | None] = mapped_column(String(1000))
    policy_url: Mapped[str | None] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(20), default="active")
    # Data handling: {trains_on_api_data: bool|None, retention_days: int|None, zero_retention_available: bool}
    data_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    regions: Mapped[list[Any]] = mapped_column(JSON, default=list)  # processing regions, e.g. ["us","eu"]
    min_age: Mapped[int | None] = mapped_column(Integer)
    allowed_countries: Mapped[list[Any]] = mapped_column(JSON, default=list)  # empty == not restricted
    blocked_countries: Mapped[list[Any]] = mapped_column(JSON, default=list)
    multi_account_permitted: Mapped[bool] = mapped_column(Boolean, default=False)
    automated_access_permitted: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _created()


class Endpoint(Base):
    __tablename__ = "endpoints"
    id: Mapped[str] = _id()
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"), nullable=False)
    base_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    protocol: Mapped[str] = mapped_column(String(40), nullable=False)  # openai_chat | anthropic_messages
    region: Mapped[str | None] = mapped_column(String(20))
    is_default: Mapped[bool] = mapped_column(Boolean, default=True)


class Model(Base):
    __tablename__ = "models"
    __table_args__ = (UniqueConstraint("provider_id", "upstream_id"),)
    id: Mapped[str] = _id()
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"), nullable=False)
    upstream_id: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), default="")
    family: Mapped[str | None] = mapped_column(String(100))
    aliases: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # {modalities_in:[text,image], tools:bool, json_mode:bool, streaming:bool}
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    context_window: Mapped[int | None] = mapped_column(Integer)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer)
    lifecycle: Mapped[str] = mapped_column(String(20), default="active")  # active|deprecated|removed
    # {task: {score: 0-100, source, date, methodology, confidence}}
    quality: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _created()


class Offer(Base):
    __tablename__ = "offers"
    id: Mapped[str] = _id()
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    access_mode: Mapped[str] = mapped_column(String(40), default="free_tier")
    state: Mapped[str] = mapped_column(String(30), default="discovered")
    dedupe_key: Mapped[str] = mapped_column(String(300), unique=True, nullable=False)
    # [{unit, period, limit, model_scope, text}]
    quota_claims: Mapped[list[Any]] = mapped_column(JSON, default=list)
    credit_type: Mapped[str] = mapped_column(String(20), default="recurring")  # recurring | one_time
    # {min_age, countries_allowed, countries_blocked, commercial_use, transferable, poolable,
    #  automated_access, requires_account_cycling}
    eligibility: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    eligibility_reasons: Mapped[list[Any]] = mapped_column(JSON, default=list)
    terms_url: Mapped[str | None] = mapped_column(String(1000))
    onboarding_url: Mapped[str | None] = mapped_column(String(1000))
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    next_review_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class OfferTransition(Base):
    __tablename__ = "offer_transitions"
    id: Mapped[str] = _id()
    offer_id: Mapped[str] = mapped_column(ForeignKey("offers.id", ondelete="CASCADE"), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(30))
    to_state: Mapped[str] = mapped_column(String(30), nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)  # user id or "system:<component>"
    rationale: Mapped[str] = mapped_column(Text, default="")
    evidence_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = _created()


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[str] = _id()
    url: Mapped[str] = mapped_column(String(2000), unique=True, nullable=False)
    domain: Mapped[str] = mapped_column(String(300), nullable=False)
    provider_id: Mapped[str | None] = mapped_column(ForeignKey("providers.id"))
    source_class: Mapped[str] = mapped_column(String(40), nullable=False)
    review_status: Mapped[str] = mapped_column(String(20), default="pending")
    terms_notes: Mapped[str] = mapped_column(Text, default="")
    allowed_methods: Mapped[list[Any]] = mapped_column(JSON, default=lambda: ["GET"])
    url_scope: Mapped[str | None] = mapped_column(String(2000))  # path prefix the source may fetch
    crawl_interval_s: Mapped[int] = mapped_column(Integer, default=86_400)
    min_interval_per_domain_s: Mapped[float] = mapped_column(Float, default=10.0)
    retention_days: Mapped[int] = mapped_column(Integer, default=180)
    etag: Mapped[str | None] = mapped_column(String(500))
    last_modified: Mapped[str | None] = mapped_column(String(100))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    last_fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_status: Mapped[str | None] = mapped_column(String(100))
    next_fetch_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    submitted_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = _created()


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[str] = _id()
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"))
    offer_id: Mapped[str | None] = mapped_column(ForeignKey("offers.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    publisher: Mapped[str | None] = mapped_column(String(300))
    retrieved_at: Mapped[datetime] = _created()
    http_status: Mapped[int | None] = mapped_column(Integer)
    http_meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    excerpt: Mapped[str] = mapped_column(Text, default="")
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_type: Mapped[str] = mapped_column(String(40), nullable=False)
    claim_value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    method: Mapped[str] = mapped_column(String(40), default="automated_extraction")
    reviewer_id: Mapped[str | None] = mapped_column(String(36))
    confidence: Mapped[float] = mapped_column(Float, default=0.3)
    next_review_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ChangeEvent(Base):
    __tablename__ = "change_events"
    id: Mapped[str] = _id()
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    offer_id: Mapped[str | None] = mapped_column(ForeignKey("offers.id", ondelete="CASCADE"))
    old_hash: Mapped[str | None] = mapped_column(String(64))
    new_hash: Mapped[str | None] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(20), default="info")
    summary: Mapped[str] = mapped_column(Text, default="")
    action_required: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _created()


# --------------------------------------------------------------------------------------------- connections
class Credential(Base):
    __tablename__ = "credentials"
    id: Mapped[str] = _id()
    kek_id: Mapped[str] = mapped_column(String(64), nullable=False)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    aad: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = _created()
    rotated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Connection(Base):
    __tablename__ = "connections"
    id: Mapped[str] = _id()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"), nullable=False)
    endpoint_id: Mapped[str | None] = mapped_column(ForeignKey("endpoints.id"))
    offer_id: Mapped[str | None] = mapped_column(ForeignKey("offers.id"))
    display_name: Mapped[str] = mapped_column(String(200), default="")
    # Non-secret label for the upstream account/project. Connections with the same label share quota pools.
    upstream_account_ref: Mapped[str] = mapped_column(String(200), default="default")
    auth_method: Mapped[str] = mapped_column(String(20), default="api_key")
    status: Mapped[str] = mapped_column(String(30), default="pending_validation")
    credential_id: Mapped[str | None] = mapped_column(ForeignKey("credentials.id"))
    priority: Mapped[int] = mapped_column(Integer, default=0)  # user preference boost
    eligibility_ack: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    allowed_models: Mapped[list[Any]] = mapped_column(JSON, default=list)  # empty == all known models
    last_validated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = _created()

    credential: Mapped[Credential | None] = relationship(lazy="joined")


# --------------------------------------------------------------------------------------------- quota ledger
class QuotaPool(Base):
    """One accounting dimension (unit + period) of an upstream allocation.

    Pools are shared by every connection in the project with the same provider and ``account_scope``.
    ``limit_value`` NULL means *unknown*, which is never treated as unlimited: unknown pools are reservable
    only when the project opts into ``allow_unknown`` and capacity reports label them as unknown.
    """

    __tablename__ = "quota_pools"
    __table_args__ = (
        UniqueConstraint("project_id", "provider_id", "account_scope", "model_scope", "unit", "period"),
    )
    id: Mapped[str] = _id()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"), nullable=False)
    account_scope: Mapped[str] = mapped_column(String(200), default="default")
    model_scope: Mapped[str] = mapped_column(String(200), default="*")  # upstream model id or "*"
    offer_id: Mapped[str | None] = mapped_column(ForeignKey("offers.id"))
    unit: Mapped[str] = mapped_column(String(30), nullable=False)
    period: Mapped[str] = mapped_column(String(20), nullable=False)
    reset_tz: Mapped[str] = mapped_column(String(64), default="UTC")
    rolling: Mapped[bool] = mapped_column(Boolean, default=False)
    limit_value: Mapped[int | None] = mapped_column(BigInteger)
    used: Mapped[int] = mapped_column(BigInteger, default=0)
    reserved: Mapped[int] = mapped_column(BigInteger, default=0)
    window_start: Mapped[datetime | None] = mapped_column(UTCDateTime)
    observed_remaining: Mapped[int | None] = mapped_column(BigInteger)
    observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    exhausted_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    needs_recheck: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    source: Mapped[str] = mapped_column(String(30), default="user_declared")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    last_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    drift: Mapped[int] = mapped_column(BigInteger, default=0)  # cumulative (actual - estimate)
    estimated_used: Mapped[int] = mapped_column(BigInteger, default=0)  # portion of `used` that is estimated
    created_at: Mapped[datetime] = _created()


class Reservation(Base):
    __tablename__ = "reservations"
    id: Mapped[str] = _id()
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    connection_id: Mapped[str] = mapped_column(String(36), nullable=False)
    model_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="reserved")
    estimate: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    actual: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    usage_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    items: Mapped[list[ReservationItem]] = relationship(
        back_populates="reservation", lazy="selectin", cascade="all, delete-orphan"
    )


class ReservationItem(Base):
    __tablename__ = "reservation_items"
    id: Mapped[str] = _id()
    reservation_id: Mapped[str] = mapped_column(
        ForeignKey("reservations.id", ondelete="CASCADE"), nullable=False
    )
    pool_id: Mapped[str] = mapped_column(ForeignKey("quota_pools.id", ondelete="CASCADE"), nullable=False)
    unit: Mapped[str] = mapped_column(String(30), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actual: Mapped[int | None] = mapped_column(BigInteger)

    reservation: Mapped[Reservation] = relationship(back_populates="items")


class UsageRecord(Base):
    __tablename__ = "usage_records"
    __table_args__ = (Index("ix_usage_project_ts", "project_id", "created_at"),)
    id: Mapped[str] = _id()
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    api_key_id: Mapped[str | None] = mapped_column(String(36))
    connection_id: Mapped[str | None] = mapped_column(String(36))
    provider_id: Mapped[str | None] = mapped_column(String(64))
    model_id: Mapped[str | None] = mapped_column(String(36))
    upstream_model: Mapped[str | None] = mapped_column(String(200))
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    provider_reported: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome: Mapped[str] = mapped_column(String(30), default="success")
    error_category: Mapped[str | None] = mapped_column(String(40))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    streamed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _created()


class RouteDecision(Base):
    __tablename__ = "route_decisions"
    __table_args__ = (Index("ix_route_project_ts", "project_id", "created_at"),)
    id: Mapped[str] = _id()
    request_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    requested_model: Mapped[str] = mapped_column(String(200), nullable=False)
    task: Mapped[str] = mapped_column(String(30), default="general")
    mode: Mapped[str] = mapped_column(String(30), nullable=False)
    candidates: Mapped[list[Any]] = mapped_column(JSON, default=list)
    rejected: Mapped[list[Any]] = mapped_column(JSON, default=list)
    attempts: Mapped[list[Any]] = mapped_column(JSON, default=list)
    served_model: Mapped[str | None] = mapped_column(String(200))
    served_provider: Mapped[str | None] = mapped_column(String(64))
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    downgraded: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome: Mapped[str] = mapped_column(String(30), default="pending")
    created_at: Mapped[datetime] = _created()


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("project_id", "key"),)
    id: Mapped[str] = _id()
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = _created()


# --------------------------------------------------------------------------------------------- health
class HealthRecord(Base):
    __tablename__ = "health_records"
    __table_args__ = (UniqueConstraint("connection_id", "model_scope"),)
    id: Mapped[str] = _id()
    connection_id: Mapped[str] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), nullable=False
    )
    model_scope: Mapped[str] = mapped_column(String(200), default="*")
    state: Mapped[str] = mapped_column(String(20), default="healthy")
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_level: Mapped[int] = mapped_column(Integer, default=0)
    open_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error_category: Mapped[str | None] = mapped_column(String(40))
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_failure_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    success_count: Mapped[int] = mapped_column(BigInteger, default=0)
    failure_count: Mapped[int] = mapped_column(BigInteger, default=0)
    ewma_latency_ms: Mapped[float | None] = mapped_column(Float)


# --------------------------------------------------------------------------------------------- notifications
class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = _id()
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), default="info")
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="")
    channel: Mapped[str] = mapped_column(String(30), default="in_app")
    facts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _created()
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    delete_after: Mapped[datetime | None] = mapped_column(UTCDateTime)


class MailGrant(Base):
    """Optional Gmail OAuth grant. Disabled unless the compliance gate is satisfied."""

    __tablename__ = "mail_grants"
    id: Mapped[str] = _id()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    credential_id: Mapped[str | None] = mapped_column(ForeignKey("credentials.id"))
    scopes: Mapped[list[Any]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    consent_version: Mapped[str] = mapped_column(String(20), nullable=False)
    consented_at: Mapped[datetime] = _created()
    last_sync_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


# --------------------------------------------------------------------------------------------- worker
class WorkerLease(Base):
    __tablename__ = "worker_leases"
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    holder: Mapped[str] = mapped_column(String(100), nullable=False)
    lease_until: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_result: Mapped[str | None] = mapped_column(Text)
