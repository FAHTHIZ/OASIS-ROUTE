import base64

import pytest
from pydantic import ValidationError

from oasis_route.config import Environment, Settings


def test_production_requires_secrets_and_postgres() -> None:
    with pytest.raises(ValidationError):
        Settings(environment=Environment.PRODUCTION)
    key = base64.b64encode(b"k" * 32).decode()
    with pytest.raises(ValidationError, match="PostgreSQL"):
        Settings(environment=Environment.PRODUCTION, master_key=key, api_key_pepper="p" * 32)
    s = Settings(
        environment=Environment.PRODUCTION,
        master_key=key,
        api_key_pepper="p" * 32,
        database_url="postgresql+asyncpg://u:p@db/x",
    )
    assert s.kek_bytes() == b"k" * 32


def test_master_key_must_be_32_bytes() -> None:
    with pytest.raises(ValidationError):
        Settings(master_key=base64.b64encode(b"short").decode())


def test_gmail_gate_in_production() -> None:
    key = base64.b64encode(b"k" * 32).decode()
    with pytest.raises(ValidationError, match="Gmail"):
        Settings(
            environment=Environment.PRODUCTION,
            master_key=key,
            api_key_pepper="p",
            database_url="postgresql+asyncpg://u:p@db/x",
            gmail_enabled=True,
        )


def test_secrets_not_in_repr() -> None:
    s = Settings(api_key_pepper="super-secret-pepper")
    assert "super-secret-pepper" not in repr(s)
