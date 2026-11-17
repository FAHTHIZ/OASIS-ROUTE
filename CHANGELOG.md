# Changelog

All notable changes are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/). The HTTP API compatibility policy is described in [docs/API.md](docs/API.md#versioning).

## [Unreleased]

### Changed

- Rate limits (`429`) now cool down only the affected model on that connection, escalating to the whole connection when a second model is also limited. Previously one model's per-model limit blocked every model on the account. Account hopping within a request is still refused.

### Added

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
