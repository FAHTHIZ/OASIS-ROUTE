# Free-tier token capacity: the maths

How many tokens per month can one person route through Oasis Route using only free tiers, legitimately? This page gives a reproducible answer from **published** limits. Regenerate the tables with:

```bash
python scripts/free_tier_capacity.py
```

Limits were checked on 2026-11-17. Providers change free tiers often. Oasis Route does not rely on these numbers at runtime: it learns real limits from response headers and `429`s, and reports them through `GET /api/v1/projects/{id}/capacity`.

## Rules for the count

1. **One account per provider.** Every provider's terms limit free tiers to one account per person. Oasis Route refuses multi-account pooling unless a provider explicitly permits it, so the count never multiplies by accounts.
2. **Published numbers only.** If a provider shows limits only inside its console, that tier counts as *unknown*, not zero and not a guess.
3. **Recurring only.** One-time trial credits are listed separately and add nothing per month.
4. **The binding limit wins.** For each quota pool:

```math
\text{tokens/day} = \min\left(\text{TPD},\ \text{RPD}\cdot S,\ \text{TPM}\cdot 1440,\ \text{RPM}\cdot 1440\cdot S\right),
\qquad
\text{tokens/month} = \text{tokens/day}\cdot \tfrac{365.25}{12}
```

where `S` is tokens per request (input plus output). If `S > TPM` the pool can never serve that request, so it contributes 0.

Two request profiles are shown: a **chat** turn (`S = 1,500`) and a **coding-agent** turn (`S = 20,000`). Agents such as Claude Code resend large contexts on every call.

## Per pool

| Pool | Binding limits | Tokens/month, chat (1,500 tok/request) | Tokens/month, coding agent (20,000 tok/request) |
|---|---|---|---|
| [Groq](https://console.groq.com/docs/rate-limits) openai/gpt-oss-120b | RPM 30, RPD 1.00K, TPM 8.00K, TPD 200.00K | 6.09M | 0 |
| [Groq](https://console.groq.com/docs/rate-limits) openai/gpt-oss-20b | RPM 30, RPD 1.00K, TPM 8.00K, TPD 200.00K | 6.09M | 0 |
| [Groq](https://console.groq.com/docs/rate-limits) qwen/qwen3.8-27b | RPM 30, RPD 1.00K, TPM 8.00K, TPD 200.00K | 6.09M | 0 |
| [OpenRouter](https://openrouter.ai/docs/api/reference/limits) all `:free` models (shared) | RPM 20, RPD 50 | 2.28M | 30.44M |
| [OpenRouter](https://openrouter.ai/docs/api/reference/limits) all `:free` models (shared) | RPM 20, RPD 1.00K | 45.66M | 608.75M |
| [NVIDIA build.nvidia.com](https://build.nvidia.com/search) most hosted models | RPM 40 | 2.63B | 35.06B |

Worked example (Groq `openai/gpt-oss-120b`, chat profile): `min(200,000, 1,000 × 1,500, 8,000 × 1,440, 30 × 1,440 × 1,500) = 200,000` tokens/day, times 30.44 = 6.09M tokens/month. For the agent profile a single 20,000-token request is larger than the 8,000-token-per-minute budget, so Groq's free tier cannot serve it at all.

## Totals

| Scenario | chat (1,500 tok/request) | coding agent (20,000 tok/request) |
|---|---|---|
| A. Published recurring floor (Groq + OpenRouter, no purchase) | 20.55M | 30.44M |
| B. A + OpenRouter 10-credit unlock | 63.92M | 608.75M |
| C. B + NVIDIA prototyping ceiling (100% utilisation) | 2.69B | 35.67B |

How to read this:

- **Scenario A, about 20-30 million tokens a month**, is the honest guaranteed floor from published, recurring, no-purchase limits.
- **Scenario B** adds a one-time purchase of 10 OpenRouter credits, which raises the shared free-model cap from 50 to 1,000 requests a day. The credits are only spent if you call paid models. Request-capped pools favour large requests, so agent-style traffic reaches about 0.6 billion tokens a month.
- **Scenario C** adds NVIDIA's published 40 requests per minute at 100% utilisation, around the clock. That is a mathematical ceiling, not a forecast, and NVIDIA describes the API as free *to prototype with*, so check its terms before routing production traffic there.

Unquantified free tiers: they add capacity, but their size is not published.

- [Google Gemini API](https://ai.google.dev/gemini-api/docs/rate-limits): free tier per project; limits shown only in AI Studio.
- [Mistral AI](https://docs.mistral.ai/admin/billing-usage/usage-limits): Free mode limits shown only in the Admin Panel; API access may need billing activation.
- [Cerebras](https://inference-docs.cerebras.ai/support/rate-limits): one-time $5 trial credit that expires after 30 days; no renewing free tier.
- [GitHub Models](https://docs.github.com/en/github-models/use-github-models/prototyping-with-ai-models): retired on 2026-07-30.

Once you connect them, Oasis Route measures these tiers itself. Learned limits appear in the capacity report alongside the published ones.

## Comparison with other gateways' claims

[OmniRoute's free-tier reference](https://github.com/cheaper-inference/OmniRoute/blob/release/v3.8.55/docs/reference/FREE_TIERS.md) advertises about 1.62B tokens a month across third-party free tiers. That total pools more providers, including keyless and console-only tiers, and assumes those tiers are fully used. The figures here are deliberately conservative: only published, recurring, one-account limits, with the binding constraint applied per request size.
