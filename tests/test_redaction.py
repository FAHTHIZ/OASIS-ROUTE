import json
import logging

from oasis_route.logging import RedactingJsonFormatter
from oasis_route.security.redaction import REDACTED, redact, redact_text, sanitize_provider_error

SECRETS = [
    "sk-proj-abcdefghijklmnopqrstuvwxyz123456",
    "sk-ant-api03-abcdefghijklmnopqrstuv",
    "AIzaSyA1234567890abcdefghijklmnopqrstuv",
    "gsk_abcdefghijklmnopqrstuvwxyz1234",
    "or_live_abcdefghijkl_ZZZZZZZZZZZZZZZZZZZZZZ",
    "ya29.a0AfH6SMBabcdefghijklmnopqrstu",
]


def test_patterns_redacted() -> None:
    for s in SECRETS:
        out = redact_text(f"error using key {s} here")
        assert s not in out, s
        assert REDACTED in out
    assert "Bearer" not in redact_text("Authorization: Bearer abcdefghijklmnop") or REDACTED in redact_text(
        "Authorization: Bearer abcdefghijklmnop"
    )
    assert "hunter22" not in redact_text("https://x.test/?api_key=hunter22&q=1")


def test_sensitive_keys_redacted_recursively() -> None:
    data = {
        "headers": {"Authorization": "Bearer abc", "x-api-key": "k"},
        "nested": [{"password": "p"}],
        "ok": "value",
    }
    out = redact(data)
    assert out["headers"]["Authorization"] == REDACTED
    assert out["headers"]["x-api-key"] == REDACTED
    assert out["nested"][0]["password"] == REDACTED
    assert out["ok"] == "value"


def test_log_formatter_redacts_message_and_extras() -> None:
    fmt = RedactingJsonFormatter(json_output=True)
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "using %s", (SECRETS[0],), None)
    rec.api_key = "whatever"
    rec.detail = f"token {SECRETS[3]}"
    line = fmt.format(rec)
    for s in SECRETS:
        assert s not in line
    parsed = json.loads(line)
    assert parsed["api_key"] == REDACTED


def test_sanitize_provider_error_truncates_and_strips() -> None:
    raw = "bad key sk-abcdefghijklmnopqrstuvwxyz\x00\x07" + "x" * 1000
    out = sanitize_provider_error(raw, limit=100)
    assert "sk-abcdefghijklmnop" not in out
    assert "\x00" not in out
    assert len(out) <= 101
