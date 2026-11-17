# Execution Plan and Acceptance Criteria

This plan is deliberately sequential. A coding agent should deliver working vertical slices, not generate all files at once and call the project finished.

## P0 — Audit and product foundation
- [ ] Inventory repository and write `IMPLEMENTATION_AUDIT.md`.
- [ ] Select stack and document trade-offs in an ADR.
- [ ] Select OSI-approved license and add license text.
- [ ] Define threat model and configuration strategy.
- [ ] Establish CI, formatting, linting, type-checking, unit tests.

**Acceptance:** clean install/build/test; documented local run; no secrets; stack decision justified.

## P1 — Core application skeleton
- [ ] Modular monolith layout with clear domain/application/adapter boundaries.
- [ ] Configuration validation and safe defaults.
- [ ] Structured logs with redaction.
- [ ] Database schema and migrations.
- [ ] Health/readiness endpoints.
- [ ] API versioning and error envelope.

**Acceptance:** application starts locally; migrations apply/revert; CI checks pass.

## P2 — Identity and credential management
- [ ] User/project model and authorization.
- [ ] API key issue, hash, one-time reveal, revoke, rotate.
- [ ] Provider connection model.
- [ ] Encrypted credential vault and key management interface.
- [ ] Audit trail and secret redaction tests.

**Acceptance:** tenant isolation tests; credential ciphertext only at rest; no secret in logs.

## P3 — Gateway and first adapter
- [ ] OpenAI-compatible chat endpoint.
- [ ] Request validation and normalized responses.
- [ ] Streaming, cancellation, timeouts.
- [ ] Adapter interface and one official provider adapter.
- [ ] Mock provider contract suite.

**Acceptance:** a user can connect an eligible provider and send a test request through Oasis Route; all adapter failure cases tested.

## P4 — Quota ledger and router
- [ ] Quota pool, reservation, usage, reset-window schemas.
- [ ] Atomic reservations and idempotency.
- [ ] Shared-pool deduplication.
- [ ] Capability-aware quality-first router.
- [ ] Bounded fallback and clear model/fallback metadata.
- [ ] Concurrency and quota exhaustion tests.

**Acceptance:** parallel requests cannot overspend a simulated pool; exhausted provider falls back only to another authorized eligible pool; no suitable fallback returns clear error.

## P5 — Discovery and verification
- [ ] Source registry and crawl policy.
- [ ] Official documentation/repository connectors.
- [ ] Change detection and candidate extraction.
- [ ] Evidence provenance and review workflow.
- [ ] Terms/eligibility fields and policy decision.
- [ ] User-submitted source workflow.

**Acceptance:** discovered offer cannot become active without verification and authorized connection; crawler obeys budgets and restrictions.

## P6 — Health, recovery, capacity
- [ ] Health state and circuit breakers.
- [ ] Cooldowns and Retry-After handling.
- [ ] Reset-aware recovery checks.
- [ ] Advertised/verified/authorized/usable capacity calculations.
- [ ] Usage and routing dashboard.
- [ ] OpenTelemetry instrumentation.

**Acceptance:** simulated outage, quota reset, auth expiry, and model removal behave correctly; dashboards reconcile with ledger.

## P7 — Product UX and deployment
- [ ] Onboarding and provider connection UX.
- [ ] API key management.
- [ ] Model catalogue and route preview.
- [ ] Usage/capacity dashboards.
- [ ] Docker/dev deployment and production deployment guide.
- [ ] Backup/restore and migration runbook.

**Acceptance:** a new user can run locally, connect one eligible provider, create a key, and make a request using documented steps.

## P8 — Optional Gmail integration
- [ ] Compliance review and approval gate.
- [ ] Narrow OAuth scopes and consent UX.
- [ ] Provider notification classifier.
- [ ] Minimal extraction, retention and deletion.
- [ ] Revocation and security tests.

**Acceptance:** no production enablement until all applicable Google verification/security assessment requirements are satisfied.

## P9 — Release readiness
- [ ] Threat-model review and remediation.
- [ ] Dependency, license, secret, and container scans.
- [ ] SBOM and release provenance.
- [ ] Load, soak, concurrency, and fault-injection tests.
- [ ] Security policy, contribution guide, code of conduct, changelog.
- [ ] Versioned API docs and honest feature matrix.

**Acceptance:** release checklist signed off; no critical/high unresolved security findings; install and upgrade paths verified.

## Prioritization rule

Do not begin broad internet crawling before the offer schema, evidence model, policy checks, and adapter contracts exist. Do not build Gmail before the gateway and security posture. The MVP is one provider end-to-end plus a correct gateway/router/ledger, not hundreds of scraped listings.
