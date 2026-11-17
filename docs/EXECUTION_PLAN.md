# Execution Plan and Acceptance Criteria

This plan is deliberately sequential. A coding agent should deliver working vertical slices, not generate all files at once and call the project finished.

## P0 — Audit and product foundation
- [x] Inventory repository and write `IMPLEMENTATION_AUDIT.md`.
- [x] Select stack and document trade-offs in an ADR.
- [x] Select OSI-approved license and add license text.
- [x] Define threat model and configuration strategy.
- [x] Establish CI, formatting, linting, type-checking, unit tests.

**Acceptance:** clean install/build/test; documented local run; no secrets; stack decision justified.

## P1 — Core application skeleton
- [x] Modular monolith layout with clear domain/application/adapter boundaries.
- [x] Configuration validation and safe defaults.
- [x] Structured logs with redaction.
- [x] Database schema and migrations.
- [x] Health/readiness endpoints.
- [x] API versioning and error envelope.

**Acceptance:** application starts locally; migrations apply/revert; CI checks pass.

## P2 — Identity and credential management
- [x] User/project model and authorization.
- [x] API key issue, hash, one-time reveal, revoke, rotate.
- [x] Provider connection model.
- [x] Encrypted credential vault and key management interface.
- [x] Audit trail and secret redaction tests.

**Acceptance:** tenant isolation tests; credential ciphertext only at rest; no secret in logs.

## P3 — Gateway and first adapter
- [x] OpenAI-compatible chat endpoint.
- [x] Request validation and normalized responses.
- [x] Streaming, cancellation, timeouts.
- [x] Adapter interface and one official provider adapter.
- [x] Mock provider contract suite.

**Acceptance:** a user can connect an eligible provider and send a test request through Oasis Route; all adapter failure cases tested.

## P4 — Quota ledger and router
- [x] Quota pool, reservation, usage, reset-window schemas.
- [x] Atomic reservations and idempotency.
- [x] Shared-pool deduplication.
- [x] Capability-aware quality-first router.
- [x] Bounded fallback and clear model/fallback metadata.
- [x] Concurrency and quota exhaustion tests.

**Acceptance:** parallel requests cannot overspend a simulated pool; exhausted provider falls back only to another authorized eligible pool; no suitable fallback returns clear error.

## P5 — Discovery and verification
- [x] Source registry and crawl policy.
- [x] Official documentation/repository connectors.
- [x] Change detection and candidate extraction.
- [x] Evidence provenance and review workflow.
- [x] Terms/eligibility fields and policy decision.
- [x] User-submitted source workflow.

**Acceptance:** discovered offer cannot become active without verification and authorized connection; crawler obeys budgets and restrictions.

## P6 — Health, recovery, capacity
- [x] Health state and circuit breakers.
- [x] Cooldowns and Retry-After handling.
- [x] Reset-aware recovery checks.
- [x] Advertised/verified/authorized/usable capacity calculations.
- [x] Usage and routing dashboard.
- [x] OpenTelemetry instrumentation.

**Acceptance:** simulated outage, quota reset, auth expiry, and model removal behave correctly; dashboards reconcile with ledger.

## P7 — Product UX and deployment
- [x] Onboarding and provider connection UX.
- [x] API key management.
- [x] Model catalogue and route preview.
- [x] Usage/capacity dashboards.
- [x] Docker/dev deployment and production deployment guide.
- [x] Backup/restore and migration runbook.

**Acceptance:** a new user can run locally, connect one eligible provider, create a key, and make a request using documented steps.

## P8 — Optional Gmail integration
- [x] Compliance review and approval gate.
- [x] Narrow OAuth scopes and consent UX.
- [x] Provider notification classifier.
- [x] Minimal extraction, retention and deletion.
- [x] Revocation and security tests.

**Acceptance:** no production enablement until all applicable Google verification/security assessment requirements are satisfied.

## P9 — Release readiness
- [x] Threat-model review and remediation.
- [x] Dependency, license, secret, and container scans.
- [x] SBOM and release provenance.
- [x] Load, soak, concurrency, and fault-injection tests.
- [x] Security policy, contribution guide, code of conduct, changelog.
- [x] Versioned API docs and honest feature matrix.
- [x] Install and upgrade paths verified in CI; account data export and erasure; release checklist ([RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md)).

**Acceptance:** release checklist signed off; no critical/high unresolved security findings; install and upgrade paths verified.

## Prioritization rule

Do not begin broad internet crawling before the offer schema, evidence model, policy checks, and adapter contracts exist. Do not build Gmail before the gateway and security posture. The MVP is one provider end-to-end plus a correct gateway/router/ledger, not hundreds of scraped listings.
