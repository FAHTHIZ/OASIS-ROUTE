"""Application configuration with validation and safe defaults.

All settings are read from environment variables prefixed with ``OASIS_ROUTE_`` (or a ``.env`` file).
Secrets are typed as :class:`pydantic.SecretStr` so they never render in reprs or logs.
"""

from __future__ import annotations

import base64
import binascii
from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OASIS_ROUTE_", env_file=".env", extra="ignore")

    environment: Environment = Environment.DEVELOPMENT
    database_url: str = "sqlite+aiosqlite:///./oasis_route.db"
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    log_level: str = "INFO"
    log_json: bool = True

    # Key material. In development a deterministic insecure key is generated if unset; production refuses.
    master_key: SecretStr | None = Field(
        default=None, description="Base64 32-byte key-encryption key (KEK) for the credential vault."
    )
    previous_master_keys: list[SecretStr] = Field(
        default_factory=list, description="Retired KEKs still accepted for decryption during rotation."
    )
    api_key_pepper: SecretStr | None = Field(
        default=None, description="Secret used for HMAC hashing of Oasis Route API keys."
    )
    admin_bootstrap_token: SecretStr | None = Field(
        default=None, description="One-time token permitting creation of the first admin user."
    )

    # Gateway limits
    max_request_bytes: int = Field(default=2_000_000, ge=1024)
    default_max_output_tokens: int = Field(default=1024, ge=1)
    max_fallback_hops: int = Field(default=3, ge=0, le=10)
    upstream_connect_timeout_s: float = Field(default=10.0, gt=0)
    upstream_read_timeout_s: float = Field(default=120.0, gt=0)
    upstream_max_response_bytes: int = Field(default=20_000_000, ge=1024)
    reservation_ttl_s: int = Field(default=900, ge=10)
    rate_limit_per_key_rpm: int = Field(default=600, ge=1)
    allow_private_upstreams: bool = Field(
        default=False,
        description="Allow upstream endpoints on private networks (local models). Off by default.",
    )

    enable_mock_providers: bool | None = Field(
        default=None,
        description="Seed the deterministic mock providers. Defaults to on outside production.",
    )

    # Circuit breaker
    breaker_failure_threshold: int = Field(default=5, ge=1)
    breaker_base_cooldown_s: float = Field(default=15.0, gt=0)
    breaker_max_cooldown_s: float = Field(default=900.0, gt=0)

    # Discovery
    discovery_user_agent: str = "OasisRouteDiscovery/0.1 (+https://github.com/FAHTHIZ/OASIS-ROUTE)"
    discovery_max_bytes: int = Field(default=2_000_000, ge=1024)
    discovery_timeout_s: float = Field(default=20.0, gt=0)
    worker_interval_s: float = Field(default=30.0, gt=0)

    # Optional integrations
    gmail_enabled: bool = False
    gmail_compliance_attestation: str | None = Field(
        default=None,
        description="Reference to the completed Google OAuth verification / security assessment. "
        "Required before Gmail can be enabled in production.",
    )
    gmail_client_id: str | None = None
    gmail_client_secret: SecretStr | None = None
    gmail_redirect_uri: str | None = None

    otel_enabled: bool = False

    @field_validator("log_level")
    @classmethod
    def _level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("invalid log level")
        return v

    @field_validator("master_key")
    @classmethod
    def _kek(cls, v: SecretStr | None) -> SecretStr | None:
        if v is None:
            return v
        try:
            raw = base64.b64decode(v.get_secret_value(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("master_key must be base64") from exc
        if len(raw) != 32:
            raise ValueError("master_key must decode to 32 bytes")
        return v

    @field_validator("previous_master_keys")
    @classmethod
    def _prev(cls, v: list[SecretStr]) -> list[SecretStr]:
        for item in v:
            cls._kek(item)
        return v

    @model_validator(mode="after")
    def _production_requirements(self) -> Settings:
        if self.environment is Environment.PRODUCTION:
            missing = [name for name in ("master_key", "api_key_pepper") if getattr(self, name) is None]
            if missing:
                raise ValueError(f"production requires: {', '.join(missing)}")
            if self.database_url.startswith("sqlite"):
                raise ValueError("production requires PostgreSQL (database_url)")
            if self.gmail_enabled and not self.gmail_compliance_attestation:
                raise ValueError("Gmail cannot be enabled in production without gmail_compliance_attestation")
        return self

    # Derived helpers -------------------------------------------------------------------------
    @property
    def mock_providers_enabled(self) -> bool:
        if self.enable_mock_providers is not None:
            return self.enable_mock_providers
        return self.environment is not Environment.PRODUCTION

    def kek_bytes(self) -> bytes:
        if self.master_key is not None:
            return base64.b64decode(self.master_key.get_secret_value())
        if self.environment is Environment.PRODUCTION:  # pragma: no cover - guarded by validator
            raise RuntimeError("master_key required")
        # Insecure, deterministic development key. Never used in production (validator above).
        return b"oasis-route-dev-insecure-kek-32b"

    def previous_kek_bytes(self) -> list[bytes]:
        return [base64.b64decode(k.get_secret_value()) for k in self.previous_master_keys]

    def pepper_bytes(self) -> bytes:
        if self.api_key_pepper is not None:
            return self.api_key_pepper.get_secret_value().encode()
        return b"oasis-route-dev-insecure-pepper"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
