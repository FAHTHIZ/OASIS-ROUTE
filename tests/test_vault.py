import os

import pytest

from oasis_route.security.vault import LocalKeyProvider, SealedSecret, Vault, VaultError


def _vault(kek: bytes | None = None, prev: list[bytes] | None = None) -> Vault:
    return Vault(LocalKeyProvider(kek or os.urandom(32), prev))


def test_roundtrip_and_ciphertext_opaque() -> None:
    v = _vault()
    sealed = v.seal({"api_key": "sk-test-super-secret-value"}, aad="connection:1")
    assert b"super-secret" not in sealed.ciphertext
    assert b"super-secret" not in sealed.wrapped_dek
    assert v.open(sealed, "connection:1") == {"api_key": "sk-test-super-secret-value"}


def test_aad_binding() -> None:
    v = _vault()
    sealed = v.seal({"k": "v"}, aad="connection:1")
    with pytest.raises(VaultError):
        v.open(sealed, "connection:2")
    forged = SealedSecret(sealed.kek_id, sealed.wrapped_dek, sealed.ciphertext, "connection:2")
    with pytest.raises(VaultError):
        v.open(forged, "connection:2")


def test_tamper_detected() -> None:
    v = _vault()
    s = v.seal({"k": "v"}, aad="a")
    bad = SealedSecret(s.kek_id, s.wrapped_dek, s.ciphertext[:-1] + bytes([s.ciphertext[-1] ^ 1]), s.aad)
    with pytest.raises(VaultError):
        v.open(bad, "a")


def test_kek_rotation_rewrap() -> None:
    old, new = os.urandom(32), os.urandom(32)
    v_old = _vault(old)
    sealed = v_old.seal({"k": "v"}, aad="a")
    v_new = _vault(new, prev=[old])
    assert v_new.open(sealed, "a") == {"k": "v"}  # still decryptable with previous KEK
    rewrapped = v_new.rewrap(sealed)
    assert rewrapped.kek_id == v_new.keys.current_kek_id
    assert _vault(new).open(rewrapped, "a") == {"k": "v"}  # old KEK no longer needed
    with pytest.raises(VaultError):
        _vault(new).open(sealed, "a")


def test_wrong_kek_size() -> None:
    with pytest.raises(VaultError):
        LocalKeyProvider(b"short")
