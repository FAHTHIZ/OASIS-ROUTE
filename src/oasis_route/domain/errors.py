"""Error envelope and application exceptions.

Admin API errors use ``{"error": {"code", "message", "request_id", "details"}}``.
Gateway (/v1) errors use the OpenAI-compatible ``{"error": {"message", "type", "code", "param"}}`` shape with
an additional ``oasis_route`` object describing routing attempts.
"""

from __future__ import annotations

from typing import Any

from oasis_route.domain.enums import ErrorCategory


class AppError(Exception):
    status_code: int = 400
    code: str = "bad_request"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"


class PolicyViolation(AppError):
    """Raised when an action would violate provider eligibility or Oasis Route hard boundaries."""

    status_code = 403
    code = "policy_violation"


class UpstreamError(Exception):
    """Normalized provider error produced by adapters."""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        status: int | None = None,
        retry_after_s: float | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.status = status
        self.retry_after_s = retry_after_s
        self.retryable = retryable if retryable is not None else category in _RETRYABLE_ELSEWHERE
        # Provider quota observations attached to the failed response (headers), if any.
        self.observations: list[Any] = []

    def __repr__(self) -> str:
        return f"UpstreamError({self.category}, status={self.status})"


# Categories after which trying a *different* candidate is acceptable.
_RETRYABLE_ELSEWHERE = {
    ErrorCategory.QUOTA_EXHAUSTED,
    ErrorCategory.RATE_LIMITED,
    ErrorCategory.PROVIDER_UNAVAILABLE,
    ErrorCategory.MODEL_UNAVAILABLE,
    ErrorCategory.REQUEST_UNSUPPORTED,
    ErrorCategory.TIMEOUT,
    ErrorCategory.UNKNOWN_UPSTREAM,
    ErrorCategory.AUTH_INVALID,
    ErrorCategory.AUTH_EXPIRED,
}

# Categories that must stop the whole request (never evade a provider decision).
STOP_CATEGORIES = {ErrorCategory.POLICY_REJECTED, ErrorCategory.CANCELLED}
