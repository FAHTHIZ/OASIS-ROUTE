"""Recognise which provider an API key the user pasted belongs to, without sending it anywhere.

Detection is local and deterministic:

1. A variable name the user supplied (``GROQ_API_KEY=...`` in a pasted ``.env``) names the provider.
2. Otherwise a documented, provider-specific key prefix identifies it (``gsk_`` for Groq, ``sk-or-`` for
   OpenRouter, ...).

Keys whose format is shared by several providers (a bare ``sk-...``, a 32-character token) are reported as
**ambiguous** with their candidate providers and are never tried against each provider in turn: sending a
user's key to a provider it was not issued by would disclose that secret to a third party. The user picks
the provider once, and the key then goes only to that provider's official endpoint.

Only values that look like keys are accepted from pasted text. Other variables (``DATABASE_URL``, passwords,
arbitrary settings) are ignored and never echoed back, so pasting a whole ``.env`` is safe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_KEYS = 50
MAX_TEXT_BYTES = 64 * 1024

# Environment variable names used by each provider's own SDK or documentation.
ENV_NAMES: dict[str, str] = {
    "OPENAI_API_KEY": "openai",
    "ANTHROPIC_API_KEY": "anthropic",
    "GEMINI_API_KEY": "gemini",
    "GOOGLE_API_KEY": "gemini",
    "GOOGLE_GENERATIVE_AI_API_KEY": "gemini",
    "GROQ_API_KEY": "groq",
    "MISTRAL_API_KEY": "mistral",
    "OPENROUTER_API_KEY": "openrouter",
    "CEREBRAS_API_KEY": "cerebras",
    "NVIDIA_API_KEY": "nvidia",
    "NVIDIA_NIM_API_KEY": "nvidia",
    "NIM_API_KEY": "nvidia",
    "SAMBANOVA_API_KEY": "sambanova",
    "HF_TOKEN": "huggingface",
    "HUGGINGFACE_API_KEY": "huggingface",
    "HUGGING_FACE_HUB_TOKEN": "huggingface",
    "HUGGINGFACEHUB_API_TOKEN": "huggingface",
    "COHERE_API_KEY": "cohere",
    "CO_API_KEY": "cohere",
    "ZAI_API_KEY": "zai",
    "ZHIPUAI_API_KEY": "zai",
}


@dataclass(frozen=True)
class KeyPattern:
    provider_id: str
    regex: re.Pattern[str]


# Ordered: more specific prefixes first (``sk-ant-`` and ``sk-or-`` before OpenAI's ``sk-proj-``).
PATTERNS: tuple[KeyPattern, ...] = (
    KeyPattern("anthropic", re.compile(r"^sk-ant-(api|admin)\d{2}-[A-Za-z0-9_\-]{20,}$")),
    KeyPattern("openrouter", re.compile(r"^sk-or-v\d+-[A-Za-z0-9]{32,}$")),
    KeyPattern("openai", re.compile(r"^sk-(proj|svcacct)-[A-Za-z0-9_\-]{20,}$")),
    KeyPattern("groq", re.compile(r"^gsk_[A-Za-z0-9]{40,}$")),
    KeyPattern("huggingface", re.compile(r"^hf_[A-Za-z0-9]{30,}$")),
    KeyPattern("nvidia", re.compile(r"^nvapi-[A-Za-z0-9_\-]{40,}$")),
    KeyPattern("cerebras", re.compile(r"^csk-[a-z0-9]{40,}$")),
    KeyPattern("gemini", re.compile(r"^AIza[0-9A-Za-z_\-]{35}$")),
    # Z.ai keys are "<32 hex>.<secret>" (https://z.ai/manage-apikey/apikey-list).
    KeyPattern("zai", re.compile(r"^[0-9a-f]{32}\.[A-Za-z0-9]{10,}$")),
)

# Shapes several providers share. Listed so the console can offer a short, correct choice.
AMBIGUOUS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"^sk-[A-Za-z0-9_\-]{20,}$"), ("openai",)),
    (re.compile(r"^[A-Za-z0-9]{32}$"), ("mistral",)),
    (re.compile(r"^[A-Za-z0-9]{40}$"), ("cohere",)),
    (
        re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
        ("sambanova",),
    ),
)

_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*(.*?)\s*$")
_TOKEN = re.compile(r"^[A-Za-z0-9_\-\.]{20,300}$")


@dataclass
class DetectedKey:
    api_key: str
    provider_id: str | None
    # name | prefix | user | ambiguous | unknown
    method: str
    candidates: list[str] = field(default_factory=list)
    env_name: str | None = None
    note: str = ""
    line: int | None = None  # 1-based line in pasted text, so a client can say which provider it is

    @property
    def hint(self) -> str:
        """A non-secret label for the key: its last four characters."""
        return "…" + self.api_key[-4:] if len(self.api_key) >= 8 else "…"


def by_prefix(api_key: str) -> str | None:
    for p in PATTERNS:
        if p.regex.match(api_key):
            return p.provider_id
    return None


def candidates_for(api_key: str) -> list[str]:
    out: list[str] = []
    for rx, providers in AMBIGUOUS:
        if rx.match(api_key):
            out += [p for p in providers if p not in out]
    return out


def detect(api_key: str, *, env_name: str | None = None, provider_id: str | None = None) -> DetectedKey:
    key = api_key.strip()
    prefix = by_prefix(key)
    if provider_id:
        note = ""
        if prefix and prefix != provider_id:
            note = f"key format looks like {prefix}; using the provider you chose"
        return DetectedKey(key, provider_id, "user", env_name=env_name, note=note)
    named = ENV_NAMES.get(env_name.upper()) if env_name else None
    if prefix:
        note = ""
        if named and named != prefix:
            note = f"variable {env_name} suggests {named}, but the key format is {prefix}'s"
        return DetectedKey(key, prefix, "prefix", env_name=env_name, note=note)
    if named:
        return DetectedKey(key, named, "name", env_name=env_name)
    cands = candidates_for(key)
    if cands:
        return DetectedKey(
            key,
            None,
            "ambiguous",
            candidates=cands,
            env_name=env_name,
            note="this key format is shared by several providers; choose the provider it was issued by",
        )
    return DetectedKey(
        key, None, "unknown", env_name=env_name, note="unrecognised key format; choose its provider"
    )


@dataclass
class ParsedText:
    keys: list[DetectedKey]
    ignored_lines: int = 0


def _unquote(v: str) -> str:
    v = v.split(" #", 1)[0].strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v.strip()


def parse_text(text: str) -> ParsedText:
    """Pull API keys out of pasted text: ``.env`` lines, ``export`` lines, or one bare key per line.

    A ``NAME=value`` line is taken only when ``NAME`` is a known provider variable or the value has a
    provider-specific key prefix. Everything else is ignored and counted, never returned.
    """
    out: list[DetectedKey] = []
    seen: set[str] = set()
    ignored = 0
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ASSIGN.match(line)
        if m:
            name, value = m.group(1), _unquote(m.group(2))
            if not value or not _TOKEN.match(value):
                ignored += 1
                continue
            if name.upper() not in ENV_NAMES and by_prefix(value) is None:
                ignored += 1
                continue
            d = detect(value, env_name=name)
        else:
            value = _unquote(line)
            if not _TOKEN.match(value):
                ignored += 1
                continue
            d = detect(value)
        if d.api_key in seen:
            continue
        seen.add(d.api_key)
        d.line = lineno
        out.append(d)
    return ParsedText(out, ignored)
