"""Adapter registry: maps a provider's ``adapter_key`` to an adapter instance."""

from __future__ import annotations

from oasis_route.adapters.anthropic import AnthropicAdapter
from oasis_route.adapters.base import ProviderAdapter
from oasis_route.adapters.openai_compat import OpenAICompatibleAdapter, OpenAIProfile
from oasis_route.domain.enums import QuotaPeriod

_FULL = {"streaming": True, "tools": True, "json_mode": True, "vision": False}

PROFILES: dict[str, OpenAIProfile] = {
    "openai": OpenAIProfile("openai", default_capabilities=dict(_FULL)),
    "groq": OpenAIProfile(
        "groq",
        stream_usage_option=False,
        ratelimit_header_period=QuotaPeriod.DAY,
        default_capabilities=dict(_FULL),
    ),
    "mistral": OpenAIProfile("mistral", stream_usage_option=False, default_capabilities=dict(_FULL)),
    "openrouter": OpenAIProfile(
        "openrouter", default_capabilities=dict(_FULL), extra_headers={"x-title": "Oasis Route"}
    ),
    "gemini": OpenAIProfile("gemini", default_capabilities=dict(_FULL)),
    "cerebras": OpenAIProfile("cerebras", stream_usage_option=False, default_capabilities=dict(_FULL)),
    "custom": OpenAIProfile("custom", stream_usage_option=False),
    "mock": OpenAIProfile("mock", default_capabilities=dict(_FULL)),
    "mock_alt": OpenAIProfile("mock_alt", default_capabilities=dict(_FULL)),
}


class AdapterRegistry:
    def __init__(self) -> None:
        self._cache: dict[str, ProviderAdapter] = {}

    def for_provider(self, provider_id: str, adapter_key: str) -> ProviderAdapter:
        cache_key = f"{adapter_key}:{provider_id}"
        if cache_key not in self._cache:
            self._cache[cache_key] = self._build(provider_id, adapter_key)
        return self._cache[cache_key]

    @staticmethod
    def _build(provider_id: str, adapter_key: str) -> ProviderAdapter:
        if adapter_key == "anthropic":
            return AnthropicAdapter()
        if adapter_key == "openai_compatible":
            return OpenAICompatibleAdapter(PROFILES.get(provider_id, OpenAIProfile(provider_id)))
        raise KeyError(f"no adapter registered for '{adapter_key}'")

    @staticmethod
    def keys() -> list[str]:
        return ["openai_compatible", "anthropic"]
