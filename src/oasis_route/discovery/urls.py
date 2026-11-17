"""Canonical URLs and bounded link extraction for the discovery frontier.

Canonicalisation makes one page one source: scheme and host are lower-cased, default ports, fragments,
tracking parameters and trailing slashes are dropped, and the remaining query is sorted. Link extraction
only *reads* ``href`` attributes from untrusted HTML; it never follows them itself. The discovery service
decides (by domain, keyword and depth budget) whether a link becomes a new source, and every fetch of that
source still goes through the safe fetcher (SSRF, robots, rate limits).
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

_TRACKING = re.compile(
    r"^(utm_[a-z]+|gclid|fbclid|msclkid|mc_[a-z]+|ref|ref_src|_hs[a-z]+|yclid|igshid)$", re.I
)
_DEFAULT_PORTS = {"https": 443, "http": 80}

# Paths worth following from an official page: access, limits, pricing, models, free tiers and terms.
FRONTIER_KEYWORDS = re.compile(
    r"(pricing|price|rate[-_]?limit|limits|quota|free|trial|credit|billing|tier|plans?|models?|"
    r"usage|terms|policy|policies|deprecat|changelog|release[-_]?notes|announc)",
    re.I,
)
_SKIP_EXT = re.compile(r"\.(png|jpe?g|gif|svg|webp|ico|css|js|mjs|woff2?|ttf|pdf|zip|gz|mp4|webm)$", re.I)


def canonicalize(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    netloc = host
    if parts.port and parts.port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)]
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query)), ""))


class _LinkParser(HTMLParser):
    def __init__(self, limit: int) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []
        self.limit = limit

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a" or len(self.hrefs) >= self.limit:
            return
        for k, v in attrs:
            if k == "href" and v:
                self.hrefs.append(v)


def extract_links(html_text: str, base_url: str, *, limit: int = 2000) -> list[str]:
    """Absolute, canonical https links found in ``<a href>`` (deduplicated, document order)."""
    parser = _LinkParser(limit)
    try:
        parser.feed(html_text[:2_000_000])
        parser.close()
    except Exception:  # noqa: S110 # pragma: no cover - HTMLParser is lenient; keep what it parsed
        pass
    out: list[str] = []
    seen: set[str] = set()
    for href in parser.hrefs:
        href = href.strip()
        if href.startswith(("#", "mailto:", "javascript:", "tel:", "data:")):
            continue
        absolute = urljoin(base_url, href)
        parts = urlsplit(absolute)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            continue
        if _SKIP_EXT.search(parts.path):
            continue
        c = canonicalize(absolute)
        if c not in seen and len(c) <= 2000:
            seen.add(c)
            out.append(c)
    return out


def is_frontier_candidate(url: str) -> bool:
    return bool(FRONTIER_KEYWORDS.search(urlsplit(url).path))
