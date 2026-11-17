"""How a user actually gets access to each provider: official consoles, key pages, OAuth availability.

These are *links the user opens themselves*. Oasis Route never signs up, logs in, creates keys, or solves
challenges on the user's behalf. Where a provider offers an official delegated flow (OAuth / PKCE key
exchange) the connection assistant uses it; otherwise it guides the user to the provider's own key page and
accepts the key through the encrypted vault.

URLs were checked against each provider's public documentation on 2026-11-17. They are display-only (never
fetched by the server) and an operator can override any of them per offer through ``Offer.links``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class AccessInfo:
    signup: str | None = None
    console: str | None = None
    api_key: str | None = None
    docs: str | None = None
    pricing: str | None = None
    rate_limits: str | None = None
    # free_tier_hint: what the provider documents about no-cost access, verbatim enough to set expectations.
    # It is a hint for the UI only; routing never relies on it (offers and connection billing modes do).
    free_tier_hint: str = ""
    card_required: bool | None = None  # None == unknown
    default_billing_mode: str = "unknown"

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "")}


ACCESS: dict[str, AccessInfo] = {
    "openai": AccessInfo(
        signup="https://platform.openai.com/signup",
        console="https://platform.openai.com/",
        api_key="https://platform.openai.com/api-keys",
        docs="https://developers.openai.com/api/docs",
        rate_limits="https://developers.openai.com/api/docs/guides/rate-limits",
        free_tier_hint="Usage is billed; API access requires a funded account.",
        card_required=True,
        default_billing_mode="paid",
    ),
    "anthropic": AccessInfo(
        console="https://console.anthropic.com/",
        api_key="https://console.anthropic.com/settings/keys",
        docs="https://platform.claude.com/docs/",
        rate_limits="https://platform.claude.com/docs/en/api/rate-limits",
        free_tier_hint="Usage is billed against prepaid credits.",
        card_required=True,
        default_billing_mode="paid",
    ),
    "gemini": AccessInfo(
        console="https://aistudio.google.com/",
        api_key="https://aistudio.google.com/apikey",
        docs="https://ai.google.dev/gemini-api/docs",
        pricing="https://aistudio.google.com/docs/pricing",
        rate_limits="https://aistudio.google.com/docs/rate-limits",
        free_tier_hint="Projects without billing enabled use the free tier with per-model limits.",
        card_required=False,
    ),
    "groq": AccessInfo(
        console="https://console.groq.com/",
        api_key="https://console.groq.com/keys",
        docs="https://console.groq.com/docs",
        rate_limits="https://console.groq.com/docs/rate-limits",
        free_tier_hint="Free plan with per-model request and token limits; Developer plan is paid.",
        card_required=False,
    ),
    "mistral": AccessInfo(
        console="https://console.mistral.ai/",
        api_key="https://console.mistral.ai/api-keys",
        docs="https://docs.mistral.ai/",
        pricing="https://mistral.ai/pricing",
        rate_limits="https://docs.mistral.ai/admin/billing-usage/usage-limits",
        free_tier_hint="Workspace usage limits depend on the plan; check the console limits page.",
    ),
    "openrouter": AccessInfo(
        signup="https://openrouter.ai/",
        console="https://openrouter.ai/settings",
        api_key="https://openrouter.ai/settings/keys",
        docs="https://openrouter.ai/docs",
        pricing="https://openrouter.ai/models",
        rate_limits="https://openrouter.ai/docs/api-reference/limits",
        free_tier_hint="Models with a :free suffix cost nothing and have their own daily request limits.",
        card_required=False,
    ),
    "cerebras": AccessInfo(
        console="https://cloud.cerebras.ai/",
        api_key="https://cloud.cerebras.ai/",
        docs="https://inference-docs.cerebras.ai/",
        rate_limits="https://inference-docs.cerebras.ai/support/rate-limits",
        free_tier_hint="Free trial credits with tier limits; Pay as You Go is billed.",
    ),
    "nvidia": AccessInfo(
        console="https://build.nvidia.com/",
        api_key="https://build.nvidia.com/settings/api-keys",
        docs="https://docs.api.nvidia.com/nim/reference/llm-apis",
        free_tier_hint="Free prototyping access for evaluation under NVIDIA's API trial terms.",
        card_required=False,
    ),
    "sambanova": AccessInfo(
        console="https://cloud.sambanova.ai/",
        api_key="https://cloud.sambanova.ai/apis",
        docs="https://docs.sambanova.ai/",
        pricing="https://cloud.sambanova.ai/plans",
        rate_limits="https://docs.sambanova.ai/docs/en/models/rate-limits",
        free_tier_hint="Free tier applies while no payment method is linked.",
        card_required=False,
    ),
    "huggingface": AccessInfo(
        signup="https://huggingface.co/join",
        console="https://huggingface.co/settings/inference-providers",
        api_key="https://huggingface.co/settings/tokens",
        docs="https://huggingface.co/docs/inference-providers/index",
        pricing="https://huggingface.co/docs/inference-providers/pricing",
        free_tier_hint="Accounts include a small monthly inference credit; PRO and Team include more.",
        card_required=False,
    ),
    "custom": AccessInfo(free_tier_hint="Your own OpenAI-compatible endpoint (for example a local server)."),
}


def access_for(provider_id: str) -> AccessInfo:
    return ACCESS.get(provider_id, AccessInfo())


def access_path_for(provider_id: str, adapter_key: str, *, oauth_available: bool) -> str:
    if provider_id.startswith("mock"):
        return "anonymous_public"
    if oauth_available:
        return "official_oauth"
    return "api_key"
