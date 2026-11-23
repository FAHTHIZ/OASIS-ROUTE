#!/usr/bin/env python3
"""Reproducible monthly token-capacity estimate for free inference tiers Oasis Route can route to.

Only limits a provider *publishes* are used. Each provider counts once (one account per person, as every
provider's terms require; Oasis Route never farms accounts). Run ``python scripts/free_tier_capacity.py`` to
print the table in docs/FREE_TIER_CAPACITY.md. Update ``TIERS`` (with the source URL and check date) when a
provider changes its limits.

For one pool the binding daily capacity is

    tokens/day = min(TPD, RPD * S, TPM * 1440, RPM * 1440 * S)

where S is tokens per request (input + output). A request larger than TPM can never be served. Monthly
capacity multiplies by 365.25 / 12 = 30.44 days.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DAYS_PER_MONTH = 365.25 / 12
CHECKED = "2026-11-23"


@dataclass(frozen=True)
class Profile:
    name: str
    input_tokens: int
    output_tokens: int

    @property
    def size(self) -> int:
        return self.input_tokens + self.output_tokens


PROFILES = (
    Profile("chat (1,500 tok/request)", 1_200, 300),
    Profile("coding agent (20,000 tok/request)", 18_000, 2_000),
)


@dataclass(frozen=True)
class Pool:
    provider: str
    model: str
    rpm: float | None = None
    rpd: float | None = None
    tpm: float | None = None
    tpd: float | None = None
    requests_per_month: float | None = None
    # Budget-metered tiers (Cloudflare Workers AI): units per day and units per million input/output tokens.
    units_per_day: float | None = None
    units_per_m_in: float | None = None
    units_per_m_out: float | None = None
    recurring: bool = True
    note: str = ""
    source: str = ""
    checked: str = CHECKED

    def daily(self, p: Profile) -> float:
        s = p.size
        if self.tpm is not None and s > self.tpm:
            return 0.0  # a single request exceeds the per-minute token budget
        caps = [
            self.tpd,
            self.rpd * s if self.rpd is not None else None,
            self.tpm * 1440 if self.tpm is not None else None,
            self.rpm * 1440 * s if self.rpm is not None else None,
            self.requests_per_month * s / DAYS_PER_MONTH if self.requests_per_month is not None else None,
        ]
        if (
            self.units_per_day is not None
            and self.units_per_m_in is not None
            and self.units_per_m_out is not None
        ):
            per_request = (
                p.input_tokens * self.units_per_m_in + p.output_tokens * self.units_per_m_out
            ) / 1e6
            caps.append(self.units_per_day / per_request * s)
        known = [c for c in caps if c is not None]
        return min(known) if known else math.nan

    def monthly(self, p: Profile) -> float:
        return self.daily(p) * DAYS_PER_MONTH if self.recurring else 0.0

    def limits(self) -> str:
        parts = [
            f"{k} {fmt(v)}"
            for k, v in (
                ("RPM", self.rpm),
                ("RPD", self.rpd),
                ("TPM", self.tpm),
                ("TPD", self.tpd),
                ("req/month", self.requests_per_month),
                ("neurons/day", self.units_per_day),
            )
            if v
        ]
        return ", ".join(parts)


GROQ = "https://console.groq.com/docs/rate-limits"
OPENROUTER = "https://openrouter.ai/docs/api/reference/limits"
NVIDIA = "https://build.nvidia.com/search"
SAMBANOVA = "https://docs.sambanova.ai/docs/en/models/rate-limits"
COHERE = "https://docs.cohere.com/docs/rate-limits"
CLOUDFLARE = "https://developers.cloudflare.com/workers-ai/platform/pricing/"

# Published, recurring free limits (chat models only). Groq limits are per model per organization
# (Free Plan tab of the rate-limits page).
FLOOR: tuple[Pool, ...] = (
    *(
        Pool("Groq", m, rpm=30, rpd=1_000, tpm=8_000, tpd=200_000, source=GROQ)
        for m in ("openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b")
    ),
    # SambaNova Free Tier (no payment method linked). The page (modified 2026-11-20) now lists two models.
    *(
        Pool("SambaNova", m, rpm=20, rpd=20, tpd=200_000, source=SAMBANOVA)
        for m in ("gpt-oss-120b", "gemma-4-31B-it")
    ),
    Pool(
        "OpenRouter",
        "all `:free` models (shared)",
        rpm=20,
        rpd=50,
        note="account-wide across free variants, < 10 credits ever purchased",
        source=OPENROUTER,
    ),
    # Cohere trial keys: 20 req/min per Command model and 1,000 API calls a month in total.
    Pool(
        "Cohere",
        "Command models, trial key (shared)",
        rpm=20,
        requests_per_month=1_000,
        note="trial keys are for evaluation, not production",
        source=COHERE,
    ),
)
# Reachable through a custom OpenAI-compatible connection: Cloudflare Workers AI's daily neuron budget.
CLOUDFLARE_POOL = Pool(
    "Cloudflare Workers AI",
    "`@cf/openai/gpt-oss-120b` (custom endpoint)",
    units_per_day=10_000,
    units_per_m_in=31_818,
    units_per_m_out=68_182,
    note="10,000 free neurons a day, shared by every Workers AI model on the account",
    source=CLOUDFLARE,
)
# OpenRouter raises the shared free-variant cap to 1,000 requests/day once 10 credits have ever been bought.
OPENROUTER_UNLOCKED = Pool(
    "OpenRouter",
    "all `:free` models (shared)",
    rpm=20,
    rpd=1_000,
    note="after a one-time purchase of 10 credits",
    source=OPENROUTER,
)
# NVIDIA publishes only a per-minute request limit and describes the API as free *to prototype with*.
NVIDIA_CEILING = Pool(
    "NVIDIA build.nvidia.com",
    "most hosted models",
    rpm=40,
    note="prototyping use; no published daily cap, so this is a theoretical ceiling at 100% utilisation",
    source=NVIDIA,
)
# Providers whose free tier exists but whose numbers are not published, or which is not recurring.
UNQUANTIFIED = (
    (
        "Google Gemini API",
        "free tier per project; per-model limits are shown only in AI Studio",
        "https://aistudio.google.com/docs/rate-limits",
    ),
    (
        "Z.ai",
        "GLM-4.7-Flash, GLM-4.5-Flash and GLM-4.6V-Flash are priced at $0, but no rate limits are published",
        "https://docs.z.ai/guides/overview/pricing",
    ),
    (
        "Cerebras",
        "one-time $5 trial credit after adding a payment method, expiring after 30 days; "
        "no renewing free tier",
        "https://inference-docs.cerebras.ai/support/rate-limits",
    ),
    (
        "Mistral AI",
        "Free mode no longer includes API access; API keys need billing activation",
        "https://docs.mistral.ai/admin/billing-usage/usage-limits",
    ),
    (
        "Hugging Face Inference Providers",
        "free users get no monthly credits; PRO includes $2.00 a month",
        "https://huggingface.co/docs/inference-providers/pricing",
    ),
    (
        "GitHub Models",
        "retired on 2026-07-30",
        "https://docs.github.com/en/github-models/use-github-models/prototyping-with-ai-models",
    ),
)


def fmt(n: float) -> str:
    if math.isnan(n):
        return "unknown"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= div:
            return f"{n / div:,.2f}{unit}"
    return f"{n:,.0f}"


def scenario(pools: tuple[Pool, ...], p: Profile) -> float:
    return sum(pool.monthly(p) for pool in pools)


def scenarios() -> list[tuple[str, tuple[Pool, ...]]]:
    a = FLOOR
    b = (*a, CLOUDFLARE_POOL)
    c = (*(x for x in b if x.provider != "OpenRouter"), OPENROUTER_UNLOCKED)
    d = (*c, NVIDIA_CEILING)
    return [
        ("A. Published recurring floor (Groq + SambaNova + OpenRouter + Cohere, no purchase)", a),
        ("B. A + Cloudflare Workers AI through a custom endpoint", b),
        ("C. B + OpenRouter 10-credit unlock (one-time purchase)", c),
        ("D. C + NVIDIA prototyping ceiling (100% utilisation)", d),
    ]


def main() -> None:
    names = [p.name for p in PROFILES]
    print(f"Limits checked on {CHECKED}.")
    print()
    print("| Pool | Binding limits | " + " | ".join(f"Tokens/month, {n}" for n in names) + " |")
    print("|---|---|" + "---|" * len(names))
    for pool in (*FLOOR, CLOUDFLARE_POOL, OPENROUTER_UNLOCKED, NVIDIA_CEILING):
        cells = " | ".join(fmt(pool.monthly(p)) for p in PROFILES)
        print(f"| [{pool.provider}]({pool.source}) {pool.model} | {pool.limits()} | {cells} |")
    print()
    print("| Scenario | " + " | ".join(names) + " |")
    print("|---|" + "---|" * len(names))
    for label, pools in scenarios():
        print(f"| {label} | " + " | ".join(fmt(scenario(pools, p)) for p in PROFILES) + " |")
    print()
    for name, why, url in UNQUANTIFIED:
        print(f"- [{name}]({url}): {why}.")


if __name__ == "__main__":
    main()
