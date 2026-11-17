"""Robots Exclusion Protocol (RFC 9309) parsing and matching.

- Groups are selected by the most specific matching user-agent product token, falling back to ``*``.
- The longest matching rule wins; on a tie ``allow`` wins (RFC 9309 section 2.2.2).
- ``*`` matches any sequence and a trailing ``$`` anchors the end of the path.
- Fetch outcome rules (section 2.3.1): 2xx parse; 4xx (unavailable) means no restrictions;
  5xx or network failure means complete disallow ("unreachable").
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit


@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str]] = field(default_factory=list)  # (allow, pattern)
    crawl_delay: float | None = None


@dataclass
class RobotsPolicy:
    groups: list[_Group] = field(default_factory=list)
    allow_all: bool = False
    disallow_all: bool = False

    @classmethod
    def unavailable(cls) -> RobotsPolicy:
        return cls(allow_all=True)

    @classmethod
    def unreachable(cls) -> RobotsPolicy:
        return cls(disallow_all=True)

    def _group_for(self, user_agent: str) -> _Group | None:
        token = user_agent.split("/")[0].strip().lower()
        best: _Group | None = None
        best_len = -1
        star: _Group | None = None
        for g in self.groups:
            for a in g.agents:
                if a == "*":
                    star = star or g
                elif token.startswith(a) and len(a) > best_len:
                    best, best_len = g, len(a)
        return best or star

    def crawl_delay(self, user_agent: str) -> float | None:
        g = self._group_for(user_agent)
        return g.crawl_delay if g else None

    def allowed(self, url: str, user_agent: str) -> bool:
        if self.disallow_all:
            return False
        if self.allow_all:
            return True
        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        if path == "/robots.txt":
            return True
        g = self._group_for(user_agent)
        if g is None:
            return True
        best: tuple[int, bool] | None = None
        for allow, pattern in g.rules:
            if not pattern:
                continue  # empty Disallow means "allow everything"
            if _match(pattern, path):
                key = (len(pattern), allow)
                if best is None or key > best:
                    best = key
        return True if best is None else best[1]


def _match(pattern: str, path: str) -> bool:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in _normalise(body))
    return re.match(regex + ("$" if anchored else ""), _normalise(path)) is not None


def _normalise(s: str) -> str:
    return unquote(s)


def parse(text: str, *, max_bytes: int = 500_000) -> RobotsPolicy:
    """Parse robots.txt; content beyond ``max_bytes`` is ignored (RFC 9309 requires >= 500 KiB)."""
    text = text[:max_bytes]
    groups: list[_Group] = []
    current: _Group | None = None
    last_was_agent = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = (x.strip() for x in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if current is None or not last_was_agent:
                current = _Group()
                groups.append(current)
            current.agents.append(value.lower())
            last_was_agent = True
            continue
        last_was_agent = False
        if current is None:
            continue
        if key in ("allow", "disallow"):
            current.rules.append((key == "allow", value))
        elif key == "crawl-delay":
            with contextlib.suppress(ValueError):
                current.crawl_delay = max(0.0, float(value))
    return RobotsPolicy(groups=groups)
