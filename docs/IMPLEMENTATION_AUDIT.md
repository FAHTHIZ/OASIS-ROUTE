# Implementation Audit (Phase 0)

Date: 2026-11-17

## 1. Repository inventory at audit time

| Item | Finding |
|---|---|
| Commits | 2 (`Initial commit`, `docs: establish Oasis Route architecture and agent handoff`) |
| Source code | None. No runtime, packages, modules, or scripts existed. |
| Dependencies / manifests | None (no `package.json`, `pyproject.toml`, lockfile, Dockerfile). |
| CI | None (no `.github/workflows`). |
| Tests | None. |
| License | None selected (README explicitly deferred the decision). |
| Deployment model | Undefined beyond the architecture's "modular monolith + worker + PostgreSQL" guidance. |
| Documentation | `README.md`, `AGENT_HANDOFF.md`, and seven architecture documents in `docs/`. All are useful and preserved. |

### Useful work to preserve
All documents. They define product boundaries, invariants, the offer state machine, quota semantics,
failure taxonomy, adapter contract, and security gates. Implementation follows them; where the code makes a
concrete choice, an ADR records it.

### Obsolete or duplicated code
None — there was no code. The only stale content is the README's "runtime is not yet implemented" status,
which is replaced by an honest feature matrix as features land.

## 2. Requirements distilled from the documents

- Modular monolith: one API process (user/admin API + inference gateway) and one worker process.
- PostgreSQL is authoritative for quota accounting; Redis must not be the source of truth.
- Atomic, idempotent, multi-dimensional quota reservations against shared pool rows.
- OpenAI-compatible chat-completions surface, streaming, cancellation, timeouts, normalized errors.
- Strict quality-first and balanced routing modes; bounded, visible fallback; no silent downgrade.
- Evidence-backed discovery with crawl policy, robots, budgets, conditional requests, SSRF defences.
- Offer verification state machine; no activation from extraction alone.
- Envelope-encrypted credential vault; hashed one-time-reveal API keys; redaction everywhere.
- Circuit breakers, Retry-After cooldowns, reset-aware recovery.
- Capacity analytics separating advertised / verified / authorized / usable / consumed / forecast.
- Gmail strictly optional and gated behind compliance attestation.

## 3. Stack decision (see ADR-0001)

| Concern | Choice | Why |
|---|---|---|
| Language/runtime | Python ≥ 3.12 | Mature async HTTP, crypto, DB, and OpenTelemetry ecosystems; provider SDK ecosystem is Python-first. |
| Web framework | FastAPI + Uvicorn | Async streaming (SSE), Pydantic validation, OpenAPI generation for versioned API docs. |
| Persistence | PostgreSQL via SQLAlchemy 2 (async, asyncpg); SQLite (aiosqlite) for zero-dependency local dev/tests | Transactional quota ledger with conditional atomic updates that are correct on both engines. |
| Migrations | Alembic | De-facto standard for SQLAlchemy; supports upgrade/downgrade tests. |
| Outbound HTTP | httpx | Async, streaming, strict timeouts, pluggable transports (deterministic mock transports in tests). |
| Crypto | `cryptography` (AES-256-GCM envelope encryption) + HMAC-SHA256 keyed hashing for API keys | Audited library; no custom primitives. |
| Queue/scheduler | Database-leased jobs run by an asyncio worker (`oasis-route worker`) | Avoids a broker dependency; jobs are idempotent and lease-based. Redis can be added later for rate limits if multi-instance scale requires it. |
| Metrics/tracing | `prometheus-client` (`/metrics`) + OpenTelemetry API spans (SDK/exporter optional) | Standard scraping and vendor-neutral tracing; no prompt content emitted. |
| UI | Server-hosted static admin console (vanilla HTML/JS, no build step) calling the JSON admin API | Keeps the project one-command runnable; no Node toolchain needed. |
| Tooling | uv, ruff (format + lint), mypy (strict on package), pytest + pytest-asyncio | Fast, reproducible, single toolchain. |
| License | Apache-2.0 | OSI-approved, patent grant, matches the Oasis project's license. |

