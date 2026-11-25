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

import hashlib
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from oasis_route.config import Settings
from oasis_route.db.models import Endpoint, Evidence, Model, Offer, OfferTransition, Provider, utcnow
from oasis_route.db.session import Database
from oasis_route.registry import imported

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


# (id, name, default base URL, website). Ports follow each server's documented default.
LOCAL_SERVERS: tuple[tuple[str, str, str, str], ...] = (
    (
        "ollama",
        "Ollama (local)",
        "http://127.0.0.1:11434/v1",
        "https://docs.ollama.com/api/openai-compatibility",
    ),
    ("llama_cpp", "llama.cpp server", "http://127.0.0.1:8080/v1", "https://github.com/ggml-org/llama.cpp"),
    ("vllm", "vLLM", "http://127.0.0.1:8000/v1", "https://docs.vllm.ai/"),
    ("lm_studio", "LM Studio", "http://127.0.0.1:1234/v1", "https://lmstudio.ai/docs/app/api"),
    ("lemonade", "Lemonade Server", "http://127.0.0.1:13305/api/v1", "https://lemonade-server.ai"),
    ("llamafile", "llamafile", "http://127.0.0.1:8080/v1", "https://github.com/Mozilla-Ocho/llamafile"),
    (
        "docker_model_runner",
        "Docker Model Runner",
        "http://127.0.0.1:12434/engines/v1",
        "https://docs.docker.com/ai/model-runner/",
    ),
    ("xinference", "Xinference", "http://127.0.0.1:9997/v1", "https://inference.readthedocs.io"),
)
LOCAL_SERVER_IDS = frozenset(s[0] for s in LOCAL_SERVERS)

