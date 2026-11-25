# OmniRoute alignment: architecture report

Reviewed on 2026-11-25 against OASIS Route `main` (`a775cea`) and the reference project
[alif2815/OmniRouter](https://github.com/alif2815/OmniRouter) at `ed2db6c` (a fork of OmniRoute v3.8.50, MIT
licence, copyright diegosouzapw).

North star: **OmniRoute's reach and ease of use, governed by OASIS Route's verification, budgeting and honest
failover.** This report was written before any implementation work and records what is adopted, adapted and
rejected, and why.

## 1. OASIS Route request lifecycle today

```text
client ──► /v1/chat/completions | /v1/responses | /v1/messages      api/gateway.py, gateway/*_compat.py
   │ Oasis Key (Bearer)  → security/api_keys.py: prefix lookup, PBKDF2 hash check, scope=inference
   │ project policy       → kill switch, rate_limit_rpm (security/rate_limit.py), idempotency key
   ▼
parse_body  (gateway/engine.py)  → ChatRequest + RouteOptions (task, mode, providers, require, free_only)
   ▼
build_plan  (routing/router.py)
   │ for every (connection, model) pair the project authorised:
   │   connection status → provider status → offer state/expiry → eligibility (age, country, multi-account)
   │   → data policy (allow/deny list, no-training, zero-retention, region) → spend policy (cost_basis)
   │   → capability/context fit → min_quality → health/breaker → quota view
   │ every rejection carries a machine-readable reason (+ retry_in_s)
   │ survivors ranked: preference → quality tier → reliability → latency → reset horizon → headroom
   ▼
Engine.complete / open_stream  (bounded by max_fallback_hops)
   │ _prepare: claim half-open probe → ledger.reserve (row-locked, idempotent per request:attempt)
   │ adapter.invoke / adapter.stream with credential decrypted per call (connections/service.load_target)
   │ _on_success: ledger.settle (provider usage or labelled estimate), breaker.record_success,
   │              ledger.apply_observations (x-ratelimit-* headers → learned pools)
   │ _on_failure: normalize_error → category; release reservation; breaker.record_failure (cooldown with
   │              Retry-After); exclude candidate / provider account; next candidate if retryable
   ▼
_decision → RouteDecision row (candidates, rejections, attempts, outcome) + UsageRecord per attempt
          → response carries `oasis_route` metadata / `x-oasis-*` headers
```

## 2. Existing building blocks

| Area | Where | What exists |
|---|---|---|
| Adapter interface | `adapters/base.py` | `ProviderAdapter` protocol: `list_models`, `validate_connection`, `estimate_usage`, `invoke`, `stream`, `normalize_error`, `extract_usage`, `quota_observations`, `health_check`, `get_capabilities`; `AdapterMetadata`; shared Retry-After/reset/SSE parsers. |
| Adapters | `adapters/openai_compat.py`, `adapters/anthropic.py`, `adapters/registry.py` | One OpenAI-compatible adapter parameterised by `OpenAIProfile` (12 profiles) and a native Anthropic Messages adapter. |
| Catalogue | `registry/catalog.py`, `db/models.py` | 13 hand-curated `ProviderSeed`s + 2 dev mocks; `Provider`, `Endpoint`, `Model` (capabilities, context, pricing, quality evidence), `Offer` (state machine, quota claims, evidence), `Source`, `Evidence`, `ProviderCandidate`. Seeding is additive. |
| Capacity states | `capacity/service.py` | advertised, verified, authorized, usable, consumed (confirmed vs estimated), forecast. |
| Quota reservation | `quota/ledger.py` | `QuotaPool` per provider account × model scope × unit × period; `reserve` with row locks, idempotency key, TTL; `settle`, `release`, `expire_stale`, learned pools from headers, `mark_exhausted`. |
| Budget policy | `routing/router.cost_basis`, ADR-0002 | `spend_policy` `free_only` (default) / `allow_paid`; `billing_mode` per connection; `cost_basis` `local`/`free_model`/`free_tier_account`/`paid`/`unknown`. Unknown is treated as paid. |
| Failover | `gateway/engine.py`, `health/breaker.py` | Bounded hops; per-model cooldown escalating to connection; half-open probes; never another account of a provider that forbids multi-account use; policy/auth errors stop. |
| Vault and keys | `security/vault.py`, `security/api_keys.py` | Envelope encryption (KEK + per-credential DEK, AES-GCM, AAD bound to connection); KEK rotation; hashed Oasis Keys shown once with scopes, expiry, rotation, revocation, audit events. |
| Surfaces | `cli.py`, `static/`, `api/*` | CLI: `dev`, `serve`, `worker`, `migrate`, `bootstrap`, `connect`, `rotate-kek`, `mock-provider`. Console tabs for status, connections, capacity, route preview, usage, discover. |
| Tests and docs | `tests/`, `docs/` | ~350 deterministic tests, no network; `tests/test_docs.py` fails when a setting or API path is undocumented; ADRs in `docs/adr/`; CHANGELOG `[Unreleased]`. |

