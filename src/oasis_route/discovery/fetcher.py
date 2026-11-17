"""Safe, polite fetcher for discovery sources.

Every fetch: SSRF-validates the URL (and every redirect hop), honours robots.txt, enforces a per-domain
minimum interval (max of the source budget and any robots ``Crawl-delay``), sends conditional headers,
caps response size and accepted content types, and never sends cookies or credentials. It does not
attempt to bypass logins, CAPTCHAs, paywalls, or blocks: 401/403/429 are recorded and backed off.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx

from oasis_route.context import AppContext
from oasis_route.discovery import robots
from oasis_route.security.ssrf import UnsafeUrlError, assert_safe_url

ALLOWED_CONTENT_TYPES = (
    "text/html",
    "text/plain",
    "text/markdown",
    "application/json",
    "application/xhtml+xml",
    "application/xml",
    "text/xml",
)


class FetchError(Exception):
    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass
class FetchResult:
    url: str
    status: int
    text: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    not_modified: bool = False


@dataclass
class _DomainState:
    robots: robots.RobotsPolicy | None = None
    robots_expires: float = 0.0
    next_allowed: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Fetcher:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._domains: dict[str, _DomainState] = {}
        self.sleep = asyncio.sleep  # injectable for tests
        self.clock = time.monotonic

    def _state(self, origin: str) -> _DomainState:
        return self._domains.setdefault(origin, _DomainState())

    async def _check(self, url: str) -> None:
        try:
            await assert_safe_url(url, allow_private=False, resolver=self.ctx.resolver)
        except UnsafeUrlError as exc:
            raise FetchError(f"unsafe_url: {exc}") from exc

    async def _raw_get(self, url: str, headers: dict[str, str]) -> httpx.Response:
        """One GET without redirects; body read up to the configured cap."""
        await self._check(url)
        cap = self.ctx.settings.discovery_max_bytes
        client = self.ctx.discovery_client
        try:
            async with client.stream("GET", url, headers=headers) as resp:
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > cap:
                    raise FetchError("response_too_large", resp.status_code)
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > cap:
                        raise FetchError("response_too_large", resp.status_code)
                return httpx.Response(
                    resp.status_code, headers=resp.headers, content=bytes(buf), request=resp.request
                )
        except httpx.HTTPError as exc:
            raise FetchError(f"network_error: {type(exc).__name__}") from exc

    async def robots_for(self, origin: str) -> robots.RobotsPolicy:
        st = self._state(origin)
        now = self.clock()
        if st.robots is not None and st.robots_expires > now:
            return st.robots
        policy: robots.RobotsPolicy
        try:
            resp = await self._follow(urljoin(origin, "/robots.txt"), {}, origin=origin, scope_host=None)
            if 200 <= resp.status_code < 300:
                policy = robots.parse(resp.text)
            elif 400 <= resp.status_code < 500:
                policy = robots.RobotsPolicy.unavailable()
            else:
                policy = robots.RobotsPolicy.unreachable()
        except FetchError:
            policy = robots.RobotsPolicy.unreachable()
        st.robots = policy
        # Unreachable robots is re-tried sooner than a parsed one.
        ttl = self.ctx.settings.discovery_robots_ttl_s if not policy.disallow_all else 3600
        st.robots_expires = now + ttl
        return policy

    async def _follow(
        self, url: str, headers: dict[str, str], *, origin: str, scope_host: str | None
    ) -> httpx.Response:
        for _ in range(self.ctx.settings.discovery_max_redirects + 1):
            resp = await self._raw_get(url, headers)
            if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                nxt = urljoin(url, resp.headers["location"])
                host = (urlsplit(nxt).hostname or "").lower()
                base = scope_host or (urlsplit(origin).hostname or "").lower()
                if not (host == base or host.endswith("." + base)):
                    raise FetchError("redirect_off_scope", resp.status_code)
                url = nxt
                continue
            return resp
        raise FetchError("too_many_redirects")

    async def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        min_interval_s: float = 10.0,
        scope_host: str | None = None,
    ) -> FetchResult:
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise FetchError("unsafe_url: discovery fetches require https")
        await self._check(url)  # fail fast and precisely before touching robots.txt
        origin = f"{parts.scheme}://{parts.netloc}"
        ua = self.ctx.settings.discovery_user_agent
        st = self._state(origin)
        policy = await self.robots_for(origin)
        if not policy.allowed(url, ua):
            raise FetchError("disallowed_by_robots")
        interval = max(min_interval_s, policy.crawl_delay(ua) or 0.0)
        async with st.lock:  # one in-flight request per origin
            wait = st.next_allowed - self.clock()
            if wait > 0:
                await self.sleep(wait)
            headers = {"accept": ", ".join(ALLOWED_CONTENT_TYPES)}
            if etag:
                headers["if-none-match"] = etag
            if last_modified:
                headers["if-modified-since"] = last_modified
            try:
                resp = await self._follow(
                    url, headers, origin=origin, scope_host=scope_host or parts.hostname
                )
            finally:
                st.next_allowed = self.clock() + interval
        meta = {
            k: resp.headers[k]
            for k in ("etag", "last-modified", "content-type", "date", "retry-after")
            if k in resp.headers
        }
        if resp.status_code == 304:
            return FetchResult(url, 304, headers=meta, not_modified=True)
        if resp.status_code >= 400:
            raise FetchError(f"http_{resp.status_code}", resp.status_code)
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype and ctype not in ALLOWED_CONTENT_TYPES:
            raise FetchError(f"unsupported_content_type: {ctype}", resp.status_code)
        return FetchResult(url, resp.status_code, text=resp.text, headers=meta)


def fetcher_for(ctx: AppContext) -> Fetcher:
    """Process-wide fetcher per context so robots caches and domain budgets are shared."""
    existing = ctx.extras.get("discovery_fetcher")
    if isinstance(existing, Fetcher):
        return existing
    f = Fetcher(ctx)
    ctx.extras["discovery_fetcher"] = f
    return f
