"""Per-request receipts: estimate, reservation, actual usage and cost, outcome, tamper-evident digest."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from oasis_route.db.models import RequestReceipt

FIELDS = (
    "request_id",
    "project_id",
    "api_key_id",
    "outcome",
    "spend_mode",
    "provider_id",
    "upstream_model",
    "connection_id",
    "cost_basis",
    "cost_status",
    "price_verified",
    "estimated_usd_micros",
    "reserved_usd_micros",
    "actual_usd_micros",
    "drift_usd_micros",
    "input_tokens",
    "output_tokens",
    "usage_source",
    "streamed",
    "attempts",
    "fallback_used",
    "approval_id",
    "reservation_ids",
    "budgets_after",
)


def digest(body: dict[str, Any]) -> str:
    canon = json.dumps({k: body.get(k) for k in FIELDS}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode()).hexdigest()


def build(**body: Any) -> RequestReceipt:
    return RequestReceipt(**body, digest=digest(body))


def _usd(m: int | None) -> float | None:
    return None if m is None else m / 1_000_000


def out(r: RequestReceipt) -> dict[str, Any]:
    body = {k: getattr(r, k) for k in FIELDS}
    return {
        **body,
        "estimated_usd": _usd(r.estimated_usd_micros),
        "reserved_usd": _usd(r.reserved_usd_micros),
        "actual_usd": _usd(r.actual_usd_micros),
        "drift_usd": _usd(r.drift_usd_micros),
        "digest": r.digest,
        "digest_valid": digest(body) == r.digest,
        "created_at": r.created_at,
    }