## 3. Useful OmniRoute patterns

| Pattern | OmniRoute location | Notes |
|---|---|---|
| Provider/model catalogue | `open-sse/config/providers/registry/*` (220 entries, 1,940 models), `src/shared/constants/providers/*` (display, `freeNote`, `website`) | Declarative entries: `format`, `executor`, `baseUrl`, `authType`, `authHeader`, `modelsUrl`, models with `contextLength`, `maxOutputTokens`, `toolCalling`, `supportsVision`, `supportsReasoning`. |
| Free-tier catalogue | `open-sse/config/freeModelCatalog.data.ts` (523 records) | Per model: `freeType` (recurring-daily/monthly, one-time, keyless, uncapped, discontinued), `monthlyTokens`, `poolKey`, ToS verdict (`ok`/`caution`/`ambiguous`/`avoid`). Research snapshot curated 2026-07-22. |
| Adapters | `open-sse/executors/*` | `default` executor covers every OpenAI/Anthropic-format API-key provider; dozens of bespoke executors for web sessions and CLI impersonation. |
| OpenAI-compatible endpoint | `src/app/api/v1/*` | Chat, Responses, Messages, models, embeddings, images, audio. |
| Auto mode | `auto/<task>` combos, 19 routing strategies (`priority`, `fill-first`, `cost-optimized`, `reset-aware`, `headroom`, ...) | |
| Multi-account | `accountSelector.ts`, `apiKeyRotator.ts` | Round-robin across extra keys of one provider. |
| Fallback chains | `domain/fallbackPolicy.ts`, `services/combo.ts`, `services/emergencyFallback.ts` | Declarative chains; emergency redirect on 402 to a "free" model, enabled by default. |
| Token refresh | `services/tokenRefresh/*` | CAS-guarded refresh with in-flight dedup. |
| Health/cooldown/lockout | `config/errorConfig.ts`, `accountFallback.ts` | Backoff steps, per-provider error rules. |
| Coding tools | `src/shared/constants/cliTools.ts` | Env/config recipes for Claude Code, Codex, Cursor, Cline, Roo, Aider, OpenCode, ... |
| Local servers | `src/shared/constants/providers/local.ts` | Ollama, LM Studio, vLLM, llama.cpp, Lemonade, llamafile, Docker Model Runner, Xinference, ... with default ports. |

## 4. Decisions

### Adopt (as data, with attribution)

- **The provider and model catalogue.** Every OmniRoute entry that uses a documented API with an API key
  (or no key, for local servers) and speaks an OpenAI-compatible or Anthropic-compatible protocol is imported
  into a generated data file, `registry/data/omniroute_catalog.json`, by `scripts/import_omniroute_catalog.py`.
  Provenance on every row: source repository, commit, file, curated date, and `review_state: unreviewed`.
- **Free-tier research** (`freeType`, `monthlyTokens`, ToS verdict, `freeNote`) is imported as **advertised**
  evidence only.
- **Local server presets** (ports and paths) and **coding-tool recipes** are re-expressed in OASIS terms.

### Adapt

