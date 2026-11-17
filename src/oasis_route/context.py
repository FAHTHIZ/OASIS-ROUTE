"""Application context: long-lived shared resources wired once per process."""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from oasis_route.config import Settings
from oasis_route.db.session import Database
from oasis_route.security.rate_limit import TokenBucketLimiter
from oasis_route.telemetry import Metrics


@dataclass
class AppContext:
    settings: Settings
    db: Database
    metrics: Metrics = field(default_factory=Metrics)
    limiter: TokenBucketLimiter = field(default_factory=TokenBucketLimiter)
    # Upstream HTTP transport override (tests inject deterministic mock transports here).
    upstream_transport: httpx.AsyncBaseTransport | None = None
    discovery_transport: httpx.AsyncBaseTransport | None = None