### Rejected alternatives
- **Wrapping LiteLLM/OmniRoute as the router:** the handoff forbids wrapping competing gateways or duplicating
  their routing engines; our router must enforce eligibility/evidence semantics they do not model.
- **Node/TypeScript:** viable, but Python offered stronger first-party crypto/DB/migration tooling with fewer
  moving parts for this team.
- **Microservices / message broker:** premature; the architecture explicitly asks for a modular monolith.
- **Redis-backed quota:** violates the "database is authoritative" invariant.

## 4. Planned module layout

```
src/oasis_route/
  config.py            settings + validation + safe defaults
  logging.py           structured JSON logs with secret redaction
  db/                  SQLAlchemy models, engine/session, Alembic migrations
  domain/              enums, value types, error envelope, state machines
  security/            api keys, credential vault (envelope encryption), redaction, SSRF guard, rate limits
  identity/            users, projects, roles, audit events
  adapters/            adapter contract, OpenAI-compatible adapter, Anthropic adapter, registry
  quota/               ledger: pools, reservations, reconciliation, reset windows
  routing/             candidate filtering, ranking, fallback executor
  health/              circuit breakers, cooldowns, recovery checks
  discovery/           sources, safe fetcher, robots, extraction, evidence, review workflow
  capacity/            advertised/verified/authorized/usable/consumed/forecast analytics
  notifications/       in-app notices; optional, gated Gmail/forwarded-mail classifier
  api/                 FastAPI routers: gateway (/v1), admin (/api/v1), health, metrics
  worker.py            scheduled jobs
  cli.py               serve / worker / migrate / bootstrap / keys
```

## 5. Risks identified
- Provider quota semantics vary and change; mitigated by evidence records with confidence and review dates.
- SQLite lacks row locks; mitigated by single-statement conditional updates that are atomic on both engines,
  with concurrency tests on PostgreSQL in CI.
- Benchmarks drift; quality scores are stored per task with source/date/confidence and are operator-editable.

## 6. Verification status (updated 2026-11-17)

Every phase in [EXECUTION_PLAN.md](EXECUTION_PLAN.md) is checked off only with code wired into the running
application and tests that exercise it. Evidence for each release gate:

| Gate | Evidence |
|---|---|
| P0–P1 skeleton, config, logs, migrations, health | `tests/test_config.py`, `test_health.py`, `test_migrations.py` (round trip + models-match-migrations drift guard) |
| P2 identity, Oasis Keys, vault, audit, isolation | `test_api_keys.py`, `test_vault.py`, `test_kek_rotation.py`, `test_audit.py`, `test_tenant_isolation.py`, `test_redaction.py` |
| P3 gateway and adapters | `test_gateway.py`, `test_adapters_contract.py` (success, malformed, streaming, cancellation, timeout, reset, 401/403/404/429/5xx, Retry-After, usage, quota headers, redaction, oversize, redirects) |
| P4 ledger and router | `test_quota_ledger.py`, `test_quota_concurrency.py`, `test_router_policy.py`, `test_router_fallback.py` |
| P5 discovery and verification | `test_discovery_review.py`, `test_extraction.py`, `test_ssrf.py` |
| P6 health, recovery, capacity, telemetry | `test_health_breaker.py`, `test_capacity.py`, `test_telemetry.py` |
| P7 UX, CLI, deployment | `test_cli.py`, `test_zero_ops.py` (one-credential onboarding end to end, supervised worker, stall alerts), CI compose smoke test |
| P8 optional mail | `test_notifications.py` |
| P9 release readiness | `test_resilience.py` (seeded fault injection under concurrency), CI supply-chain job (pip-audit, licenses, gitleaks, SBOM), Trivy, `scripts/install_check.sh`, `scripts/upgrade_check.sh`, [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) |
| Data rights (export, erasure, retention) | `test_data_rights.py`, `test_worker.py::test_record_retention_is_opt_in_and_prunes_only_old_rows` |

Open items are tracked as known limitations in the [README](../README.md#known-limitations), not as
unchecked plan items.
