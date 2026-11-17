"""Credential vault with envelope encryption.

Each secret is encrypted with a fresh 256-bit data-encryption key (DEK) using AES-256-GCM. The DEK is wrapped
by a key-encryption key (KEK) from a :class:`KeyProvider`. Ciphertext is bound to its owning record through
associated data (AAD), so swapping ciphertext between rows fails authentication.

The default :class:`LocalKeyProvider` holds the KEK in process memory from configuration. Hosted
deployments should implement :class:`KeyProvider` against a managed KMS (wrap/unwrap calls),
leaving this module unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE = 12


class VaultError(Exception):
    pass


class KeyProvider(Protocol):
    @property
    def current_kek_id(self) -> str: ...

    def wrap(self, dek: bytes) -> tuple[str, bytes]: ...

    def unwrap(self, kek_id: str, wrapped: bytes) -> bytes: ...


def kek_id_for(kek: bytes) -> str:
    return "local:" + hashlib.sha256(b"oasis-route-kek-id" + kek).hexdigest()[:16]


class LocalKeyProvider:
    def __init__(self, current: bytes, previous: list[bytes] | None = None) -> None:
        if len(current) != 32:
            raise VaultError("KEK must be 32 bytes")
        self._keys: dict[str, bytes] = {kek_id_for(current): current}
        for k in previous or []:
            self._keys[kek_id_for(k)] = k
        self._current = kek_id_for(current)

    @property
    def current_kek_id(self) -> str:
        return self._current

    def wrap(self, dek: bytes) -> tuple[str, bytes]:
        nonce = os.urandom(_NONCE)
        ct = AESGCM(self._keys[self._current]).encrypt(nonce, dek, b"oasis-route-dek")
        return self._current, nonce + ct

    def unwrap(self, kek_id: str, wrapped: bytes) -> bytes:
        kek = self._keys.get(kek_id)
        if kek is None:
            raise VaultError("unknown KEK id; configure it in previous_master_keys to decrypt")
        try:
            return AESGCM(kek).decrypt(wrapped[:_NONCE], wrapped[_NONCE:], b"oasis-route-dek")
        except InvalidTag as exc:
            raise VaultError("DEK unwrap failed") from exc


@dataclass(frozen=True)
class SealedSecret:
    kek_id: str
    wrapped_dek: bytes
    ciphertext: bytes
    aad: str


class Vault:
    def __init__(self, keys: KeyProvider) -> None:
        self.keys = keys

    def seal(self, secret: dict[str, Any], aad: str) -> SealedSecret:
        dek = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(_NONCE)
        payload = json.dumps(secret, separators=(",", ":")).encode()
        ct = AESGCM(dek).encrypt(nonce, payload, aad.encode())
        kek_id, wrapped = self.keys.wrap(dek)
        return SealedSecret(kek_id=kek_id, wrapped_dek=wrapped, ciphertext=nonce + ct, aad=aad)

    def open(self, sealed: SealedSecret, expected_aad: str) -> dict[str, Any]:
        if sealed.aad != expected_aad:
            raise VaultError("credential bound to a different record")
        dek = self.keys.unwrap(sealed.kek_id, sealed.wrapped_dek)
        try:
            raw = AESGCM(dek).decrypt(
                sealed.ciphertext[:_NONCE], sealed.ciphertext[_NONCE:], sealed.aad.encode()
            )
        except InvalidTag as exc:
            raise VaultError("credential decryption failed") from exc
        value: dict[str, Any] = json.loads(raw)
        return value

    def rewrap(self, sealed: SealedSecret) -> SealedSecret:
        """Re-wrap the DEK under the current KEK (KEK rotation) without touching the ciphertext."""
        dek = self.keys.unwrap(sealed.kek_id, sealed.wrapped_dek)
        kek_id, wrapped = self.keys.wrap(dek)
        return SealedSecret(kek_id=kek_id, wrapped_dek=wrapped, ciphertext=sealed.ciphertext, aad=sealed.aad)
