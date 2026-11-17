"""Deterministic claim extraction from fetched pages.

Untrusted page content is only ever *matched* against fixed regular expressions. Nothing in a page can
change control flow, call tools, reach secrets, or set an offer's state: results are candidate claims
with excerpts and hashes that a human reviewer verifies (threat T8, prompt injection).
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

_MAX_TEXT = 400_000
_EXCERPT = 160

_DROP_BLOCKS = re.compile(r"<(script|style|noscript|template|svg|iframe)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_COMMENTS = re.compile(r"<!--.*?-->", re.S)
_BREAKS = re.compile(r"<(br|/p|/div|/li|/tr|/h[1-6]|/td|/th)\b[^>]*>", re.I)
_TAGS = re.compile(r"<[^>]+>")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2060-\u2064]")
_SPACES = re.compile(r"[ \t\r\f\v]+")


def normalise(content: str, content_type: str = "text/html") -> str:
    """HTML/markdown/plain text to normalised plain text (bounded, control/bidi characters removed)."""
    text = content[: _MAX_TEXT * 2]
    if "html" in content_type or "xml" in content_type or "<" in text[:2000]:
        text = _COMMENTS.sub(" ", text)
        text = _DROP_BLOCKS.sub(" ", text)
        text = _BREAKS.sub("\n", text)
        text = _TAGS.sub(" ", text)
        text = html.unescape(text)
    text = _CONTROL.sub("", text)
    lines = [_SPACES.sub(" ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)[:_MAX_TEXT]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class Claim:
    claim_type: str  # quota | credit | min_age | region | expiry | model | terms | free_tier | prohibited
    value: dict[str, Any]
    excerpt: str
    confidence: float

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.excerpt.encode()).hexdigest()


@dataclass
class Extraction:
    claims: list[Claim] = field(default_factory=list)

    def of(self, claim_type: str) -> list[Claim]:
        return [c for c in self.claims if c.claim_type == claim_type]

    @property
    def prohibited_flags(self) -> list[str]:
        return sorted({str(c.value["flag"]) for c in self.of("prohibited")})

    def summary(self) -> dict[str, Any]:
        """Normalised, order-independent view used for change detection between fetches."""
        out: dict[str, Any] = {}
        for c in self.claims:
            out.setdefault(c.claim_type, set()).add(tuple(sorted((k, str(v)) for k, v in c.value.items())))
        return {k: sorted(v) for k, v in sorted(out.items())}


def _excerpt(text: str, start: int, end: int) -> str:
    a, b = max(0, start - _EXCERPT // 2), min(len(text), end + _EXCERPT // 2)
    return text[a:b].replace("\n", " ").strip()


_NUM = r"(\d{1,3}(?:[,\s]\d{3})+|\d+(?:\.\d+)?)\s*([kKmM])?"
_PERIOD = {
    "minute": "minute",
    "min": "minute",
    "hour": "hour",
    "hr": "hour",
    "day": "day",
    "month": "month",
    "mo": "month",
}
_ACRONYMS = {
    "rpm": ("requests", "minute"),
    "rpd": ("requests", "day"),
    "rph": ("requests", "hour"),
    "tpm": ("total_tokens", "minute"),
    "tpd": ("total_tokens", "day"),
    "itpm": ("input_tokens", "minute"),
    "otpm": ("output_tokens", "minute"),
}
_QUOTA_LONG = re.compile(
    _NUM
    + r"\s*(requests?|reqs?|calls?|(?:input |output )?tokens?)\s*(?:per|/|a|an|each|every)\s*"
    + r"(minute|min|hour|hr|day|month|mo)\b",
    re.I,
)
_QUOTA_ACRO = re.compile(_NUM + r"\s*(RPM|RPD|RPH|TPM|TPD|ITPM|OTPM)\b")
_CREDIT = re.compile(
    r"\$\s?(\d+(?:\.\d{1,2})?)\s*(?:USD\s*)?(?:in\s+)?(?:free\s+)?(?:trial\s+)?(?:api\s+)?credits?",
    re.I,
)
_ONE_TIME_HINT = re.compile(r"one[- ]time|trial|sign[- ]?up|new accounts?|expire|valid for|first \d+", re.I)
_MONTHLY_HINT = re.compile(r"(?:per|each|every|a)\s+month|monthly|resets?", re.I)
_AGE = re.compile(
    r"(?:at least|minimum age(?: of)?|must be|aged?|over(?: the age of)?)\s*(1[3-9]|2[01])\s*"
    r"(?:years|\+|or older)"
    r"|(1[3-9]|2[01])\s*\+\s*(?:only|years)"
    r"|(?:under|younger than)\s*(1[3-9]|2[01])\b[^.]{0,40}not (?:permitted|allowed|eligible)",
    re.I,
)
_REGION = re.compile(
    r"(not (?:currently )?(?:available|supported|offered) in|unavailable in|only available in|"
    r"restricted to|supported (?:countries|regions))\b[^.\n]{0,160}",
    re.I,
)
_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|december|"
    "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
_EXPIRY = re.compile(
    r"(?:until|expires?(?: on)?|ends?(?: on)?|through|valid (?:until|through)|available until)\s+"
    rf"(?:(\d{{4}}-\d{{2}}-\d{{2}})|(?:({_MONTHS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})))",
    re.I,
)
_PRICE_BEFORE = re.compile(r"(?:[$€£]\s?\d[\d,.]*|\d[\d,.]*\s?(?:usd|eur|per\s+1m))[^.\n]{0,25}$", re.I)
_OFFER_WORDS = re.compile(r"\b(?:credits?|trial|free|promo(?:tion|tional)?|offer)\b", re.I)
_FREE = re.compile(r"\bfree (?:tier|plan|of charge|usage|access)\b|\bno credit card\b|\bfree to use\b", re.I)
_NO_CARD = re.compile(
    r"\bno (?:credit )?card (?:is )?(?:required|needed)\b|\bwithout (?:a |adding a )?(?:credit )?card\b|"
    r"\bno payment (?:method|information) (?:is )?(?:required|needed)\b",
    re.I,
)
_CARD = re.compile(
    r"\b(?:credit card|payment method|billing (?:account|information)) (?:is |must be )?"
    r"(?:required|needed|must be (?:added|linked|on file))\b|"
    r"\b(?:add|link|attach) a (?:credit card|payment method) to (?:use|access|unlock|start)\b",
    re.I,
)
_TRIAL_DAYS = re.compile(
    r"\b(\d{1,3})[- ]day (?:free )?trial\b|\btrial (?:credits? )?(?:valid|lasts?|expires?) (?:for|after) "
    r"(\d{1,3}) days\b|\bcredits?,?\s+(?:which\s+)?(?:expire|are valid for|valid for) (?:after |in )?"
    r"(\d{1,3}) days\b",
    re.I,
)
_EVALUATION = re.compile(
    r"\b(?:for )?(?:evaluation|prototyping|testing) (?:and (?:testing|evaluation|experimentation) )?"
    r"(?:purposes )?only\b|\bnot (?:intended |to be used |for use )?(?:for|in) production\b|"
    r"\bshould not be used in production\b",
    re.I,
)
_TERMS = re.compile(r"\b(?:terms of (?:service|use)|acceptable use policy|usage polic(?:y|ies))\b", re.I)
_RESTRICT = {
    "no_commercial_use": re.compile(
        r"non[- ]commercial (?:use )?only|not (?:for|permitted for) commercial", re.I
    ),
    "single_account_per_person": re.compile(
        r"(?:one|single) (?:free )?account per (?:person|user|individual|customer)", re.I
    ),
    "not_transferable": re.compile(
        r"(?:credits?|keys?|accounts?) (?:are|is) (?:non-?|not )transferable", re.I
    ),
}
# Offers whose only access path is a prohibited practice are marked INELIGIBLE automatically. A match that
# is negated nearby ("do not create multiple accounts...", "...is prohibited") is a *restriction* instead.
_NEGATION = re.compile(
    r"\b(?:not|never|no|don't|do not|cannot|can't|must not|may not|prohibit\w*|forbid\w*|ban\w*|"
    r"violat\w*|against)\b",
    re.I,
)
_PROHIBITED = {
    "account_cycling": re.compile(
        r"(?:create|make|open|register|sign up for) (?:new|multiple|additional|many|several) accounts? "
        r"(?:to|for) (?:get|obtain|receive|keep|reset|more)",
        re.I,
    ),
    "disposable_email": re.compile(
        r"(?:use|with|using|register with|sign up with) (?:a |any )?"
        r"(?:disposable|temporary|throwaway|burner) e-?mails?",
        re.I,
    ),
    "key_rotation_to_evade_limits": re.compile(
        r"rotat(?:e|ing) (?:between )?(?:multiple )?(?:api )?(?:keys|accounts) (?:to|for) "
        r"(?:avoid|bypass|evade|get around|increase)",
        re.I,
    ),
    "captcha_bypass": re.compile(r"(?:bypass|solve|skip)(?:ing)? (?:the )?captcha", re.I),
    "leaked_credentials": re.compile(
        r"(?:leaked|shared|public|free) api keys? (?:list|dump)|keys? leaked", re.I
    ),
}


def _number(raw: str, suffix: str | None) -> int:
    n = float(re.sub(r"[,\s]", "", raw))
    mult = {"k": 1_000, "m": 1_000_000}.get((suffix or "").lower(), 1)
    return int(n * mult)


def _unit(word: str) -> str:
    w = word.lower()
    if w.startswith("input"):
        return "input_tokens"
    if w.startswith("output"):
        return "output_tokens"
    if w.startswith("token"):
        return "total_tokens"
    return "requests"


def _parse_date(m: re.Match[str]) -> str | None:
    if m.group(1):
        return m.group(1)
    month_names = [x for x in _MONTHS.split("|")]
    mon = m.group(2).lower()
    full = month_names[:12]
    idx = next((i for i, n in enumerate(full) if n.startswith(mon[:3])), None)
    if idx is None:
        return None
    try:
        return datetime(int(m.group(4)), idx + 1, int(m.group(3)), tzinfo=UTC).date().isoformat()
    except ValueError:
        return None


def extract(text: str, *, known_models: list[str] | None = None) -> Extraction:
    """Extract candidate claims from normalised text. Deterministic; same input, same output."""
    out = Extraction()
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: dict[str, Any], m: re.Match[str], conf: float) -> None:
        key = (kind, repr(sorted(value.items())))
        if key in seen:
            return
        seen.add(key)
        out.claims.append(Claim(kind, value, _excerpt(text, m.start(), m.end()), conf))

    for m in _QUOTA_LONG.finditer(text):
        add(
            "quota",
            {
                "limit": _number(m.group(1), m.group(2)),
                "unit": _unit(m.group(3)),
                "period": _PERIOD[m.group(4).lower()],
            },
            m,
            0.5,
        )
    for m in _QUOTA_ACRO.finditer(text):
        unit, period = _ACRONYMS[m.group(3).lower()]
        add("quota", {"limit": _number(m.group(1), m.group(2)), "unit": unit, "period": period}, m, 0.5)
    for m in _CREDIT.finditer(text):
        window = text[max(0, m.start() - 160) : m.end() + 160]
        if _ONE_TIME_HINT.search(window):
            ctype = "one_time"
        elif _MONTHLY_HINT.search(window):
            ctype = "recurring"
        else:
            ctype = "unknown"
        add("credit", {"amount_usd": float(m.group(1)), "credit_type": ctype}, m, 0.4)
    for m in _AGE.finditer(text):
        age = next(g for g in m.groups() if g)
        add("min_age", {"min_age": int(age)}, m, 0.6)
    for m in _REGION.finditer(text):
        add("region", {"statement": m.group(0)[:200].strip()}, m, 0.3)
    for m in _EXPIRY.finditer(text):
        # "$2.00 through November 30, 2026" is a price schedule, not the end of an offer.
        before = re.split(r"[.;:]\s|\n", text[max(0, m.start() - 40) : m.start()])[-1]  # same clause only
        if _PRICE_BEFORE.search(before) and not _OFFER_WORDS.search(before):
            continue
        d = _parse_date(m)
        if d:
            add("expiry", {"date": d}, m, 0.4)
    for m in _FREE.finditer(text):
        add("free_tier", {"mentioned": True}, m, 0.3)
        break
    no_card = _NO_CARD.search(text)
    if no_card is not None:
        add("billing", {"card_required": False}, no_card, 0.5)
    elif (card := _CARD.search(text)) is not None:
        add("billing", {"card_required": True}, card, 0.5)
    for m in _TRIAL_DAYS.finditer(text):
        days = int(next(g for g in m.groups() if g))
        if 0 < days <= 366:
            add("trial", {"days": days}, m, 0.5)
    if (ev := _EVALUATION.search(text)) is not None:
        add("usage_scope", {"evaluation_only": True}, ev, 0.5)
    for m in _TERMS.finditer(text):
        add("terms", {"mentioned": True}, m, 0.2)
        break
    for flag, rx in _RESTRICT.items():
        if (rm := rx.search(text)) is not None:
            add("restriction", {"flag": flag}, rm, 0.5)
    for flag, rx in _PROHIBITED.items():
        for pm in rx.finditer(text):
            before = text[max(0, pm.start() - 60) : pm.start()]
            after = text[pm.end() : pm.end() + 40]
            if _NEGATION.search(before) or re.match(
                r"[^.]{0,30}\b(?:is|are) (?:not allowed|prohibited|forbidden)", after
            ):
                add("restriction", {"flag": f"forbids_{flag}"}, pm, 0.5)
            else:
                add("prohibited", {"flag": flag}, pm, 0.6)
    for model in known_models or []:
        rx = re.compile(r"(?<![\w.-])" + re.escape(model) + r"(?![\w-])", re.I)
        if (mm := rx.search(text)) is not None:
            add("model", {"model": model}, mm, 0.5)
    return out


def eligibility_from(ex: Extraction) -> dict[str, Any]:
    """Candidate eligibility facts. Unknown stays unknown (absent); reviewers confirm before activation."""
    elig: dict[str, Any] = {}
    ages = [int(c.value["min_age"]) for c in ex.of("min_age")]
    if ages:
        elig["min_age"] = max(ages)
    flags = {str(c.value["flag"]) for c in ex.of("restriction")}
    if "no_commercial_use" in flags:
        elig["commercial_use"] = False
    if "single_account_per_person" in flags:
        elig["poolable"] = False
    if "not_transferable" in flags:
        elig["transferable"] = False
    if ex.prohibited_flags:
        elig["requires_prohibited_practice"] = ex.prohibited_flags
    if "account_cycling" in ex.prohibited_flags:
        elig["requires_account_cycling"] = True
    regions = [str(c.value["statement"]) for c in ex.of("region")]
    if regions:
        elig["region_statements"] = regions[:5]
    return elig
