"""Stable domain enumerations shared across modules."""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"  # instance operator: registry review, global policy
    MEMBER = "member"  # project member


class ProjectRole(StrEnum):
    OWNER = "owner"
    DEVELOPER = "developer"
    VIEWER = "viewer"


class ApiKeyScope(StrEnum):
    INFERENCE = "inference"  # call /v1 gateway
    READ = "read"  # read project config/usage
    MANAGE = "manage"  # manage project keys/connections/pools


class ConnectionStatus(StrEnum):
    PENDING_VALIDATION = "pending_validation"
    ACTIVE = "active"
    SUSPENDED = "suspended"  # auth invalid/expired or operator action — needs user action
    REVOKED = "revoked"


class AuthMethod(StrEnum):
    API_KEY = "api_key"
    OAUTH = "oauth"
    NONE = "none"  # explicitly permitted public/anonymous access only


class OfferState(StrEnum):
    DISCOVERED = "discovered"
    NEEDS_REVIEW = "needs_review"
    VERIFIED_ELIGIBLE = "verified_eligible"
    CONNECTABLE = "connectable"
    ACTIVE = "active"
    REJECTED = "rejected"
    INELIGIBLE = "ineligible"
    EXPIRED = "expired"
    SUSPENDED = "suspended"
    STALE = "stale"
    REVOKED = "revoked"


class QuotaUnit(StrEnum):
    REQUESTS = "requests"
    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"
    TOTAL_TOKENS = "total_tokens"
    CONCURRENT = "concurrent_requests"
    CREDITS_USD_MICROS = "credits_usd_micros"


class QuotaPeriod(StrEnum):
    MINUTE = "minute"
    HOUR = "hour"
    DAY = "day"
    MONTH = "month"
    ONE_TIME = "one_time"  # credit balance; never recurring
    NONE = "none"  # instantaneous (concurrency)


class QuotaSource(StrEnum):
    PROVIDER_API = "provider_api"
    RESPONSE_HEADERS = "response_headers"
    USER_DECLARED = "user_declared"
    OFFER_EVIDENCE = "offer_evidence"
    ESTIMATE = "estimate"


class ReservationStatus(StrEnum):
    RESERVED = "reserved"
    COMMITTED = "committed"
    RELEASED = "released"
    EXPIRED = "expired"


class ErrorCategory(StrEnum):
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_INVALID = "AUTH_INVALID"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    REQUEST_UNSUPPORTED = "REQUEST_UNSUPPORTED"
    POLICY_REJECTED = "POLICY_REJECTED"
    UNKNOWN_UPSTREAM = "UNKNOWN_UPSTREAM"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"


class RoutingMode(StrEnum):
    STRICT_QUALITY = "strict_quality"
    BALANCED = "balanced"


class HealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OPEN = "open"  # circuit open: in cooldown
    HALF_OPEN = "half_open"  # one probe permitted


class SourceClass(StrEnum):
    OFFICIAL_DOCS = "official_docs"
    OFFICIAL_ANNOUNCEMENT = "official_announcement"
    OFFICIAL_REPOSITORY = "official_repository"
    MODEL_CATALOGUE = "model_catalogue"
    CURATED_DIRECTORY = "curated_directory"
    USER_SUBMITTED = "user_submitted"


class SourceReviewStatus(StrEnum):
    PENDING = "pending"  # terms/robots not reviewed — fetching disabled
    APPROVED = "approved"
    BLOCKED = "blocked"


class Task(StrEnum):
    GENERAL = "general"
    CODING = "coding"
    REASONING = "reasoning"
    CREATIVE = "creative"
    EXTRACTION = "extraction"
    VISION = "vision"
