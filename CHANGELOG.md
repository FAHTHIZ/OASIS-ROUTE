# Changelog

All notable changes are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/). The HTTP API compatibility policy is described in [docs/API.md](docs/API.md#versioning).

## [Unreleased]

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
