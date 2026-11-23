"""Structured extractors: HTML rate-limit tables and public JSON model catalogues.

These complement the regular-expression claim extractor in :mod:`oasis_route.discovery.extraction`. They
parse *data* (table cells, JSON fields) into the same :class:`~oasis_route.discovery.extraction.Claim`
shape with an exact excerpt, so every structured fact is still evidence-backed and reviewable. Nothing here
executes, follows, or trusts page content: tables are read with the standard library HTML parser, JSON
with :func:`json.loads`, and every value is type-checked and bounded before it becomes a claim.

Supported formats (detected from the URL and content, never from instructions inside the page):

- ``html_tables``: any HTML page with a ``<table>`` whose header names a model column and one or more
  rate-limit columns (RPM, RPD, RPH, TPM, TPH, TPD, ITPM, OTPM, or the spelled-out forms). The tier a table
  describes (free / paid) is inferred conservatively from nearby headings and text; ambiguous tables are
  labelled ``unverified`` rather than guessed.
- ``openrouter_models``: ``https://openrouter.ai/api/v1/models``. Free variants (``:free`` ids or zero
  prompt/completion/request prices) become ``model`` claims marked ``free``.
- ``hf_router_models``: ``https://router.huggingface.co/v1/models``. Each inference provider behind the
  router becomes a ``provider_listing`` claim (a lead for a new provider candidate, never an active offer).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from oasis_route.discovery.extraction import Claim

EXTRACTOR_VERSION = "2026.11.1"
_MAX_TABLES = 40
_MAX_ROWS = 400
_MAX_CELL = 300
_MAX_MODELS = 600
_CONTEXT_CHARS = 600


@dataclass
class Table:
    headers: list[str]
    rows: list[list[str]]
    context: str  # nearest preceding heading + text, used only for tier classification
    heading: str = ""


@dataclass
class _TableBuild:
    rows: list[list[str]] = field(default_factory=list)
    row: list[str] | None = None
    cell: list[str] | None = None
    header_row: bool = False
    header_rows: list[int] = field(default_factory=list)


class _TableParser(HTMLParser):
    _SKIP = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[Table] = []
        self._stack: list[_TableBuild] = []
        self._text: list[str] = []  # running document text outside tables (bounded)
        self._heading: str = ""
        self._in_heading = False
        self._heading_buf: list[str] = []
        self._skip = 0

    def _push_text(self, s: str) -> None:
        self._text.append(s)
        if sum(len(x) for x in self._text) > _CONTEXT_CHARS * 4:
            joined = "".join(self._text)[-_CONTEXT_CHARS * 2 :]
            self._text = [joined]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip += 1
            return
        if tag in ("h1", "h2", "h3", "h4") and not self._stack:
            self._in_heading, self._heading_buf = True, []
            self._text = []  # a new section starts: earlier text no longer describes what follows
        if tag == "table":
            if len(self.tables) + len(self._stack) < _MAX_TABLES:
                self._stack.append(_TableBuild())
            return
        if not self._stack:
            if tag in ("br", "p", "div", "li", "button", "span"):
                self._push_text(" ")
            return
        t = self._stack[-1]
        if tag == "tr":
            t.row, t.header_row = [], True
        elif tag in ("td", "th") and t.row is not None:
            t.cell = []
            if tag == "td":
                t.header_row = False

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if tag in ("h1", "h2", "h3", "h4") and self._in_heading:
            self._in_heading = False
            self._heading = " ".join("".join(self._heading_buf).split())[:200]
            self._push_text(" " + self._heading + " ")
            return
        if not self._stack:
            return
        t = self._stack[-1]
        if tag in ("td", "th") and t.cell is not None and t.row is not None:
            t.row.append(" ".join("".join(t.cell).split())[:_MAX_CELL])
            t.cell = None
        elif tag == "tr" and t.row is not None:
            if t.row and len(t.rows) < _MAX_ROWS:
                if t.header_row:
                    t.header_rows.append(len(t.rows))
                t.rows.append(t.row)
            t.row = None
        elif tag == "table":
            self._stack.pop()
            self._finish(t)

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_heading:
            self._heading_buf.append(data)
        if self._stack:
            t = self._stack[-1]
            if t.cell is not None:
                t.cell.append(data)
            return
        self._push_text(data)

    def _finish(self, t: _TableBuild) -> None:
        if not t.rows:
            return
        context = " ".join("".join(self._text).split())[-_CONTEXT_CHARS:]
        self.tables.append(Table(t.rows[0], t.rows[1:], context, self._heading))
        self._text = []  # the next table's context starts after this one
        if len(t.rows) <= 2:  # tab strips / sticky headers label the tables that follow them
            self._push_text(" " + " ".join(" ".join(r) for r in t.rows) + " ")


def parse_tables(html_text: str) -> list[Table]:
    p = _TableParser()
    try:
        p.feed(html_text[:3_000_000])
        p.close()
    except Exception:  # noqa: S110 # pragma: no cover - lenient parser; keep whatever parsed
        pass
    return p.tables


# --------------------------------------------------------------------------------------------- tables
_COLUMN_UNITS: list[tuple[re.Pattern[str], tuple[str, str]]] = [
    (re.compile(r"^(?:rpm|requests? per minute)\b", re.I), ("requests", "minute")),
    (re.compile(r"^(?:rph|requests? per hour)\b", re.I), ("requests", "hour")),
    (re.compile(r"^(?:rpd|requests? per day)\b", re.I), ("requests", "day")),
    (re.compile(r"^(?:itpm|input tokens? per minute)\b", re.I), ("input_tokens", "minute")),
    (re.compile(r"^(?:otpm|output tokens? per minute)\b", re.I), ("output_tokens", "minute")),
    (re.compile(r"^(?:tpm|tokens? per minute)\b", re.I), ("total_tokens", "minute")),
    (re.compile(r"^(?:tph|tokens? per hour)\b", re.I), ("total_tokens", "hour")),
    (re.compile(r"^(?:tpd|tokens? per day)\b", re.I), ("total_tokens", "day")),
]
_MODEL_HEADER = re.compile(r"\bmodel(?:\s*(?:id|name))?\b", re.I)
_VALUE = re.compile(r"^\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([kKmMbB])?\s*(?:\d)?\s*$")
_FREE_CTX = re.compile(r"\bfree (?:tier|plan|trial|usage|access)\b|\bfree\b", re.I)
_PAID_CTX = re.compile(
    r"\b(?:developer (?:tier|plan)|paid (?:tier|plan)|pay[- ]as[- ]you[- ]go|tier [1-5]|"
    r"enterprise|scale tier|production tier|build tier)\b",
    re.I,
)
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{1,120}$")


def _limit(cell: str) -> int | None:
    m = _VALUE.match(cell.replace("\u00a0", " "))
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get((m.group(2) or "").lower(), 1)
    value = int(n * mult)
    return value if 0 < value < 10**13 else None


def classify_tier(context: str) -> str:
    """``free`` / ``paid`` only when the nearby text is unambiguous; otherwise ``unverified``."""
    free = bool(_FREE_CTX.search(context))
    paid = bool(_PAID_CTX.search(context))
    if free and not paid:
        return "free"
    if paid and not free:
        return "paid"
    return "unverified"


def _header_row(rows: list[list[str]]) -> int | None:
    for i, row in enumerate(rows[:3]):
        if any(_MODEL_HEADER.search(h) for h in row) and any(_column_unit(h) for h in row):
            return i
    return None


def _column_unit(header: str) -> tuple[str, str] | None:
    h_clean = re.sub(r"[^\w\s]", " ", header).strip()
    for rx, unit in _COLUMN_UNITS:
        if rx.search(h_clean):
            return unit
    return None


def table_claims(tables: list[Table]) -> list[Claim]:
    claims: list[Claim] = []
    seen: set[tuple[Any, ...]] = set()
    carried: list[str] | None = None  # sticky headers rendered as a separate one-row table
    for t in tables:
        rows = [t.headers, *t.rows]
        idx = _header_row(rows)
        if idx is not None:
            headers, body = rows[idx], rows[idx + 1 :]
        elif carried is not None and rows and len(rows[0]) == len(carried):
            headers, body = carried, rows
        else:
            carried = None
            continue
        carried = headers if not body else None
        model_col = next(i for i, h in enumerate(headers) if _MODEL_HEADER.search(h))
        cols = {i: u for i, h in enumerate(headers) if (u := _column_unit(h)) is not None}
        if not cols:
            continue
        tier = classify_tier(f"{t.heading} {t.context}")
        for row in body:
            if len(row) <= model_col:
                continue
            model = row[model_col].strip()
            if not _MODEL_ID.match(model):
                continue
            key_m = ("model", model)
            if key_m not in seen:
                seen.add(key_m)
                claims.append(
                    Claim("model", {"model": model, "source": "rate_limit_table"}, " | ".join(row)[:300], 0.6)
                )
            for i, (unit, period) in cols.items():
                if i >= len(row):
                    continue
                limit = _limit(row[i])
                if limit is None:
                    continue
                value = {"model": model, "unit": unit, "period": period, "limit": limit, "tier": tier}
                key = tuple(sorted(value.items()))
                if key in seen:
                    continue
                seen.add(key)
                excerpt = f"{' | '.join(headers)} :: {' | '.join(row)}"[:300]
                claims.append(Claim("model_quota", value, excerpt, 0.7 if tier != "unverified" else 0.5))
    return claims


# --------------------------------------------------------------------------------------------- JSON
def detect(url: str, content_type: str) -> str:
    u = url.lower().split("?", 1)[0].rstrip("/")
    if u == "https://openrouter.ai/api/v1/models":
        return "openrouter_models"
    if u == "https://router.huggingface.co/v1/models":
        return "hf_router_models"
    ct = content_type.lower()
    if "atom+xml" in ct or "rss+xml" in ct or u.endswith((".atom", ".rss", "/feed.xml", "/rss.xml")):
        return "feed"
    if "json" in content_type:
        return "json"
    return "html"


FEED_MAX_ENTRIES = 30
_ATOM = "{http://www.w3.org/2005/Atom}"


def feed_text(raw: str) -> str:
    """Atom/RSS (announcement posts, GitHub release feeds) to plain text, newest entries first.

    DTDs are refused outright so entity-expansion tricks never reach the parser. Each entry's
    HTML body is normalised like a page; the result feeds the ordinary text-claims pipeline.
    """
    import xml.etree.ElementTree as ET

    from oasis_route.discovery.extraction import normalise

    head = raw[:4096].upper()
    if "<!DOCTYPE" in head or "<!ENTITY" in raw.upper():
        return ""
    try:
        root = ET.fromstring(raw.encode("utf-8", "replace"))  # noqa: S314 - DTDs refused above
    except ET.ParseError:
        return ""

    def txt(node: ET.Element | None) -> str:
        return "".join(node.itertext()) if node is not None else ""

    out: list[str] = []
    if root.tag == f"{_ATOM}feed":
        for e in root.findall(f"{_ATOM}entry")[:FEED_MAX_ENTRIES]:
            title = txt(e.find(f"{_ATOM}title")).strip()
            when = txt(e.find(f"{_ATOM}updated")).strip() or txt(e.find(f"{_ATOM}published")).strip()
            body = txt(e.find(f"{_ATOM}content")) or txt(e.find(f"{_ATOM}summary"))
            out.append(f"{title} ({when})\n{normalise(body)}" if when else f"{title}\n{normalise(body)}")
    else:
        for item in root.iter("item"):
            if len(out) >= FEED_MAX_ENTRIES:
                break
            title = txt(item.find("title")).strip()
            when = txt(item.find("pubDate")).strip()
            body = txt(item.find("description"))
            out.append(f"{title} ({when})\n{normalise(body)}" if when else f"{title}\n{normalise(body)}")
    return "\n\n".join(x.strip() for x in out if x.strip())


def _zero(v: Any) -> bool:
    try:
        return v is not None and float(v) == 0.0
    except (TypeError, ValueError):
        return False


def _compact(obj: dict[str, Any], keys: tuple[str, ...]) -> str:
    return json.dumps({k: obj.get(k) for k in keys if k in obj}, sort_keys=True, default=str)[:300]


def openrouter_claims(raw: str) -> list[Claim]:
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    claims: list[Claim] = []
    free = 0
    for m in items[: _MAX_MODELS * 2]:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str):
            continue
        mid = m["id"][:200]
        raw_pricing = m.get("pricing")
        pricing: dict[str, Any] = raw_pricing if isinstance(raw_pricing, dict) else {}
        is_free = mid.endswith(":free") or (
            _zero(pricing.get("prompt"))
            and _zero(pricing.get("completion"))
            and (pricing.get("request") is None or _zero(pricing.get("request")))
        )
        if not is_free:
            continue
        free += 1
        ctxlen = m.get("context_length")
        raw_top = m.get("top_provider")
        top: dict[str, Any] = raw_top if isinstance(raw_top, dict) else {}
        value: dict[str, Any] = {
            "model": mid,
            "free": True,
            "context_window": ctxlen if isinstance(ctxlen, int) else None,
            "max_output_tokens": top.get("max_completion_tokens")
            if isinstance(top.get("max_completion_tokens"), int)
            else None,
            "tools": "tools" in (m.get("supported_parameters") or []),
            "source": "openrouter_models_api",
        }
        exp = m.get("expiration_date")
        if isinstance(exp, str) and re.match(r"^\d{4}-\d{2}-\d{2}", exp):
            value["expires"] = exp[:10]
        claims.append(Claim("model", value, _compact(m, ("id", "pricing", "context_length")), 0.9))
    claims.append(
        Claim(
            "catalogue",
            {"total_models": len(items), "free_models": free, "source": "openrouter_models_api"},
            f"openrouter.ai/api/v1/models: {len(items)} models, {free} free variants",
            0.9,
        )
    )
    if free:
        claims.append(
            Claim("free_tier", {"mentioned": True}, f"{free} models with zero prompt/completion price", 0.8)
        )
    return claims


def hf_router_claims(raw: str) -> list[Claim]:
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    by_provider: dict[str, dict[str, Any]] = {}
    for m in items[:_MAX_MODELS]:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str):
            continue
        for p in m.get("providers") or []:
            if not isinstance(p, dict) or not isinstance(p.get("provider"), str):
                continue
            slug = re.sub(r"[^a-z0-9-]", "", p["provider"].lower())[:64]
            if not slug:
                continue
            entry = by_provider.setdefault(
                slug,
                {
                    "provider": slug,
                    "provider_name": str(p.get("provider_name") or slug)[:120],
                    "models": 0,
                    "live_models": 0,
                    "free_models": [],
                },
            )
            entry["models"] += 1
            if p.get("status") == "live":
                entry["live_models"] += 1
            if p.get("is_free") is True and len(entry["free_models"]) < 50:
                entry["free_models"].append(m["id"][:200])
    claims = [
        Claim(
            "provider_listing",
            {**v, "source": "hf_router_models_api", "via": "huggingface"},
            f"router.huggingface.co lists {v['models']} models served by {v['provider_name']}",
            0.7,
        )
        for _, v in sorted(by_provider.items())
    ]
    claims.append(
        Claim(
            "catalogue",
            {"total_models": len(items), "providers": len(by_provider), "source": "hf_router_models_api"},
            f"router.huggingface.co/v1/models: {len(items)} models across {len(by_provider)} providers",
            0.9,
        )
    )
    return claims


def structured_claims(kind: str, raw: str) -> list[Claim]:
    if kind == "openrouter_models":
        return openrouter_claims(raw)
    if kind == "hf_router_models":
        return hf_router_claims(raw)
    if kind == "html":
        return table_claims(parse_tables(raw))
    return []


def canonical_json(raw: str) -> str | None:
    """Stable text for change detection of JSON documents (key order and whitespace independent)."""
    try:
        return json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"))
    except ValueError:
        return None