- **Adapter coverage:** a catalogue-driven `OpenAIProfile` per imported provider (base URL, auth header, model
  list URL, capabilities), so a new provider is a data row, not router code. The Anthropic adapter gains
  base-URL-configurable Anthropic-compatible endpoints. Native Gemini stays on Google's documented
  OpenAI-compatible endpoint.
- **Routing strategies:** OmniRoute's `cost-optimized`, `headroom`, `reset-aware`, `fill-first` and latency
  strategies become a `strategy` preference applied *after* OASIS hard gates, never instead of them.
- **Fallback chains:** expressed as the ordered, explained candidate list of a route plan plus the three spend
  modes below. A chain never crosses from free to paid without the mode allowing it.
- **Multi-account:** kept to OASIS's rule: several accounts of one provider only when the provider's terms
  permit it (`multi_account_permitted`), never as quota cycling.
- **Pricing:** OmniRoute's per-model prices are imported as `pricing.source = omniroute` with
  `verified: false`; they feed cost *estimates*. OmniRoute's silent $5/$15 per-million default for unknown
  models is not adopted: unknown stays unknown.

### Reject

- **Web-session executors** (`chatgpt-web`, `claude-web`, `gemini-web`, `copilot-web`, `perplexity-web`,
  `deepseek-web`, ... 23 providers), cookie auth and the Cloudflare Turnstile solver: they automate consumer
  sessions and bypass bot checks, which the OASIS hard boundaries forbid.
- **CLI/IDE identity impersonation** (`codex`, `cursor`, `kiro`, `windsurf`, `antigravity`, `github` Copilot,
  `grok-cli`, `devin-cli`, `qoder`, `trae`, `codebuddy-cn`, Claude Code fingerprints, tool cloaking): these
  reuse another product's OAuth client or subscription outside its terms.
- **Reverse-engineered or third-party resale proxies** of other providers' free tiers (`g4f-*`, and every
  entry whose OmniRoute ToS verdict is `avoid`). They are imported into the report but marked
  `blocked` with the reason, so users see why they are not connectable.
- **Emergency fallback on budget exhaustion enabled by default**, round-robin key rotation to stretch free
  quota, and any fallback that silently changes cost class.

### Conflicts with OASIS guarantees (and how they are resolved)

| OmniRoute behaviour | Conflicting guarantee | Resolution |
|---|---|---|
| Free claims shown as usable capacity | Verified free-tier capacity | Imported claims are `advertised`; only provider-confirmed zero price (`pricing.free` from the provider's own list or a primary source) or a free-tier account makes a route free. |
| Default price for unknown models | Never present a paid route as free / honest cost | `cost_unknown`, blocked in zero-budget. |
| Emergency fallback to another model on 402 | Zero-budget enforcement, honest failover | Fallback only to candidates the spend mode allows; every hop recorded in the decision. |
| Retry across accounts | Honest, bounded failover; anti-abuse | Unchanged OASIS rule. |
| In-memory cooldowns | Auditability | Cooldowns stay in `HealthRecord` rows and are listed by `oasis-route quota`/`doctor`. |

## 5. Implementation order

Each item lands on `main` with tests, docs and a CHANGELOG entry before the next starts:

1. This report.
2. Unified catalogue: importer, data file, provenance, capacity layering, free/paid/unknown classification.
3. Adapter layer: catalogue-driven profiles, Anthropic-compatible base URLs, local presets (Ollama, llama.cpp,
   vLLM, LM Studio), adapter capability declaration.
4. Turnkey onboarding: local server detection, connectable free-provider list, default route set.
5. Policy-aware routing: strategy preference after gates, richer explanations.
6. Honest fallback: `zero_budget`, `bounded_autonomy` (explicit approval), `continuity` (pre-authorised paid
   fallback) spend modes; 429 cooldown metadata.
7. Budget ledger: worst-case USD cost reservation and settlement for paid routes.
8. Client compatibility: recipes for Claude Code, Codex, Cursor, Cline, Roo Code, Aider, OpenCode, SDKs.
9. CLI and console: `providers`, `models`, `routes`, `quota`, `verify`, `preview`, `doctor`, `explain`.
