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
PROFILES = {"chat (1,500 tok/request)": 1_500, "coding agent (20,000 tok/request)": 20_000}


@dataclass(frozen=True)
class Pool:
    provider: str
    model: str
    rpm: float | None = None
    rpd: float | None = None
    tpm: float | None = None
    tpd: float | None = None
    recurring: bool = True
    note: str = ""
    source: str = ""
    checked: str = "2026-11-17"

    def daily(self, s: int) -> float:
        if self.tpm is not None and s > self.tpm:
            return 0.0  # a single request exceeds the per-minute token budget
        caps = [
            self.tpd,
            self.rpd * s if self.rpd is not None else None,
            self.tpm * 1440 if self.tpm is not None else None,
            self.rpm * 1440 * s if self.rpm is not None else None,
        ]
        known = [c for c in caps if c is not None]
        return min(known) if known else math.nan

    def monthly(self, s: int) -> float:
        return self.daily(s) * DAYS_PER_MONTH if self.recurring else 0.0


GROQ = "https://console.groq.com/docs/rate-limits"
OPENROUTER = "https://openrouter.ai/docs/api/reference/limits"
NVIDIA = "https://build.nvidia.com/search"
SAMBANOVA = "https://docs.sambanova.ai/docs/en/models/rate-limits"

# Published, recurring free limits (chat models only). Groq limits are per model per organization.
FLOOR: tuple[Pool, ...] = (
    Pool("Groq", "openai/gpt-oss-120b", rpm=30, rpd=1_000, tpm=8_000, tpd=200_000, source=GROQ),
    Pool("Groq", "openai/gpt-oss-20b", rpm=30, rpd=1_000, tpm=8_000, tpd=200_000, source=GROQ),
    Pool("Groq", "qwen/qwen3.8-27b", rpm=30, rpd=1_000, tpm=8_000, tpd=200_000, source=GROQ),
    # SambaNova Free Tier (no payment method linked), per production model. Preview models are left out
    # because they "may be removed at short notice". Its plans page reads differently; see the doc.
    *(
        Pool("SambaNova", m, rpm=20, rpd=20, tpd=200_000, source=SAMBANOVA)
        for m in ("DeepSeek-V3.1", "Meta-Llama-3.3-70B-Instruct", "gpt-oss-120b")
    ),
    Pool(
        "OpenRouter",
        "all `:free` models (shared)",
        rpm=20,
        rpd=50,
        note="account-wide across free variants, < 10 credits ever purchased",
        source=OPENROUTER,
    ),
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
        "free tier per project; limits shown only in AI Studio",
        "https://ai.google.dev/gemini-api/docs/rate-limits",
    ),
    (
        "Mistral AI",
        "Free mode limits shown only in the Admin Panel; API access may need billing activation",
        "https://docs.mistral.ai/admin/billing-usage/usage-limits",
    ),
    (
        "Cerebras",
        "one-time $5 trial credit that expires after 30 days; no renewing free tier",
        "https://inference-docs.cerebras.ai/support/rate-limits",
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


def scenario(pools: tuple[Pool, ...], s: int) -> float:
    return sum(p.monthly(s) for p in pools)


def main() -> None:
    names = list(PROFILES)
    print("| Pool | Binding limits | " + " | ".join(f"Tokens/month, {n}" for n in names) + " |")
    print("|---|---|" + "---|" * len(names))
    for p in (*FLOOR, OPENROUTER_UNLOCKED, NVIDIA_CEILING):
        limits = ", ".join(
            f"{k.upper()} {fmt(v)}"
            for k, v in (("rpm", p.rpm), ("rpd", p.rpd), ("tpm", p.tpm), ("tpd", p.tpd))
            if v
        )
        cells = " | ".join(fmt(p.monthly(s)) for s in PROFILES.values())
        print(f"| [{p.provider}]({p.source}) {p.model} | {limits} | {cells} |")
    print()
    floor = {n: scenario(FLOOR, s) for n, s in PROFILES.items()}
    unlocked = {
        n: scenario((*(p for p in FLOOR if p.provider != "OpenRouter"), OPENROUTER_UNLOCKED), s)
        for n, s in PROFILES.items()
    }
    ceiling = {n: unlocked[n] + NVIDIA_CEILING.monthly(s) for n, s in PROFILES.items()}
    print("| Scenario | " + " | ".join(names) + " |")
    print("|---|" + "---|" * len(names))
    print(
        "| A. Published recurring floor (Groq + SambaNova + OpenRouter, no purchase) | "
        + " | ".join(fmt(floor[n]) for n in names)
        + " |"
    )
    print("| B. A + OpenRouter 10-credit unlock | " + " | ".join(fmt(unlocked[n]) for n in names) + " |")
    print(
        "| C. B + NVIDIA prototyping ceiling (100% utilisation) | "
        + " | ".join(fmt(ceiling[n]) for n in names)
        + " |"
    )
    print()
    for name, why, url in UNQUANTIFIED:
        print(f"- [{name}]({url}): {why}.")


if __name__ == "__main__":
    main()
