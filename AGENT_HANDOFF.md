# Agent Handoff — Oasis Route

You are the implementation agent for Oasis Route. Your task is to turn the architecture in this repository into a tested, usable open-source inference platform—not merely to produce another plan.

## Operating rules

- Work directly on the repository's existing default branch when the owner has authorized direct changes. Do not create a branch or PR unless explicitly requested.
- Begin by inspecting the complete repository, commit history, existing code, CI, package manifests, and current README. Preserve useful work. Remove obsolete or duplicated implementations rather than layering replacements on top.
- Read every document in `docs/` before implementation.
- Do not claim a feature is complete unless it is implemented, tested, documented, and wired into the application.
- Work in small coherent slices. After each slice: format, lint, type-check, run focused tests, run the full test suite where feasible, and inspect the diff.
- Keep the application runnable throughout development. Avoid huge unreviewable generated changes.
- Prefer mature open-source libraries for protocol handling, persistence, OAuth, telemetry, and provider SDKs. Do not adopt a dependency without checking maintenance, license, security posture, and whether it duplicates core logic.
- Record decisions in ADRs under `docs/adr/` when a meaningful architectural choice is made.
- Never commit real credentials, tokens, OAuth secrets, private email content, or user data.

## Product definition

Build a provider-neutral inference control plane with:
1. Public-offer discovery and source provenance.
2. Human-reviewable offer verification and policy eligibility.
3. Explicit user-authorized upstream provider connections.
4. A unified, preferably OpenAI-compatible inference API.
5. Capability-aware, strongest-suitable-first model routing.
6. Atomic quota reservation and usage reconciliation.
7. Bounded retries, circuit breakers, cooldowns, and fallback.
8. Capacity analytics that distinguish advertised, verified, and usable tokens.
9. An auditable administration interface and operational documentation.

Oasis Route is independent of Oasis. Oasis may be a first-party client, but no core capability may require Oasis.

## Hard boundaries

The system may:
- Create and manage a user's Oasis Route account and **Oasis Keys** (the unified credentials applications use to call the Oasis Route gateway).
- Discover public provider offers, official signup pages, documentation, public model endpoints, and advertised quota details.
- Help a user understand and complete provider onboarding.
- Connect provider accounts using official OAuth or user-supplied API credentials, where the provider supports this and the user is eligible.
- Track quota and route across separately authorized allocations when provider terms allow that use.
- Use anonymous/public endpoints only when the operator explicitly permits automated access and the terms allow it.

The system must not:
- Create or automate multiple upstream identities to obtain repeated trials or reset exhausted quotas.
- Use disposable inboxes, phone-number farms, identity rotation, CAPTCHA circumvention, or verification bypass.
- Scrape exposed keys, leaked credentials, private repositories, private mailboxes, or secrets from public code.
- Bypass paywalls, authentication, provider rate limits, geographical restrictions, age gates, or terms.
- Treat promotional credits as transferable, poolable, or resellable without explicit permission.
- Retry indefinitely or conceal a provider's rejection.
- Send user prompts to a provider that is not authorized for the user's selected data policy.

If an offer depends on prohibited account cycling or unclear terms, mark it ineligible and explain why. Do not implement a workaround.

## Mandatory product requirement: zero-operations autonomy

Read [docs/ZERO_OPERATIONS_AUTONOMY.md](docs/ZERO_OPERATIONS_AUTONOMY.md) before further implementation. This is a core product requirement and takes precedence over treating automation as optional polish. The user should add an authorized upstream API key (or complete official OAuth) once; Oasis Route must then validate and connect it, synchronize models/capabilities/quotas, initialize safe routing, and run discovery, health checks, quota refresh, connection revalidation, offer reconciliation, and recovery in the background. The user should not have to configure providers, routing rules, quota pools, schedules, or worker processes. The application’s default deployment must actually start and supervise the worker; dormant worker functions do not satisfy this requirement. Add lease renewal/heartbeats, graceful shutdown, crash recovery, job observability, and end-to-end tests. Keep user consent for paid usage, provider authorization, privacy choices, and policy exceptions explicit. The Oasis Key remains distinct from the upstream provider credential. Treat the document’s acceptance criteria as release gates.

