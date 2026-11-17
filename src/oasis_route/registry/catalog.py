"""Built-in provider catalogue.

This is a seed, not a source of truth. ``sync_catalog`` only inserts rows that are missing, so operator edits
made through the admin registry API (data policy, eligibility, quality evidence) are never overwritten.

Facts we cannot verify from a primary source at build time are left **unknown** (``None`` / empty):
- ``data_policy`` keys (``trains_on_inputs``, ``zero_retention``) are unset for real providers unless a
  primary source states them. NVIDIA's API Catalog says inputs are recorded to improve its models, so it is
  seeded as ``trains_on_inputs: True`` with that URL. Projects requiring no-training or zero-retention exclude
  providers with unknown policy until an operator records it with an evidence URL. Unknown never satisfies a
  privacy requirement.
- Model quality is not seeded for real providers. Models discovered through ``list_models`` are *unrated* and
  rank below rated models; operators attach task-specific scores with source/date/methodology/confidence.
- Eligibility values below are taken from each provider's published terms as linked in ``terms_url`` and must
  be re-checked by an operator; ``notes`` says so explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from oasis_route.config import Settings
from oasis_route.db.models import Endpoint, Model, Provider
from oasis_route.db.session import Database

REVIEW_NOTE = (
    "Seeded from the built-in catalogue; an operator must re-verify terms, eligibility, and data policy."
)


@dataclass(frozen=True)
class ProviderSeed:
    id: str
    name: str
    adapter_key: str
    base_url: str
    protocol: str
    official_domains: tuple[str, ...]
    terms_url: str | None = None
    policy_url: str | None = None
    min_age: int | None = None
    multi_account_permitted: bool = False
    data_policy: dict[str, Any] = field(default_factory=dict)
    regions: tuple[str, ...] = ()
    notes: str = REVIEW_NOTE
    models: tuple[dict[str, Any], ...] = ()
    dev_only: bool = False


PROVIDERS: tuple[ProviderSeed, ...] = (
    ProviderSeed(
        "openai",
        "OpenAI",
        "openai_compatible",
        "https://api.openai.com/v1",
        "openai_chat",
        ("openai.com", "platform.openai.com"),
        terms_url="https://openai.com/policies/terms-of-use/",
        policy_url="https://openai.com/policies/usage-policies/",
    ),
    ProviderSeed(
        "anthropic",
        "Anthropic",
        "anthropic",
        "https://api.anthropic.com",
        "anthropic_messages",
        ("anthropic.com", "docs.anthropic.com"),
        terms_url="https://www.anthropic.com/legal/commercial-terms",
        policy_url="https://www.anthropic.com/legal/aup",
    ),
    ProviderSeed(
        "gemini",
        "Google Gemini API",
        "openai_compatible",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "openai_chat",
        ("ai.google.dev", "generativelanguage.googleapis.com"),
        terms_url="https://ai.google.dev/gemini-api/terms",
        min_age=18,
    ),
    ProviderSeed(
        "groq",
        "Groq",
        "openai_compatible",
        "https://api.groq.com/openai/v1",
        "openai_chat",
        ("groq.com", "console.groq.com"),
        terms_url="https://groq.com/terms-of-use/",
    ),
    ProviderSeed(
        "mistral",
        "Mistral AI",
        "openai_compatible",
        "https://api.mistral.ai/v1",
        "openai_chat",
        ("mistral.ai", "docs.mistral.ai"),
        terms_url="https://mistral.ai/terms/",
    ),
    ProviderSeed(
        "openrouter",
        "OpenRouter",
        "openai_compatible",
        "https://openrouter.ai/api/v1",
        "openai_chat",
        ("openrouter.ai",),
        terms_url="https://openrouter.ai/terms",
    ),
    ProviderSeed(
        "cerebras",
        "Cerebras Inference",
        "openai_compatible",
        "https://api.cerebras.ai/v1",
        "openai_chat",
        ("cerebras.ai", "inference-docs.cerebras.ai"),
        terms_url="https://www.cerebras.ai/terms-of-service",
    ),
    ProviderSeed(
        "nvidia",
        "NVIDIA API Catalog (NIM)",
        "openai_compatible",
        "https://integrate.api.nvidia.com/v1",
        "openai_chat",
        ("nvidia.com", "build.nvidia.com", "docs.api.nvidia.com", "integrate.api.nvidia.com"),
        terms_url="https://assets.ngc.nvidia.com/products/api-catalog/legal/NVIDIA%20API%20Trial%20Terms%20of%20Service.pdf",
        min_age=18,
        # build.nvidia.com: "Your input and output will be recorded ... to improve NVIDIA products and
        # services, including AI models". Recorded so no-training / zero-retention projects exclude it.
        data_policy={
            "trains_on_inputs": True,
            "zero_retention": False,
            "source": "https://build.nvidia.com/openai/gpt-oss-20b/deploy",
        },
        notes="Free prototyping access (rate-limited, about 40 RPM for most models). The trial terms "
        "allow internal testing and evaluation only, not production use. " + REVIEW_NOTE,
    ),
    ProviderSeed(
        "sambanova",
        "SambaNova Cloud",
        "openai_compatible",
        "https://api.sambanova.ai/v1",
        "openai_chat",
        ("sambanova.ai", "docs.sambanova.ai", "cloud.sambanova.ai"),
        terms_url="https://sambanova.ai/cloud-end-user-license-agreement",
        notes="Free tier applies while no payment method is linked (per-model RPM, RPD and TPD limits, "
        "https://docs.sambanova.ai/docs/en/models/rate-limits). " + REVIEW_NOTE,
    ),
    ProviderSeed(
        "huggingface",
        "Hugging Face Inference Providers",
        "openai_compatible",
        "https://router.huggingface.co/v1",
        "openai_chat",
        ("huggingface.co", "router.huggingface.co"),
        terms_url="https://huggingface.co/terms-of-service",
        notes="OpenAI-compatible router for chat models. Billed against Hugging Face credits; PRO and "
        "Team accounts include monthly credits (https://huggingface.co/docs/inference-providers/pricing). "
        + REVIEW_NOTE,
    ),
    ProviderSeed(
        "custom",
        "Custom OpenAI-compatible endpoint",
        "openai_compatible",
        "",
        "openai_chat",
        (),
        notes="User-supplied base URL. Private-network URLs require "
        "OASIS_ROUTE_ALLOW_PRIVATE_UPSTREAMS=true.",
    ),
    # Deterministic local providers used by `oasis-route dev`, demos, and tests. Never seeded in production.
    *(
        ProviderSeed(
            pid,
            name,
            "openai_compatible",
            "http://127.0.0.1:9100/v1",
            "openai_chat",
            ("127.0.0.1",),
            data_policy={"trains_on_inputs": False, "zero_retention": True, "source": "local mock"},
            regions=("local",),
            notes="Local deterministic mock provider (development only).",
            dev_only=True,
            multi_account_permitted=True,
            models=tuple(
                {
                    "upstream_id": mid,
                    "display_name": mid,
                    "family": "mock",
                    "context_window": ctxw,
                    "max_output_tokens": 4096,
                    "capabilities": {"streaming": True, "tools": True, "json_mode": True, "vision": False},
                    "quality": {
                        "general": {
                            "score": score,
                            "source": "synthetic test fixture",
                            "as_of": "2026-01-01",
                            "methodology": "fixed ordering for deterministic routing tests",
                            "confidence": 1.0,
                        }
                    },
                }
                for mid, ctxw, score in models
            ),
        )
        for pid, name, models in (
            (
                "mock",
                "Mock Provider",
                (("mock-large", 128_000, 90.0), ("mock-medium", 32_000, 70.0), ("mock-small", 8_000, 50.0)),
            ),
            (
                "mock_alt",
                "Mock Provider (alternate)",
                (("mock-large", 128_000, 90.0), ("mock-medium", 32_000, 70.0)),
            ),
        )
    ),
)


def get_seed(provider_id: str) -> ProviderSeed | None:
    return next((p for p in PROVIDERS if p.id == provider_id), None)


async def sync_catalog(db: Database, settings: Settings) -> int:
    """Insert missing providers, default endpoints, and seed models. Returns the number of rows inserted."""
    inserted = 0
    async with db.transaction() as s:
        for seed in PROVIDERS:
            if seed.dev_only and not settings.mock_providers_enabled:
                continue
            if await s.get(Provider, seed.id) is None:
                s.add(
                    Provider(
                        id=seed.id,
                        name=seed.name,
                        adapter_key=seed.adapter_key,
                        official_domains=list(seed.official_domains),
                        terms_url=seed.terms_url,
                        policy_url=seed.policy_url,
                        status="active",
                        data_policy=dict(seed.data_policy),
                        regions=list(seed.regions),
                        min_age=seed.min_age,
                        allowed_countries=[],
                        blocked_countries=[],
                        multi_account_permitted=seed.multi_account_permitted,
                        automated_access_permitted=True,
                        notes=seed.notes,
                    )
                )
                inserted += 1
                await s.flush()
            if seed.base_url:
                has_ep = (
                    await s.execute(select(Endpoint.id).where(Endpoint.provider_id == seed.id).limit(1))
                ).first()
                if has_ep is None:
                    s.add(
                        Endpoint(
                            provider_id=seed.id,
                            base_url=seed.base_url,
                            protocol=seed.protocol,
                            is_default=True,
                        )
                    )
                    inserted += 1
            for m in seed.models:
                exists = (
                    await s.execute(
                        select(Model.id).where(
                            Model.provider_id == seed.id, Model.upstream_id == m["upstream_id"]
                        )
                    )
                ).first()
                if exists is None:
                    s.add(Model(provider_id=seed.id, lifecycle="active", aliases=[], **m))
                    inserted += 1
    return inserted
