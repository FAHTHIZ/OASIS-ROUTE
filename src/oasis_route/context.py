"""Application context: long-lived shared resources wired once per process."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from oasis_route.adapters.registry import AdapterRegistry
from oasis_route.config import Settings
from oasis_route.db.session import Database
from oasis_route.security.rate_limit import TokenBucketLimiter
from oasis_route.security.ssrf import Resolver
from oasis_route.security.vault import LocalKeyProvider, Vault
from oasis_route.telemetry import Metrics


@dataclass
class AppContext:
    settings: Settings
    db: Database
    metrics: Metrics = field(default_factory=Metrics)
    limiter: TokenBucketLimiter = field(default_factory=TokenBucketLimiter)
    vault: Vault | None = None
    adapters: AdapterRegistry = field(default_factory=AdapterRegistry)
    # Upstream HTTP transport override (tests inject deterministic mock transports here).
    upstream_transport: httpx.AsyncBaseTransport | None = None
    discovery_transport: httpx.AsyncBaseTransport | None = None
    # DNS resolver used by SSRF checks (tests inject a fake resolver).
    resolver: Resolver | None = None
    # Process-local helpers (e.g. the discovery fetcher with robots cache and per-domain budgets).
    extras: dict[str, Any] = field(default_factory=dict, repr=False)
    _upstream_client: httpx.AsyncClient | None = field(default=None, repr=False)
    _discovery_client: httpx.AsyncClient | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.vault is None:
            self.vault = Vault(
                LocalKeyProvider(self.settings.kek_bytes(), self.settings.previous_kek_bytes())
            )

    @property
    def vault_(self) -> Vault:
        assert self.vault is not None
        return self.vault

    @property
    def upstream_client(self) -> httpx.AsyncClient:
        """Shared upstream client. Redirects are never followed (an SSRF and credential-leak vector)."""
        if self._upstream_client is None:
            self._upstream_client = httpx.AsyncClient(
                transport=self.upstream_transport,
                follow_redirects=False,
                timeout=httpx.Timeout(
                    self.settings.upstream_read_timeout_s, connect=self.settings.upstream_connect_timeout_s
                ),
                limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
                headers={"user-agent": "OasisRoute/0.1"},
            )
        return self._upstream_client

    @property
    def discovery_client(self) -> httpx.AsyncClient:
        if self._discovery_client is None:
            self._discovery_client = httpx.AsyncClient(
                transport=self.discovery_transport,
                follow_redirects=False,
                timeout=httpx.Timeout(self.settings.discovery_timeout_s),
                headers={"user-agent": self.settings.discovery_user_agent},
            )
        return self._discovery_client

    async def aclose(self) -> None:
        for c in (self._upstream_client, self._discovery_client):
            if c is not None:
                await c.aclose()
        self._upstream_client = self._discovery_client = None
        await self.db.dispose()