## Required implementation sequence

### Phase 0 — Repository and stack audit
- Inventory files, dependencies, runtime, CI, tests, license, and deployment model.
- Identify existing useful modules and duplicated/obsolete code.
- Write `docs/IMPLEMENTATION_AUDIT.md` with findings and a concise stack decision.
- Choose one coherent backend, persistence layer, queue/scheduler approach, and UI strategy. Avoid premature microservices.

### Phase 1 — Runnable skeleton and contracts
- Establish configuration, structured logging, health endpoints, migrations, CI, formatting, linting, type checking, and test harness.
- Define domain types and API contracts before provider-specific code.
- Add local development instructions and a one-command development startup where practical.

### Phase 2 — Identity, keys, and credential vault
- Implement user identity and scoped **Oasis Keys** for authenticating applications to the Oasis Route gateway. Use `OASIS_API_KEY` as the documented client environment-variable name.
- Hash Oasis Keys; show secret material only once. Keep Oasis Keys distinct from upstream provider credentials, which are encrypted in the vault.
- Encrypt upstream credentials at rest using a managed key or envelope-encryption design.
- Redact secrets from logs, traces, errors, and support exports.
- Add revocation, rotation, and audit events.

### Phase 3 — Provider adapters and unified gateway
- Define a provider adapter interface and implement one well-supported provider end-to-end.
- Add model discovery, capability metadata, streaming, cancellation, timeouts, error normalization, and usage extraction.
- Implement an OpenAI-compatible chat-completions surface where feasible, with explicit compatibility notes.
- Add contract tests with deterministic mock servers.

### Phase 4 — Quota ledger and router
- Implement quota pools, atomic reservations, usage reconciliation, idempotency, concurrency control, and reset windows.
- Implement strongest-suitable-first routing and bounded fallback.
- Add policy constraints, capability requirements, and downgrade transparency.
- Test concurrent exhaustion and duplicate/shared pools.

### Phase 5 — Discovery and registry
- Implement source connectors for official documentation, public repositories, public announcements, and curated source lists.
- Respect source terms, robots directives where applicable, crawl budgets, backoff, and conditional requests.
- Store evidence snapshots, source URLs, retrieval timestamps, and confidence.
- Extract candidate offers, but never activate an offer from unverified extraction alone.
- Provide human review and an auditable verification state machine.

### Phase 6 — Health, recovery, and capacity
- Add circuit breakers, provider health, cooldowns, reset-aware recovery, and safe retry rules.
- Calculate advertised, verified, currently usable, and forecast capacity separately.
- Add metrics and a dashboard for quota, routing, provider health, and failover.

### Phase 7 — Optional Gmail integration
- Do not make Gmail a prerequisite for core operation.
- Implement only after a documented Google OAuth and data-protection review.
- Use the narrowest scope possible. Gmail `gmail.readonly` is a restricted scope; server-side storage/transmission can trigger Google's security assessment requirements.
- Prefer provider-specific notification integrations or user-forwarded messages if they meet the use case with less access.
- No automated provider account creation or trial cycling. Mail is for user-authorized notifications and onboarding assistance only.
- Do not enable in production until OAuth verification, consent text, retention, deletion, and security requirements are satisfied.

### Phase 8 — Release hardening
- Threat model, dependency audit, secret scanning, SBOM, license review, backups, migration tests, rate limiting, abuse controls, load tests, disaster recovery, and deployment guide.
- Publish a truthful feature matrix and known limitations.
- Choose and add an OSI-approved license before public release.

## Definition of done

A phase is done only when:
- Its code is integrated into the actual application.
- Unit and integration tests cover normal and failure paths.
- Security and policy boundaries are enforced server-side.
- Migrations and configuration are documented.
- Logs and metrics make behavior diagnosable without leaking secrets.
- User-facing docs explain setup, limitations, and failure modes.
- CI passes and the working tree is clean.

## First actions

1. Complete Phase 0 and commit the audit.
2. Propose the stack with evidence and one ADR.
3. Implement Phase 1 before building provider features.
4. Continue phases sequentially; do not skip to scraping or Gmail before the gateway, authorization model, and policy foundations exist.
