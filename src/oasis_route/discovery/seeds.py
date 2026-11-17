"""Built-in, maintainer-reviewed discovery sources.

Each seed is a public, unauthenticated page or JSON document published by the provider itself (or the
provider's own public catalogue API) describing pricing, free tiers, rate limits, or available models. The
maintainers reviewed these for automated, low-frequency access on 2026-11-17: robots.txt allowed the paths
for generic user agents and none require login. That review is recorded in ``terms_notes``; robots.txt is
still re-checked before **every** fetch, and per-origin rate limits apply, so a provider can withdraw
permission at any time and discovery will stop.

Seeds are inserted only if missing (``sync_seed_sources``); an admin who blocks or edits a seeded source
keeps that decision. Disable all seeds with ``OASIS_ROUTE_DISCOVERY_SEED_SOURCES=false``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import select

from oasis_route.db.models import Provider, Source, utcnow
from oasis_route.db.session import Database
from oasis_route.discovery.urls import canonicalize
from oasis_route.domain.enums import SourceClass, SourceReviewStatus

log = logging.getLogger(__name__)

REVIEWED_ON = "2026-11-17"
DAY = 86_400


@dataclass(frozen=True)
class SeedSource:
    provider_id: str
    url: str
    source_class: SourceClass = SourceClass.OFFICIAL_DOCS
    crawl_interval_s: int = DAY
    purpose: str = "rate limits"


SEEDS: tuple[SeedSource, ...] = (
    # Structured public catalogues (JSON).
    SeedSource(
        "openrouter", "https://openrouter.ai/api/v1/models", SourceClass.MODEL_CATALOGUE, 6 * 3600, "models"
    ),
    SeedSource(
        "huggingface",
        "https://router.huggingface.co/v1/models",
        SourceClass.MODEL_CATALOGUE,
        DAY,
        "providers",
    ),
    # Official documentation pages.
    SeedSource("openrouter", "https://openrouter.ai/docs/api-reference/limits"),
    SeedSource("groq", "https://console.groq.com/docs/rate-limits"),
    SeedSource("groq", "https://console.groq.com/docs/models", purpose="models"),
    SeedSource("gemini", "https://ai.google.dev/gemini-api/docs/rate-limits"),
    SeedSource("gemini", "https://aistudio.google.com/docs/pricing", purpose="pricing"),
    SeedSource("cerebras", "https://inference-docs.cerebras.ai/support/rate-limits"),
    SeedSource("cerebras", "https://inference-docs.cerebras.ai/models/overview", purpose="models"),
    SeedSource("sambanova", "https://docs.sambanova.ai/docs/en/models/rate-limits"),
    SeedSource("sambanova", "https://cloud.sambanova.ai/plans", purpose="pricing"),
    SeedSource("huggingface", "https://huggingface.co/docs/inference-providers/pricing", purpose="pricing"),
    SeedSource("mistral", "https://docs.mistral.ai/admin/billing-usage/usage-limits"),
    SeedSource("mistral", "https://mistral.ai/pricing", purpose="pricing"),
    SeedSource("nvidia", "https://docs.api.nvidia.com/nim/reference/llm-apis", purpose="models"),
    SeedSource("openai", "https://developers.openai.com/api/docs/guides/rate-limits"),
    SeedSource("anthropic", "https://platform.claude.com/docs/en/api/rate-limits"),
)


def terms_note(seed: SeedSource) -> str:
    return (
        f"Built-in seed ({seed.purpose}) reviewed by the Oasis Route maintainers on {REVIEWED_ON}: public "
        "page published by the provider, no login required, robots.txt permitted generic agents at review "
        "time. robots.txt is re-checked before every fetch and per-origin rate limits apply."
    )


async def sync_seed_sources(db: Database) -> int:
    """Insert missing seed sources as approved. Never modifies existing rows. Returns rows inserted."""
    inserted = 0
    now = utcnow()
    async with db.transaction() as s:
        providers = {p.id for p in (await s.execute(select(Provider))).scalars()}
        existing = set((await s.execute(select(Source.url))).scalars())
        for seed in SEEDS:
            if seed.provider_id not in providers:
                continue
            url = canonicalize(seed.url)
            if url in existing:
                continue
            s.add(
                Source(
                    url=url,
                    domain=(urlsplit(url).hostname or "").lower(),
                    provider_id=seed.provider_id,
                    source_class=seed.source_class.value,
                    review_status=SourceReviewStatus.APPROVED.value,
                    terms_notes=terms_note(seed),
                    url_scope=None,
                    crawl_interval_s=seed.crawl_interval_s,
                    min_interval_per_domain_s=10.0,
                    origin="seed",
                    depth=0,
                    next_fetch_at=now,
                    submitted_by=None,
                )
            )
            existing.add(url)
            inserted += 1
    if inserted:
        log.info("discovery seed sources inserted", extra={"count": inserted})
    return inserted
