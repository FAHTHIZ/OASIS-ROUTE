"""Oasis Route API keys.

Format: ``or_<env>_<lookup>_<secret>`` where ``lookup`` is 12 random base32 chars (stored, indexed) and
``secret`` is 32 random bytes (urlsafe base64). The full key is hashed with HMAC-SHA256 under a server pepper;
because keys carry 256 bits of entropy, a keyed hash is appropriate (no password stretching needed).
The plaintext key is returned exactly once, at creation/rotation time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass

_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"


@dataclass(frozen=True)
class IssuedKey:
    plaintext: str
    prefix: str
    key_hash: str


def _lookup() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(12))


def hash_key(plaintext: str, pepper: bytes) -> str:
    return hmac.new(pepper, plaintext.encode(), hashlib.sha256).hexdigest()


def issue_key(pepper: bytes, env: str = "live") -> IssuedKey:
    env = "test" if env != "live" else "live"
    prefix = f"or_{env}_{_lookup()}"
    secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    plaintext = f"{prefix}_{secret}"
    return IssuedKey(plaintext=plaintext, prefix=prefix, key_hash=hash_key(plaintext, pepper))


def parse_prefix(plaintext: str) -> str | None:
    parts = plaintext.split("_", 3)
    if len(parts) != 4 or parts[0] != "or" or parts[1] not in {"live", "test"} or len(parts[2]) != 12:
        return None
    return "_".join(parts[:3])


def verify_key(plaintext: str, stored_hash: str, pepper: bytes) -> bool:
    return hmac.compare_digest(hash_key(plaintext, pepper), stored_hash)
