# Changelog

All notable changes are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/). The HTTP API compatibility policy is described in [docs/API.md](docs/API.md#versioning).

## [Unreleased]

### Added

- Turnkey onboarding ([docs/ONBOARDING.md](docs/ONBOARDING.md)): `oasis-route setup` and `GET`/`POST /api/v1/projects/{id}/setup` detect local servers on their default ports (only when private upstreams are allowed), connect them as last-resort routes, list verified free providers separately from advertised ones, and return the single OpenAI-compatible endpoint. `GET /api/v1/projects/{id}/routes` classifies every route as free/paid/unknown and eligible/blocked/exhausted with the reason. Setup never changes the spend policy or billing modes. `<PROVIDER>_API_KEY` is recognised for every connectable imported provider.
- Adapter layer: providers resolve to a curated profile, a local-server preset, or a profile generated from the imported catalogue (auth header and prefix, extra headers, model-list URL), so adding one never touches the router. Anthropic-compatible APIs can use bearer or named-header auth. New local presets `ollama`, `llama_cpp`, `vllm`, `lm_studio`, `lemonade`, `llamafile`, `docker_model_runner`, `xinference` (no key; refused unless the base URL resolves to a private or loopback address). `routing/cost.py` estimates cost as free, priced (verified or estimate) or unknown in integer USD micros. See [docs/PROVIDER_ADAPTERS.md](docs/PROVIDER_ADAPTERS.md#adding-a-provider-without-touching-the-router).
- Unified catalogue ([docs/CATALOG.md](docs/CATALOG.md)): about 220 providers and 2,000 models imported from the OmniRoute catalogue (MIT) with provenance (commit, source URL, curation date, review state). Imported models use the new `lifecycle: "catalog"` and stay non-routable (`catalog_model_unconfirmed`) until the provider's own model list confirms them; imported free tiers become `discovered` offers with evidence and never set `pricing.free`; imported prices are `verified: false` estimates; blocked and unsupported providers are listed with the reason. New `catalog` JSON column on providers and models (migration 0009) and setting `OASIS_ROUTE_CATALOG_IMPORT_ENABLED`.
- OmniRoute alignment report ([docs/OMNIROUTE_ALIGNMENT.md](docs/OMNIROUTE_ALIGNMENT.md)): request lifecycle map, existing building blocks, and what OASIS Route adopts, adapts and rejects from OmniRoute's catalogue, adapters, routing and fallback.
- Whole-ecosystem free-capacity total: `scripts/ecosystem_capacity.py` counts every free tier listed by OASIS Route, the FreeLLMAPI catalog and OmniRoute, once per account with shared pools counted once. About 1.7B tokens a month for agent-sized requests (626M at chat size), 2.3B with OpenRouter's one-time unlock. A new README card and the `TOTAL` badge show it, next to FreeLLMAPI's 10.1B and OmniRoute's 1.62B claims.
- Free-tier capacity re-checked on 2026-11-23: about 30M chat / 70M agent tokens a month with no purchase, about 650M agent tokens after a one-time 10 OpenRouter credits, and a 2.7B theoretical ceiling with NVIDIA's prototyping tier. `scripts/free_tier_capacity.py` now models request-per-month caps (Cohere) and Cloudflare neurons, prints four scenarios, and reflects SambaNova's reduced model list. See [docs/FREE_TIER_CAPACITY.md](docs/FREE_TIER_CAPACITY.md).
- Redesigned README with generated, theme-aware artwork (`scripts/readme_assets.py` writes `assets/readme/*.svg` and the social preview), a capacity chart and a comparison with OmniRoute and other gateways.
- Community files: issue forms (bug, feature idea, provider or free-tier update), pull request template, `SUPPORT.md`, `CITATION.cff` and Dependabot for uv, GitHub Actions and Docker.
- Package metadata for discovery: PyPI keywords, classifiers and project URLs.
- Hugging Face device-code sign-in (RFC 8628) for servers without a browser: `"mode": "device"`, a short user code, and `POST .../oauth/{flow_id}/poll` honouring `interval` and `slow_down`.
- `oasis-route dev --connect-env [--free-tier --age N --country CC]` connects the provider keys in your environment on every start and pauses the mock providers once a real one is active.
- Paste-your-keys now lets you pick the provider for keys whose format several providers share (`providers_by_line`, from the dry run's `line`).
- One-step onboarding. **Connections → Paste your keys** in the console, `POST /api/v1/projects/{id}/connections/quick` and `oasis-route connect --from-env` connect every key at once: each key's provider is recognised locally from its variable name or prefix (ambiguous keys are never tried against several providers), validated only with that provider, and its models and approved discovery sources refreshed.
- Cohere (trial keys, OpenAI-compatible endpoint) and Z.ai (free GLM Flash models) providers, with key-only validation for providers that do not list models.
- Atom/RSS announcement and release feeds as discovery sources (DTDs refused).
- Sources record the extractor release that read them; after an upgrade each page is fetched unconditionally once and re-extracted (migration 0008).

### Changed

- Position OASIS Route as the first-party, optional inference-routing companion to OASIS while preserving standalone deployment and standard OpenAI/Anthropic API compatibility. Add an integration guide that distinguishes the working generic endpoint configuration from the upcoming native OASIS provider preset.
- Upgrade GitHub Actions to current Node 24-compatible major releases (`checkout`/`setup-uv`/`upload-artifact` v7 and Docker login/build-push v4/v7).

### Fixed

- The discovery fetcher decoded gzip responses twice, so every site that compresses robots.txt was recorded as `disallowed_by_robots`. Found by running discovery against the live seeds.
- Rate-limit tables of providers documented as billed (OpenAI, Anthropic) are no longer labelled as a free tier; price schedules ("$2.00 through November 30") are no longer read as an offer's expiry date.
- The Gemini rate-limits seed moved to `aistudio.google.com`; moved seeds now migrate automatically.
- Per-key rate limits now hold across replicas: on PostgreSQL the counters live in the database (sliding one-minute window, migration `0005`). Previously N replicas allowed up to N times the limit. `OASIS_ROUTE_RATE_LIMIT_BACKEND` selects `auto`, `database` or `memory`.
- `oasis-route dev` crashed inside the container image, which sets production mode; `dev` now always runs in development mode and keeps state in `/data/dev` (`OASIS_ROUTE_DEV_STATE_DIR`).
- Image names in the release workflow are lowercased; GHCR rejects the mixed-case owner name.

### Changed

- Rate limits (`429`) now cool down only the affected model on that connection, escalating to the whole connection when a second model is also limited. Previously one model's per-model limit blocked every model on the account. Account hopping within a request is still refused.

### Added

- Live provider discovery: 17 maintainer-reviewed seed sources (official docs and public catalogues), a bounded frontier that follows related pages on official domains, OpenRouter and Hugging Face catalogue extractors, HTML rate-limit table extraction, credit, trial, card and usage-scope claims, and provider candidates from catalogues (`/api/v1/admin/provider-candidates`). `POST /api/v1/admin/discovery/run` runs due sources now. See [docs/DISCOVERY_AND_REGISTRY.md](docs/DISCOVERY_AND_REGISTRY.md).
- Free-only spend policy, the default for new projects: routes are classified `local`, `free_model`, `free_tier_account`, `paid` or `unknown`, and the last two are rejected (`paid_route_blocked`, `cost_unknown`) unless the project allows paid usage with an audited acknowledgement. Connections have a `billing_mode`. See [ADR-0002](docs/adr/0002-free-only-spend-policy.md).
- Account sync: OpenRouter key status (free tier, credits, free-model daily quota as a `*:free` pool) and Hugging Face billing status.
- Sign in with Hugging Face: OAuth 2.0 authorization code + PKCE, a Client ID Metadata Document at `/.well-known/oauth-cimd`, and the `connections.oauth_refresh` worker job.
- `GET /api/v1/discover` and the console's Discover tab: providers with access requirements, verified offers, evidence links, model counts and the next step, with search and filters.
- CI publishes `ghcr.io/fahthiz/oasis-route:edge` from every green push to `main`, and smoke-tests `docker run <image> dev`.
- `docs/COMPARISON.md`: dated comparison with LiteLLM, OmniRoute, new-api, Portkey, TensorZero and Bifrost, including where they are ahead.
- Provider seeds: NVIDIA API Catalog (`https://integrate.api.nvidia.com/v1`, data policy recorded from its trial terms: inputs are used to improve NVIDIA models, so privacy-restricted projects exclude it), SambaNova Cloud and the Hugging Face Inference Providers router.
- OpenAI Responses API: `POST /v1/responses` (string or item input, function calls, JSON formats, images, streaming events with sequence numbers), stateless, routed through the same engine as chat completions. Codex CLI now works with `wire_api = "responses"`; see [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md).
- README: one-command trial (`uvx --from git+https://github.com/FAHTHIZ/OASIS-ROUTE oasis-route dev`) and `uv tool install` instructions, both verified against `main`.
- [docs/LOCAL_MODELS.md](docs/LOCAL_MODELS.md): connecting llama.cpp, Ollama and vLLM as providers, with a verified end-to-end run against a real local model.
- [docs/FREE_TIER_CAPACITY.md](docs/FREE_TIER_CAPACITY.md) and `scripts/free_tier_capacity.py`: reproducible monthly token-capacity maths from published free-tier limits (one account per provider, binding-limit per request size).
- Anthropic Messages API: `POST /v1/messages` (text, images, tools, streaming) and `POST /v1/messages/count_tokens`, routed through the same engine as chat completions. Unconnected `claude-*` names route as `auto`, so Claude Code works against any provider.
- `auto/<task>` model aliases (`auto/coding`, `auto/reasoning`, ...) and listing in `/v1/models`.
- [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md): cURL, Python, Node, Anthropic SDK, Claude Code, Aider, Continue, Cline, OpenCode, Open WebUI.
- Name-based routing priors for unrated models: `auto` now tries larger/flagship models before small ones without operator input. Priors are labelled (`prior`, `prior_source`, `quality_prior`), always rank after real quality evidence, and never satisfy `min_quality`.
- Account data rights: `GET /api/v1/projects/{id}/export`, `GET /api/v1/users/{id}/export`, `DELETE /api/v1/projects/{id}`, `DELETE /api/v1/users/{id}`. Exports never include secrets; deletion destroys stored provider credentials first.
- Optional retention for usage records, routing decisions and audit events (`OASIS_ROUTE_USAGE_RETENTION_DAYS`, `OASIS_ROUTE_ROUTE_DECISION_RETENTION_DAYS`, `OASIS_ROUTE_AUDIT_RETENTION_DAYS`; default keep).
- Official provider OAuth connections (OpenRouter PKCE) with redirect and headless modes, single-use expiring state, vault-sealed verifier and a console sign-in button. New setting `OASIS_ROUTE_PUBLIC_URL`.

## [0.1.0] - 2026-11-17

First functional release.

### Added

- OpenAI-compatible gateway: `POST /v1/chat/completions` (streaming and non-streaming, tools, JSON mode) and `GET /v1/models`, authenticated with an Oasis Key (`Authorization: Bearer $OASIS_API_KEY`).
- Provider adapters for OpenAI, Anthropic, Gemini (OpenAI-compatible endpoint), Groq, Mistral, OpenRouter, Cerebras and custom OpenAI-compatible servers, with normalized errors, usage and rate-limit header parsing.
- Envelope-encrypted credential vault with master-key rotation, automatic re-wrap job and `oasis-route rotate-kek`.
- Atomic quota ledger with reservations, settlement, expiry, shared pools, reset windows and learning from provider headers.
- Strongest-suitable-first router with task detection, capability filters, eligibility policy, explainable decisions and route preview.
- Automatic failover with circuit breakers, bounded retries, honest downgrade headers and `Retry-After` on exhaustion.
- Idempotency keys for non-streaming requests.
- Discovery control plane: SSRF-safe fetcher with robots handling, extraction, evidence retention, verification workflow and offer lifecycle.
- Capacity analytics and exhaustion forecasting; usage and route-decision history.
- Identity, projects, scoped Oasis Keys, audit trail and tenant isolation.
- Supervised background worker (embedded by default) with leases, heartbeats, timeouts, exponential backoff, crash recovery, stall detection and administrator alerts.
- Web console with plain-language status, connections, capacity, keys, models, route preview, usage, offers, notices and background jobs.
- Optional mail notices (forwarding, and Gmail behind a compliance attestation), off by default.
- OpenTelemetry tracing and Prometheus metrics.
- CLI (`serve`, `worker`, `dev`, `migrate`, `bootstrap`, `rotate-kek`, `mock-provider`), container image and Docker Compose deployment.
- Documentation: API, configuration, deployment, operations, development.
