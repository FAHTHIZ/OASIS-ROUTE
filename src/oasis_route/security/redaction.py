"""Secret redaction for logs, errors, traces, and exports.

Redaction is defence in depth: code should never pass secrets to loggers, but if it does, these patterns
remove common credential shapes before the record is emitted.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = re.compile(
    r"(authorization|api[_-]?key|x-api-key|secret|token|password|passwd|cookie|set-cookie|"
    r"refresh[_-]?token|access[_-]?token|client[_-]?secret|master[_-]?key|pepper|credential|private[_-]?key)",
    re.IGNORECASE,
)

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\boasis_(?:live|test)_[A-Za-z0-9_-]{16,}"),  # Oasis Keys
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}"),  # OpenAI / Anthropic style
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),  # Google API keys
    re.compile(r"\bgsk_[A-Za-z0-9]{20,}"),  # Groq
    re.compile(r"\bghp_[A-Za-z0-9]{30,}"),  # GitHub PAT
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),  # Slack
    re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}"),  # Google OAuth access token
    re.compile(r"\b1//[0-9A-Za-z_-]{20,}"),  # Google refresh token
    re.compile(r"(?i)(api[_-]?key|token|secret|password)=([^&\s\"']{6,})"),
]


def redact_text(text: str) -> str:
    out = text
    for pat in _PATTERNS:
        if pat.groups >= 2:
            out = pat.sub(lambda m: f"{m.group(1)}={REDACTED}", out)
        else:
            out = pat.sub(REDACTED, out)
    return out


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEYS.search(key))


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively redact mappings/sequences/strings."""
    if _depth > 8:
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            k: (REDACTED if isinstance(k, str) and is_sensitive_key(k) else redact(v, _depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v, _depth + 1) for v in value]
    return value


def sanitize_provider_error(text: str, limit: int = 500) -> str:
    """Provider error bodies are untrusted: redact, strip control chars, and truncate."""
    cleaned = "".join(ch for ch in text if ch.isprintable() or ch in " \t")
    cleaned = redact_text(cleaned)
    return cleaned[:limit] + ("…" if len(cleaned) > limit else "")
