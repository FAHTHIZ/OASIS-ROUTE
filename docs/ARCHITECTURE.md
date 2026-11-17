# Oasis Route Architecture

## 1. Product boundary

Oasis Route is an open-source inference control plane. It discovers public offers, maintains an evidence-backed registry, connects user-authorized upstream accounts, and routes application requests to eligible inference endpoints.

It is not an account farm, credential marketplace, anonymous proxy, or quota-evasion service.

## 2. System planes

### Control plane
Responsible for users, projects, API keys, provider connections, discovery jobs, offer review, policy decisions, routing configuration, quota pools, and administration.

### Data plane
Responsible for authenticated inference requests, task/capability constraints, model selection, quota reservations, provider adapter calls, streaming, retries, and usage reconciliation.

Keep the planes logically separate. The first implementation should be a modular monolith with clear package boundaries, not a premature distributed microservice fleet. Split services only after measured scaling or isolation needs justify it.

## 3. Logical components

- **Identity & Projects:** users, organizations if later needed, projects, roles, sessions, API keys, audit events.
- **Credential Vault:** provider connections, OAuth grants, encrypted API credentials, key rotation and revocation.
- **Source Discovery:** source catalogue, scheduled fetches, crawl policy, change detection, candidate extraction.
- **Offer Verification:** evidence review, terms and eligibility checks, confidence, reviewer history, expiry.
- **Provider Registry:** provider, model, endpoint, adapter, offer, quota pool, capability, geographic and audience constraints.
- **Provider Adapters:** protocol translation, authentication, streaming, timeout, normalized errors and usage.
- **Unified Gateway:** OpenAI-compatible endpoints, request validation, API-key authentication, streaming, request IDs.
- **Task & Capability Resolver:** derive required modalities, context size, tool use, structured output, coding/reasoning constraints from explicit request options and conservative metadata.
- **Router:** policy filter, capability filter, quota-aware selection, priority scoring, fallback.
- **Quota Ledger:** quota pools, reservations, reconciliation, reset windows, idempotency and concurrency.
- **Health & Recovery:** probes, circuit breakers, cooldowns, provider status and reset-aware reactivation.
- **Capacity Analytics:** advertised/verified/usable capacity, utilization, forecasts and uncertainty.
- **Notifications:** in-app and optional provider/Gmail notices.
- **Telemetry:** metrics, traces, structured logs and auditable routing decisions.

## 4. Request lifecycle

1. Authenticate the caller's Oasis Route API key and resolve project policy.
2. Validate request size, model alias, requested features, streaming mode, and idempotency key.
3. Resolve the caller's data policy, region restrictions, and any provider allow/deny lists.
4. Build candidate models from the registry.
5. Filter candidates by current offer eligibility, user authorization, model capability, region, data policy, provider health, and quota.
6. Rank candidates by model quality for the task, then policy preference, reliability, latency, and quota availability.
7. Atomically reserve estimated quota in the relevant quota pool.
8. Dispatch through the provider adapter.
9. Stream or return the normalized response.
10. Reconcile reservation against provider-reported usage; if usage is unavailable, mark estimate and confidence explicitly.
11. Persist route decision, normalized outcome, usage, and safe telemetry.
12. If retryable, release or adjust reservations and perform bounded fallback according to retry policy.

## 5. Discovery lifecycle

1. Load an approved source record with allowed methods, scope, crawl interval, and policy notes.
2. Fetch within per-domain budgets, obey applicable robots directives and terms, use conditional requests and backoff.
3. Extract candidate provider, model, access mode, quota statements, terms links, official onboarding URL, and evidence fragments.
4. Deduplicate candidate offers and shared quota pools.
5. Mark candidates as discovered—not active.
6. Verify claims against official documentation and provider terms. Record reviewer, evidence, timestamp, confidence, and eligibility.
7. A user connects an eligible upstream account through the supported authorization path.
8. Validate the connection and provider/model endpoint.
9. Activate only when offer policy, authorization, and health checks pass.
10. Revisit evidence and expiry; suspend on material changes.

Discovery never creates provider access by itself.

## 6. Deployment topology

Initial target: one deployable modular application plus a worker process, one relational database, and an optional cache/queue. Use interfaces so components can be separated later.

Suggested logical deployment:
- API process: user API, admin API, inference gateway.
- Worker: discovery, verification assistance, scheduled health checks, notification jobs.
- PostgreSQL: authoritative state, quota ledger, audit events.
- Redis or equivalent: ephemeral locks, rate limits, queue coordination only where justified.
- Object storage: evidence snapshots only if retention policy requires it.
- OpenTelemetry-compatible collector/exporter: metrics and traces.

Do not make Redis the source of truth for quota accounting. Quota reservations must be transactionally correct in the authoritative database.

## 7. Core invariants

- No request dispatch without a valid user/project authorization and an active provider connection.
- No offer activation based solely on crawler extraction.
- No quota reservation beyond the pool's known available budget.
- Shared quotas are counted once.
- Unknown quota is not treated as unlimited.
- No credential appears in logs or API responses.
- Provider policy eligibility is checked before routing.
- Fallback is bounded and visible in response metadata/logs.
- User data policies are enforced before a provider receives a prompt.
- A provider's quota reset does not imply permission to create another account.
