# How Oasis Route compares

Reviewed on 2026-11-17 against the most-starred open-source LLM gateways and routers. Star counts are from each repository page on that date and will drift. This page is meant to be fair: it says where other projects are ahead, not only where Oasis Route differs.

## The projects

| Project | Language | Stars | One-line start | Focus |
|---|---|---|---|---|
| [LiteLLM](https://github.com/BerriAI/litellm) | Python | 63.4k | `uv tool install 'litellm[proxy]'` | SDK plus proxy for 100+ providers, virtual keys, spend tracking, guardrails |
| [OmniRoute](https://github.com/cheaper-inference/OmniRoute) | TypeScript | 89.7k (2026-11-23) | `npm install -g omniroute` or `docker run` | Free-tier aggregation, `auto/<task>` combos, many routing strategies |
| [new-api](https://github.com/QuantumNous/new-api) | Go | 52k | `docker run` | Multi-tenant API resale/management: Chat, Responses, Messages, Gemini, embeddings, rerank, realtime |
| [Portkey Gateway](https://github.com/Portkey-AI/gateway) | TypeScript | 13.4k | `npx @portkey-ai/gateway` | Edge gateway with guardrails and conditional routing |
| [TensorZero](https://github.com/tensorzero/tensorzero) | Rust | 11.7k | Docker container | Gateway plus observability, evaluations, A/B experiments, fine-tuning loop |
| [Bifrost](https://github.com/maximhq/bifrost) | Go | 9.7k | `npx -y @maximhq/bifrost` or `docker run -p 8080:8080 maximhq/bifrost` | Low-latency gateway, semantic caching, budgets |
| **Oasis Route** | Python | new | `uvx --from git+https://github.com/FAHTHIZ/OASIS-ROUTE oasis-route dev` or `docker run -p 8080:8080 ghcr.io/fahthiz/oasis-route:edge dev` | Provider intelligence: evidence-backed offers, terms and data-policy gates, quota-aware routing |

## Feature matrix

"Yes" means the project's README documents it on the review date. "—" means the README does not mention it; the feature may still exist deeper in that project's docs, so read "—" as "not advertised", not "absent". Oasis Route cells describe what this repository implements and tests.

| Capability | LiteLLM | OmniRoute | new-api | Portkey | TensorZero | Bifrost | Oasis Route |
|---|---|---|---|---|---|---|---|
| OpenAI Chat Completions | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| OpenAI Responses API | Yes | Yes | Yes | — | — | — | Yes, stateless |
| Anthropic Messages API | Yes | Yes | Yes | — | — | Yes | Yes, incl. streaming |
| Embeddings | Yes | Yes | Yes | — | Yes | — | No (see below) |
| Virtual keys, quotas or budgets | Yes | Yes | Yes | Yes | — | Yes | Yes (scoped Oasis Keys, project limits) |
| Automatic failover | Yes | Yes | Yes | Yes | Yes | Yes | Yes, bounded by policy |
| Per-model rate-limit lockout | — | Yes | — | — | — | — | Yes (model first, connection on repeat) |
| Task-aware routing | — | Yes (`auto/<task>`) | — | Conditional rules | Experiments | — | Yes (`auto`, `auto/<task>`) |
| Response caching | Yes | Yes | — | Yes | — | Yes | No |
| Guardrails or PII masking | Yes | Yes | — | Yes | — | Yes | No |
| Web dashboard | Yes | Yes | Yes | — | Yes | Yes | Yes (`/console/`) |
| Published container image | Yes | Yes | Yes | — | Yes | Yes | Yes (`ghcr.io/fahthiz/oasis-route:edge`) |
| Quotas learned from response headers | — | — | — | — | — | — | Yes |
| Provider terms, age and multi-account checks | — | — | — | — | — | — | Yes |
| Data-policy gating (no-training, zero-retention) | — | — | — | — | — | — | Yes, unknown never passes |
| Evidence trail behind each offer | — | — | — | — | — | — | Yes |
| Reproducible free-tier token maths | — | Claimed total (~1.62B) | — | — | — | — | Yes, ~30M chat / ~70M agent with no purchase ([script](../scripts/free_tier_capacity.py)) |

## Where the others are ahead

- **Surface area.** LiteLLM and new-api cover embeddings, images, audio, batches and rerank. Oasis Route only routes text generation.
- **Install.** OmniRoute, Bifrost and Portkey start with one `npx` or `docker run`. Oasis Route needs `uv` or Docker (one command each), but has no npm package.
- **Provider count.** OmniRoute lists hundreds of providers and LiteLLM over a hundred. Oasis Route seeds twelve real providers (Cohere and Z.ai were added on 2026-11-23) and accepts any OpenAI-compatible endpoint, including Cloudflare Workers AI and local servers.
- **Operations features.** Caching, guardrails, cost and spend analytics, and experiment tooling (TensorZero) are not in scope yet. The built-in console covers overview, capacity, connections, keys, models, route preview, usage, offers and notices.

## Where Oasis Route is different

- **It refuses what others allow.** No account pooling, no key rotation to reset free quotas, and a provider whose terms or data policy are unknown is excluded from privacy-restricted projects instead of being assumed safe. See [SECURITY_AND_COMPLIANCE.md](SECURITY_AND_COMPLIANCE.md).
- **Capacity is measured, not advertised.** Limits learned from `x-ratelimit-*` headers and `429`s feed the router and the capacity report. The free-tier totals in [FREE_TIER_CAPACITY.md](FREE_TIER_CAPACITY.md) are a conservative floor computed from published limits, where [OmniRoute's reference](https://github.com/cheaper-inference/OmniRoute/blob/main/docs/reference/FREE_TIERS.md) gives a larger total of about 1.62B a month. Re-checked on 2026-11-23: about 1B of that total is Mistral, whose [usage-limits page](https://docs.mistral.ai/admin/billing-usage/usage-limits) now says Free mode doesn't include API access, and about 545M comes from third-party aggregators and proxies. OmniRoute's first-party pools add up to about 68M, close to the 32M-71M no-purchase range Oasis Route computes. Details in [FREE_TIER_CAPACITY.md](FREE_TIER_CAPACITY.md#comparison-with-other-gateways-claims).
- **Rate limits are scoped correctly.** A `429` cools down the model first; the whole connection is cooled only when a second model on it is rate-limited, and failover never hops to a second account of the same provider.
- **Every offer has evidence.** Discovery stores the source, date and review state behind each offer, see [DISCOVERY_AND_REGISTRY.md](DISCOVERY_AND_REGISTRY.md).

## Gaps closed after this review

Found by this comparison and implemented on `main` the same day: per-model `429` handling, the Anthropic Messages API, the OpenAI Responses API, `auto/<task>` aliases, coding-tool setup guides ([INTEGRATIONS.md](INTEGRATIONS.md)), local model guides ([LOCAL_MODELS.md](LOCAL_MODELS.md)), one-command `uvx` and `docker run` installs, shared rate limits across replicas, quality priors for well-known models, free-tier capacity maths, and seeds for NVIDIA, SambaNova and Hugging Face.

## Deliberately not done

- **Embeddings fallback.** Vectors from different models are not interchangeable, so silently failing over from one embedding model to another would corrupt a vector index. Embeddings need a pinned-model design without cross-model fallback. Discovery currently skips embedding models for this reason.
- **Stateful Responses.** `previous_response_id` and `background` need stored conversation state. They return a clear `400` rather than pretending to work.
