#!/usr/bin/env python3
"""Whole-ecosystem estimate: every free LLM tier anyone lists, counted once per account.

``free_tier_capacity.py`` counts only limits a provider publishes itself. This script answers the bigger
question - "if I signed up once at *every* free tier that other gateways list, how many tokens a month is
that?" - and puts the numbers other projects advertise next to it.

Inputs, in order of trust:

1. ``free_tier_capacity`` pools (official provider pages, checked by hand).
2. The public FreeLLMAPI catalog snapshot (``https://api.freellmapi.co/v1/latest``, version below), which
   records per-model limits and budget labels for 52 providers.
3. OmniRoute's ``docs/reference/FREE_TIERS.md`` for providers only OmniRoute lists.

Rules that make the total honest:

- **One account per provider.** No account pooling, ever.
- **Shared pools count once.** When a provider says a quota is shared across models (Cloudflare neurons,
  "200 requests/day shared", "500K tokens/day"), it is one pool, not one pool per model. FreeLLMAPI adds the
  per-model label of every model, which is the main reason its headline is so large.
- **Request caps become tokens** at the same two request sizes as ``free_tier_capacity`` (1,500 tokens for
  chat, 20,000 for a coding agent). A budget label from the catalog is applied as an extra monthly cap.
- **Credits become tokens** at ``CREDIT_USD_PER_M`` (a blended input+output price for mid-size open models).
- **Rate-limit-only tiers** (a per-minute limit and nothing else) are a 24/7 theoretical ceiling and are
  reported separately, never in the headline.
- **Disputed tiers** (a gateway lists it, the provider's own docs say otherwise) are excluded and listed.

Run ``python scripts/ecosystem_capacity.py`` to print the tables in docs/FREE_TIER_CAPACITY.md.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from free_tier_capacity import (
    CLOUDFLARE_POOL,
    DAYS_PER_MONTH,
    FLOOR,
    NVIDIA_CEILING,
    OPENROUTER_UNLOCKED,
    PROFILES,
    Pool,
    Profile,
    fmt,
)

CHECKED = "2026-11-23"
CATALOG = "https://api.freellmapi.co/v1/latest"
CATALOG_VERSION = "2026.11.23"
CATALOG_PAGE = "https://freellmapi.co/models.html"
OMNIROUTE = "https://github.com/cheaper-inference/OmniRoute/blob/main/docs/reference/FREE_TIERS.md"
FREELLMAPI = "https://github.com/tashfeenahmed/freellmapi"
CREDIT_USD_PER_M = 1.00

FIRST_PARTY = "first-party"
GATEWAY = "gateway"


@dataclass(frozen=True)
class Entry:
    """One account-level pool. ``monthly_cap`` (tokens) is an extra cap from a budget label or credits."""

    name: str
    kind: str
    pool: Pool
    monthly_cap: float | None = None
    count: int = 1
    basis: str = ""
    source: str = CATALOG

    def monthly(self, p: Profile) -> float:
        one = self.pool.monthly(p)
        if self.monthly_cap is not None:
            one = min(one, self.monthly_cap)
        return one * self.count


def monthly_tokens(n: float) -> Pool:
    """A pool whose only limit is a fixed number of tokens a month."""
    return Pool("", "", tpd=n / DAYS_PER_MONTH)


def credits(usd_per_month: float) -> float:
    return round(usd_per_month / CREDIT_USD_PER_M * 1e6, -3)


def verified(pool: Pool) -> Entry:
    return Entry(
        f"{pool.provider} - {pool.model}", FIRST_PARTY, pool, basis=pool.limits(), source=pool.source
    )


ENTRIES: tuple[Entry, ...] = (
    # 1. Official limits, from free_tier_capacity.py.
    *(verified(p) for p in FLOOR),
    verified(CLOUDFLARE_POOL),
    # 2. First-party tiers listed in the FreeLLMAPI catalog but not on an official limits page.
    Entry(
        "Groq - allam-2-7b",
        FIRST_PARTY,
        Pool("Groq", "allam-2-7b", rpm=30, rpd=1_000, tpm=6_000, tpd=500_000),
        basis="30 RPM, 1K RPD, 6K TPM, 500K TPD",
    ),
    Entry(
        "Google Gemini - Flash and Flash-Lite models",
        FIRST_PARTY,
        Pool("Google", "Gemini Flash", rpm=10, rpd=20, tpm=250_000),
        monthly_cap=3e6,
        count=12,
        basis="12 models (aliases and Live-only models dropped), each 20 RPD, label ~3M",
    ),
    Entry(
        "Google Gemini API - Gemma 4 (31B, 26B-A4B)",
        FIRST_PARTY,
        Pool("Google", "Gemma 4", rpm=15, rpd=1_000, tpm=250_000),
        monthly_cap=30e6,
        count=2,
        basis="each 15 RPM, 1K RPD, label ~30M",
    ),
    Entry(
        "Z.ai - GLM-4.7/4.5/4.6V-Flash",
        FIRST_PARTY,
        Pool("Z.ai", "GLM Flash", tpd=1_000_000),
        count=3,
        basis="$0 models; catalog lists 1M tokens/day each",
    ),
    Entry(
        "IteraCompute - qwen3.8-27b",
        FIRST_PARTY,
        monthly_tokens(30e6),
        basis="30M tokens/month per account",
    ),
    Entry(
        "ModelScope (Alibaba) - Qwen, DeepSeek, GLM",
        FIRST_PARTY,
        Pool("ModelScope", "", rpd=200),
        basis="200 daily credits shared, non-commercial",
    ),
    Entry(
        "Aion Labs - 4 models",
        FIRST_PARTY,
        Pool("Aion", "", rpm=15, tpm=20_000, tpd=20_000),
        count=4,
        basis="20K tokens/day each",
    ),
    # 3. Gateways, proxies and community routes (they resell or relay other providers' capacity).
    Entry(
        "DreamPrompting", GATEWAY, Pool("", "", rpd=5_000, tpd=500_000), basis="500K tokens/day per account"
    ),
    Entry("xKiro", GATEWAY, Pool("", "", tpd=1_000_000), basis="500K tokens/day, 1M with Telegram"),
    Entry("Api.Airforce", GATEWAY, Pool("", "", rpm=1, rpd=1_000), basis="1,000 requests/day, 1/min"),
    Entry("Requesty", GATEWAY, Pool("", "", rpd=200), basis="200 requests/day shared"),
    Entry("Routeway", GATEWAY, Pool("", "", rpm=5, rpd=200), basis="200 requests/day shared"),
    Entry("BazaarLink", GATEWAY, Pool("", "", rpm=10, rpd=50), basis="50 requests/day shared"),
    Entry(
        "Inferbase",
        GATEWAY,
        Pool("", "", rpd=100, requests_per_month=5_000),
        basis="5,000 requests/month, 100/day",
    ),
    Entry("Gizmo", GATEWAY, Pool("", "", requests_per_month=1_000), basis="1,000 requests/month shared"),
    Entry("Blaze", GATEWAY, Pool("", "", tpd=200_000), basis="200K tokens/day shared"),
    Entry("LLM7", GATEWAY, Pool("", "", rpm=40, tpd=100_000), basis="100K tokens/24h shared"),
    Entry(
        "Aclide",
        GATEWAY,
        monthly_tokens(credits(23)),
        basis=f"EUR 20 (~$23)/month credits at ${CREDIT_USD_PER_M:.2f}/M",
    ),
    Entry(
        "Electron Hub",
        GATEWAY,
        monthly_tokens(credits(0.25 * 52 / 12)),
        basis=f"$0.25/week credits at ${CREDIT_USD_PER_M:.2f}/M",
    ),
    Entry("Router9", GATEWAY, monthly_tokens(credits(1)), basis="10,000 credits (~$1)/month"),
    # 4. Listed only by OmniRoute (its own estimates, not independently checked).
    Entry("NaraRouter", GATEWAY, monthly_tokens(210e6), basis="OmniRoute estimate", source=OMNIROUTE),
    Entry("Bluesminds", GATEWAY, monthly_tokens(7e6), basis="OmniRoute estimate", source=OMNIROUTE),
    Entry("Arcee", FIRST_PARTY, monthly_tokens(5e6), basis="OmniRoute estimate", source=OMNIROUTE),
    Entry("HuggingChat", GATEWAY, monthly_tokens(0.5e6), basis="OmniRoute estimate", source=OMNIROUTE),
    Entry("Morph", FIRST_PARTY, monthly_tokens(0.4e6), basis="OmniRoute estimate", source=OMNIROUTE),
)

# One-time unlock: OpenRouter's free cap rises to 1,000 requests/day after 10 credits are ever bought.
UNLOCK = verified(OPENROUTER_UNLOCKED)

# Per-minute limit and nothing else: theoretical, at 100% utilisation around the clock.
CEILING: tuple[Entry, ...] = (
    verified(NVIDIA_CEILING),
    Entry("OVH AI Endpoints", FIRST_PARTY, Pool("", "", rpm=2), basis="2 requests/min per IP"),
    Entry("AI Singapore SEA-LION", FIRST_PARTY, Pool("", "", rpm=10), basis="10 RPM"),
    Entry("Kilo", GATEWAY, Pool("", "", rpm=200 / 60), basis="200 requests/hour per IP"),
    Entry("UnoRouter", GATEWAY, Pool("", "", rpm=1), count=2, basis="1 request/min per model"),
)

# Listed by a gateway, contradicted by the provider. Value = what the gateway implies.
DISPUTED: tuple[tuple[str, str, float, str], ...] = (
    (
        "Mistral AI (FreeLLMAPI: $10/month API credits; OmniRoute: ~1B tokens/month)",
        "Mistral's usage-limits page says Free mode doesn't include API access",
        1e9,
        "https://docs.mistral.ai/admin/billing-usage/usage-limits",
    ),
)

UNQUANTIFIED = (
    "AI Horde (volunteer queue)",
    "Waterfall",
    "Septor",
    "Clod",
    "Radeon",
    "SimpleLLM",
    "PlugSky",
    "LogFare",
    "LLMTR",
    "BlockRun",
    "Agnes AI",
    "Pollinations",
    "Typhoon",
    "Cerebras (one-time $5)",
    "Hugging Face (no monthly credits)",
)

# What other projects advertise, for comparison.
CLAIMS: tuple[tuple[str, float, str, str], ...] = (
    (
        "FreeLLMAPI README (live paid catalog, 87 providers, 1,313 endpoints)",
        10.1e9,
        "sum of each model's own budget label, shared pools counted once per model",
        FREELLMAPI,
    ),
    (
        "FreeLLMAPI public catalog snapshot, same method (52 providers, 448 chat models)",
        1.47e9,
        "same method on the free monthly snapshot",
        CATALOG_PAGE,
    ),
    (
        "OmniRoute FREE_TIERS.md",
        1.62e9,
        "about 1B of it is Mistral, which has no free API access",
        OMNIROUTE,
    ),
)


def total(entries: tuple[Entry, ...], p: Profile) -> float:
    return sum(e.monthly(p) for e in entries)


def headline() -> dict[str, float]:
    """The numbers on the README card."""
    chat, agent = PROFILES
    first = tuple(e for e in ENTRIES if e.kind == FIRST_PARTY)
    swapped = (*(e for e in ENTRIES if not e.name.startswith("OpenRouter")), UNLOCK)
    return {
        "verified_chat": total(tuple(verified(p) for p in (*FLOOR, CLOUDFLARE_POOL)), chat),
        "verified_agent": total(tuple(verified(p) for p in (*FLOOR, CLOUDFLARE_POOL)), agent),
        "first_party_chat": total(first, chat),
        "first_party_agent": total(first, agent),
        "all_chat": total(ENTRIES, chat),
        "all_agent": total(ENTRIES, agent),
        "unlock_chat": total(swapped, chat),
        "unlock_agent": total(swapped, agent),
        "ceiling_chat": total(swapped + CEILING, chat),
        "ceiling_agent": total(swapped + CEILING, agent),
        "providers": len({e.name.split(" - ")[0].split(" (")[0] for e in ENTRIES}),
    }


def main() -> None:
    names = [p.name for p in PROFILES]
    print(
        f"Checked {CHECKED}; FreeLLMAPI catalog {CATALOG_VERSION}; "
        f"credits at ${CREDIT_USD_PER_M:.2f}/M tokens."
    )
    print()
    print("| Pool (one account) | Kind | Basis | " + " | ".join(names) + " |")
    print("|---|---|---|" + "---|" * len(names))
    for e in sorted(ENTRIES, key=lambda e: -e.monthly(PROFILES[1])):
        cells = " | ".join(fmt(e.monthly(p)) for p in PROFILES)
        print(f"| [{e.name}]({e.source}) | {e.kind} | {e.basis} | {cells} |")
    print()
    h = headline()
    rows = (
        ("Verified official limits, no purchase", "verified"),
        ("Every first-party free tier", "first_party"),
        ("Everything listed, one account each, recurring", "all"),
        ("... plus OpenRouter 10-credit unlock (one-time)", "unlock"),
        ("... plus rate-limit-only tiers at 100% utilisation (theoretical)", "ceiling"),
    )
    print("| Total | " + " | ".join(names) + " |")
    print("|---|" + "---|" * len(names))
    for label, key in rows:
        print(f"| {label} | {fmt(h[key + '_chat'])} | {fmt(h[key + '_agent'])} |")
    print()
    print("| Rate-limit-only pool | Basis | " + " | ".join(names) + " |")
    print("|---|---|" + "---|" * len(names))
    for e in CEILING:
        print(
            f"| [{e.name}]({e.source}) | {e.basis} | "
            + " | ".join(fmt(e.monthly(p)) for p in PROFILES)
            + " |"
        )
    print()
    for label, value, why, url in CLAIMS:
        print(f"- [{label}]({url}): {fmt(value)} - {why}.")
    for label, why, value, url in DISPUTED:
        print(f"- Disputed, excluded: {label} - {why} ([source]({url})); gateway figure {fmt(value)}.")
    print("- Unquantified (no published cap): " + ", ".join(UNQUANTIFIED) + ".")


if __name__ == "__main__":
    main()
