"""Adapter registry: maps a provider's ``adapter_key`` to an adapter instance.

Adding a provider never touches the router core. In order of precedence a provider gets:

1. a hand-written profile in :data:`PROFILES` (curated seeds with reviewed quirks),
2. a local-server preset (:data:`LOCAL_PROFILES`: Ollama, llama.cpp, vLLM, LM Studio, ...), or
3. a profile generated from the imported catalogue entry (auth header and prefix, extra headers, model
   listing URL), see :func:`profile_from_catalog`,
4. otherwise the plain protocol defaults.
"""

from __future__ import annotations

from typing import Any

from oasis_route.adapters.anthropic import AnthropicAdapter, AnthropicProfile
from oasis_route.adapters.base import ProviderAdapter
from oasis_route.adapters.openai_compat import OpenAICompatibleAdapter, OpenAIProfile
from oasis_route.domain.enums import QuotaPeriod
from oasis_route.registry import imported

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
    # Catalogues mixing chat, code and base models: only streaming is assumed for every model. Tool and
    # JSON support is left unknown rather than over-claimed. Usage in streams is estimated when absent.
    "nvidia": OpenAIProfile("nvidia", stream_usage_option=False, default_capabilities={"streaming": True}),
    "sambanova": OpenAIProfile(
        "sambanova", stream_usage_option=False, default_capabilities={"streaming": True}
    ),
    "huggingface": OpenAIProfile(
        "huggingface", stream_usage_option=False, default_capabilities={"streaming": True}
    ),
    # https://docs.cohere.com/docs/compatibility-api ; models from the native list endpoint
    # (https://docs.cohere.com/reference/list-models), filtered to chat-capable models.
    "cohere": OpenAIProfile(
        "cohere",
        stream_usage_option=False,
        default_capabilities={"streaming": True, "tools": True, "json_mode": True, "vision": False},
        models_url="https://api.cohere.com/v1/models?endpoint=chat&page_size=1000",
        models_key="models",
        models_id_field="name",
    ),
    # https://docs.z.ai/guides/develop/http/introduction ; no public model-list endpoint is documented, so
    # keys are checked with a 1-token call to GLM-4.5-Flash, which the pricing page lists as free.
    "zai": OpenAIProfile(
        "zai",
        stream_usage_option=False,
        default_capabilities={"streaming": True, "tools": True, "json_mode": False, "vision": False},
        list_models_supported=False,
        probe_model="glm-4.5-flash",
    ),
    "custom": OpenAIProfile("custom", stream_usage_option=False),
    "mock": OpenAIProfile("mock", default_capabilities=dict(_FULL)),
    "mock_alt": OpenAIProfile("mock_alt", default_capabilities=dict(_FULL)),
}

# Local OpenAI-compatible servers: they ignore the API key, and their model lists say nothing about tools.
LOCAL_PROFILES: dict[str, OpenAIProfile] = {
    pid: OpenAIProfile(
        pid, stream_usage_option=usage, default_capabilities={"streaming": True}, api_key_required=False
    )
    for pid, usage in (
        ("ollama", True),
        ("llama_cpp", True),
        ("vllm", True),
        ("lm_studio", True),
        ("lemonade", False),
        ("llamafile", False),
        ("docker_model_runner", False),
        ("xinference", False),
    )
}


def _auth(style: str | None, prefix: str | None) -> tuple[str, str]:
    """(header, prefix) for an imported auth style: ``bearer``, ``x-api-key`` or ``header:<Name>``."""
    style = (style or "bearer").strip()
    if style == "bearer":
        return "authorization", "Bearer " if prefix is None else str(prefix)
    if style.startswith("header:") and style[7:]:
        return style[7:].lower(), str(prefix or "")
    return style.lower(), str(prefix or "")


def profile_from_catalog(provider_id: str, facts: dict[str, Any]) -> OpenAIProfile:
    auth = facts.get("auth") or {}
    header, prefix = _auth(auth.get("style"), auth.get("prefix"))
    models_url = facts.get("models_url")
    return OpenAIProfile(
        provider_id,
        stream_usage_option=False,  # estimated when absent; unknown params are not sent to unknown servers
        default_capabilities={"streaming": True},
        extra_headers={str(k).lower(): str(v) for k, v in (facts.get("extra_headers") or {}).items()},
        models_url=models_url if isinstance(models_url, str) and models_url.startswith("https://") else None,
        auth_header=header,
        auth_prefix=prefix,
        api_key_required=auth.get("required", True) is not False,
    )


def anthropic_profile_from_catalog(provider_id: str, facts: dict[str, Any]) -> AnthropicProfile:
    auth = facts.get("auth") or {}
    style = auth.get("style") or "x-api-key"
    header, prefix = _auth(style, auth.get("prefix"))
    return AnthropicProfile(
        provider_id=provider_id,
        auth_header=header,
        auth_prefix=prefix,
        extra_headers={str(k).lower(): str(v) for k, v in (facts.get("extra_headers") or {}).items()},
    )


def openai_profile(provider_id: str) -> OpenAIProfile:
    if provider_id in PROFILES:
        return PROFILES[provider_id]
    if provider_id in LOCAL_PROFILES:
        return LOCAL_PROFILES[provider_id]
    entry = imported.get(provider_id)
    if entry is not None:
        return profile_from_catalog(provider_id, entry.catalog_facts({}))
    return OpenAIProfile(provider_id)


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
            entry = imported.get(provider_id) if provider_id != "anthropic" else None
            if entry is None:
                return AnthropicAdapter()
            return AnthropicAdapter(anthropic_profile_from_catalog(provider_id, entry.catalog_facts({})))
        if adapter_key == "openai_compatible":
            return OpenAICompatibleAdapter(openai_profile(provider_id))
        raise KeyError(f"no adapter registered for '{adapter_key}'")

    @staticmethod
    def keys() -> list[str]:
        return ["openai_compatible", "anthropic"]