PROVIDERS: tuple[ProviderSeed, ...] = (
    ProviderSeed(
        "openai",
        "OpenAI",
        "openai_compatible",
        "https://api.openai.com/v1",
        "openai_chat",
        ("openai.com", "platform.openai.com", "developers.openai.com"),
        terms_url="https://openai.com/policies/terms-of-use/",
        policy_url="https://openai.com/policies/usage-policies/",
    ),
    ProviderSeed(
        "anthropic",
        "Anthropic",
        "anthropic",
        "https://api.anthropic.com",
        "anthropic_messages",
        ("anthropic.com", "docs.anthropic.com", "platform.claude.com", "claude.com"),
        terms_url="https://www.anthropic.com/legal/commercial-terms",
        policy_url="https://www.anthropic.com/legal/aup",
    ),
    ProviderSeed(
        "gemini",
        "Google Gemini API",
        "openai_compatible",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "openai_chat",
        ("ai.google.dev", "aistudio.google.com", "generativelanguage.googleapis.com"),
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
        ("cerebras.ai", "inference-docs.cerebras.ai", "cloud.cerebras.ai"),
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
        "cohere",
        "Cohere",
        "openai_compatible",
        "https://api.cohere.ai/compatibility/v1",
        "openai_chat",
        ("cohere.com", "cohere.ai", "docs.cohere.com", "dashboard.cohere.com", "api.cohere.com"),
        terms_url="https://cohere.com/terms-of-use",
        notes="Trial (evaluation) keys are free and limited to 1,000 API calls a month and 20 requests a "
        "minute per chat model; production keys are billed (https://docs.cohere.com/docs/rate-limits). "
        "Trial keys are for evaluation, not production use. " + REVIEW_NOTE,
    ),
    ProviderSeed(
        "zai",
        "Z.ai (GLM)",
        "openai_compatible",
        "https://api.z.ai/api/paas/v4",
        "openai_chat",
        ("z.ai", "docs.z.ai", "api.z.ai"),
        terms_url="https://chat.z.ai/legal-agreement/terms-of-service",
        notes="Pay-as-you-go API with several models priced at zero "
        "(https://docs.z.ai/guides/overview/pricing). GLM Coding Plan quota may only be used inside "
        "officially supported coding tools, so Oasis Route connects only standard API keys on the general "
        "endpoint. " + REVIEW_NOTE,
        models=tuple(
            {
                "upstream_id": mid,
                "display_name": name,
                "family": "glm",
                "context_window": ctxw,
                "max_output_tokens": max_out,
                "capabilities": {"streaming": True, "tools": True, "json_mode": False, "vision": vision},
                "pricing": {
                    "free": True,
                    "source": "https://docs.z.ai/guides/overview/pricing",
                    "observed_at": "2026-11-23",
                },
            }
            for mid, name, ctxw, max_out, vision in (
                ("glm-4.7-flash", "GLM-4.7-Flash", 200_000, 128_000, False),
                ("glm-4.5-flash", "GLM-4.5-Flash", 128_000, 96_000, False),
                ("glm-4.6v-flash", "GLM-4.6V-Flash", 128_000, 32_000, True),
            )
        ),
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
    # Local OpenAI-compatible servers (defaults from the OmniRoute catalogue's local-server list). Running on
    # your own hardware they cost nothing, so they are the one "free tier" without a cap. The base URL must
    # stay on a private or loopback address (enforced at connect time) and needs
    # OASIS_ROUTE_ALLOW_PRIVATE_UPSTREAMS=true. Models come from the server's own /v1/models.
    *(
        ProviderSeed(
            pid,
            name,
            "openai_compatible",
            url,
            "openai_chat",
            ("localhost", "127.0.0.1"),
            terms_url=site,
            data_policy={
                "trains_on_inputs": False,
                "zero_retention": True,
                "source": "runs on your own machine; no data leaves it",
            },
            regions=("local",),
            multi_account_permitted=True,
            notes="Local model server. The base URL must be a private or loopback address.",
        )
        for pid, name, url, site in LOCAL_SERVERS
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
            current = await s.get(Provider, seed.id)
            if current is not None:
                # Additive only: providers move documentation to new hosts; never remove operator domains.
                missing = [d for d in seed.official_domains if d not in (current.official_domains or [])]
                if missing:
                    current.official_domains = [*(current.official_domains or []), *missing]
            if current is None:
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
    if settings.catalog_import_enabled:
        inserted += await sync_imported(db)
    return inserted


async def sync_imported(db: Database) -> int:
    """Seed the imported catalogue (registry/imported.py). Additive and idempotent; curated seeds win."""
    src = imported.source()
    curated = {p.id for p in PROVIDERS}
    inserted = 0
    async with db.transaction() as s:
        existing_providers = {p.id: p for p in (await s.execute(select(Provider))).scalars()}
        existing_models = {
            (pid, uid) for pid, uid in (await s.execute(select(Model.provider_id, Model.upstream_id))).all()
        }
        has_endpoint = set((await s.execute(select(Endpoint.provider_id).distinct())).scalars())
        existing_offers = set(
            (await s.execute(select(Offer.dedupe_key).where(Offer.dedupe_key.like("omniroute:%")))).scalars()
        )
        for ip in imported.providers():
            facts = ip.catalog_facts(src)
            prov = existing_providers.get(ip.id)
            if prov is None:
                prov = Provider(
                    id=ip.id,
                    name=ip.name,
                    adapter_key=ip.adapter_key,
                    official_domains=list(ip.raw.get("official_domains") or []),
                    terms_url=None,
                    policy_url=None,
                    status=ip.status,
                    data_policy={},
                    regions=[],
                    min_age=None,
                    allowed_countries=[],
                    blocked_countries=[],
                    multi_account_permitted=False,
                    automated_access_permitted=ip.status == "active",
                    notes=(
                        f"Imported from the OmniRoute catalogue ({ip.status}: {ip.status_reason}). "
                        if ip.status_reason
                        else "Imported from the OmniRoute catalogue. "
                    )
                    + REVIEW_NOTE,
                    catalog=facts,
                )
                s.add(prov)
                existing_providers[ip.id] = prov
                inserted += 1
                await s.flush()
            elif not prov.catalog:
                # Curated providers keep their reviewed facts; only record what the import advertises.
                prov.catalog = {**facts, "curated_override": ip.id in curated}
            if ip.id not in curated and ip.base_url and ip.protocol and ip.id not in has_endpoint:
                s.add(
                    Endpoint(provider_id=ip.id, base_url=ip.base_url, protocol=ip.protocol, is_default=True)
                )
                has_endpoint.add(ip.id)
                inserted += 1
            for m in ip.models:
                key = (ip.id, str(m["upstream_id"])[:200])
                if key in existing_models:
                    continue
                s.add(Model(provider_id=ip.id, **imported.model_row(m, src)))
                existing_models.add(key)
                inserted += 1
            for o in imported.offer_rows(ip, src):
                if o["dedupe_key"] in existing_offers:
                    continue
                evidence_url = o.pop("evidence_url")
                excerpt = o.pop("excerpt")
                now = utcnow()
                offer = Offer(
                    provider_id=ip.id,
                    first_observed_at=now,
                    last_observed_at=now,
                    next_review_at=now,
                    extractor_version="omniroute-1",
                    **o,
                )
                s.add(offer)
                await s.flush()
                s.add(
                    OfferTransition(
                        offer_id=offer.id,
                        from_state=None,
                        to_state=offer.state,
                        actor="system:catalog_import",
                        rationale="Advertised by the imported OmniRoute free-tier research snapshot; "
                        "requires review against the provider's own documentation.",
                        evidence_ids=[],
                    )
                )
                s.add(
                    Evidence(
                        offer_id=offer.id,
                        url=str(evidence_url)[:2000],
                        publisher="OmniRoute catalogue (MIT)",
                        excerpt=excerpt,
                        sha256=hashlib.sha256(excerpt.encode()).hexdigest(),
                        claim_type="free_tier",
                        claim_value={"quota_claims": o["quota_claims"], **o["billing"]},
                        method="catalog_import",
                        confidence=0.2,
                        next_review_at=now,
                    )
                )
                existing_offers.add(o["dedupe_key"])
                inserted += 1
    return inserted
