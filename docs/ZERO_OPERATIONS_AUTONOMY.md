# Zero-Operations UX and Autonomous Orchestration

## Product requirement

**The user supplies an authorized provider API key (or completes an official OAuth connection) once. Oasis Route handles the operational work in the background. The user should be able to return to their normal work—especially coding—without managing providers, quotas, routing rules, scheduled jobs, or recovery procedures.**

This is a core product requirement, not optional polish. Do not build a product that exposes its internal infrastructure as a sequence of chores for the user.

The intended experience is:

1. The user opens Oasis Route and adds an upstream provider API key, or authorizes a provider through its official OAuth flow.
2. Oasis Route securely stores the credential, validates it, identifies the account's eligible models and available capabilities, and reports any required action in plain language.
3. Oasis Route automatically initializes the user's routing profile, discovers and verifies eligible public offers, starts background maintenance, checks provider health, synchronizes quota information, and prepares safe routing.
4. Oasis Route issues or helps the user create an Oasis Key for client applications. This is the unified credential used by applications to call the Oasis Route gateway; it is distinct from the upstream provider credential.
5. The user configures their coding tool or application once to use the Oasis Route endpoint and Oasis Key. From then on, requests are routed automatically.
6. Oasis Route continuously maintains the system in the background. The user only sees concise status, meaningful notifications, and actions that genuinely require their authorization.

## What the user should not have to do

Do not require users to manually:
- Search for providers or eligible offers.
- Compare model catalogues or select a provider for every request.
- Configure routing tables, fallback chains, quota pools, or reset schedules.
- Start, restart, or monitor background workers.
- Trigger discovery, connection checks, quota refreshes, or retention jobs.
- Diagnose routine provider outages or manually move traffic after a failure.
- Re-enter a valid credential for routine refreshes when the provider's supported authorization mechanism can refresh it safely.
- Understand internal terms such as leases, circuit breakers, adapters, or reservation ledgers.

These are internal responsibilities of the product. Advanced controls may exist for operators, but must not be prerequisites for ordinary use.

## Autonomous responsibilities

After a credential is added, the system should automatically:

### Credential onboarding
- Encrypt and persist the credential using the credential-vault design.
- Validate it against the provider's official authentication or account/model endpoint.
- Detect provider identity, accessible models, capabilities, region, and relevant limits where the provider exposes them.
- Create or update the provider connection and associated quota pools idempotently.
- Explain invalid, expired, insufficient-scope, or unsupported credentials clearly, without leaking secrets.
- Never log, echo, or return the submitted secret after initial entry.

### Discovery and registry maintenance
- Run approved discovery sources on a schedule.
- Fetch only public, permitted sources within configured crawl budgets and policy restrictions.
- Extract candidate offers and preserve evidence and provenance.
- Verify eligibility and terms before making an offer routable.
- Reconcile changed, expired, withdrawn, or stale offers and suspend them when evidence no longer supports eligibility.
- Never treat discovery as authorization: a discovered offer cannot be used until the user has authorized the upstream connection or the provider explicitly permits public access.

### Capacity and quota management
- Refresh quota and usage data through supported provider APIs and response metadata.
- Track shared quota pools once, not once per credential or model alias.
- Distinguish advertised, verified, authorized, currently usable, consumed, and forecast capacity.
- Respect reset windows, rate limits, concurrency limits, token limits, and provider Retry-After instructions.
- Use conservative behavior when limits are unknown; unknown does not mean unlimited.
- Reconcile reservations with actual usage and record uncertainty when usage is estimated.

### Routing and recovery
- Select the strongest suitable model that satisfies task requirements, user policy, provider terms, authorization, health, and available quota.
- Use bounded retries and fallback only among eligible, authorized candidates.
- Preserve the actual model/provider identity and disclose fallback or downgrade in response metadata where appropriate.
- Apply circuit breakers, cooldowns, reset-aware rechecks, and connection revalidation automatically.
- Never silently violate a user's model, privacy, region, or provider restrictions.

### Worker lifecycle and operations
- The production application must actually start and supervise the worker process (or provide a documented, automatically deployed worker service). Merely defining worker functions is not sufficient.
- Provide reliable startup, graceful shutdown, crash recovery, and health/readiness reporting.
- Use durable, lease-coordinated jobs so multiple instances do not duplicate work.
- Renew/heartbeat long-running leases or enforce bounded execution so a live job cannot be reclaimed prematurely.
- Make jobs idempotent and retry transient failures with bounded backoff and jitter.
- Record job status, last success, next scheduled run, duration, and safe error summaries.
- Detect a stalled or missing worker and surface an operational alert. Do not silently leave background automation dormant.
- Ensure the API remains usable when a non-critical background task is temporarily unavailable; recover the task automatically.

## User experience and notifications

The default interface should answer only the questions an ordinary user cares about:
- Is my connection working?
- Is Oasis Route ready?
- Which model is currently serving my request?
- Is my available capacity healthy?
- Does something need my attention?

Use plain-language statuses such as Connected, Checking provider, Ready, Temporarily degraded, and Action required. Do not expose internal job/lease terminology in the normal user interface.

Notify users only when an event is meaningful: credential invalidation, authorization renewal, provider policy/eligibility change, exhausted capacity with no eligible fallback, or a sustained service issue. Routine discovery and quota refreshes should be silent.

Never claim that setup is complete until credential validation, connection persistence, initial model/capability sync, and the minimum routing readiness checks have succeeded. Show progress while asynchronous setup runs, and make retries safe.

## Security, consent, and spending boundaries

Autonomy is bounded by the permissions the user grants. It does not mean unrestricted access.

- Never create upstream accounts, rotate identities, farm trials, bypass verification, evade rate limits, or reset allocations through account cycling.
- Never use a provider credential for a purpose outside its documented scope or the user's authorization.
- Never enable a paid provider, purchase credits, upgrade a plan, or incur a new charge without explicit user consent.
- Do not silently opt users into data sharing, training, retention, or cross-region processing.
- Do not route prompts to a provider that conflicts with the user's privacy, region, or allow/deny policy.
- Ask for user action only when the provider requires an interactive authorization, credential renewal, consent, payment decision, or other decision that cannot safely be made on the user's behalf.
- Keep a clear audit trail of automated actions and provide controls to disconnect a provider, revoke Oasis Keys, pause automation, and delete account data.

## Implementation priorities

Treat this requirement as a cross-cutting product milestone:

1. One-time onboarding: secure credential entry, validation, automatic connection setup, initial model/capability/quota sync, and clear readiness state.
2. Always-on orchestration: wire the worker into the actual deployment and lifecycle; add durable scheduling, lease renewal, graceful shutdown, health monitoring, and automatic recovery.
3. Autonomous maintenance: discovery, offer reconciliation, quota refresh, health checks, connection revalidation, retention, and notifications run on schedules without user intervention.
4. Automatic routing: sensible defaults choose the best eligible model and perform bounded fallback without requiring manual routing configuration.
5. Quiet, transparent UX: routine work stays in the background; meaningful failures and user-consent requirements are clearly surfaced.
6. End-to-end verification: test from fresh install and one provider credential through a real or deterministic mocked request, worker restart, provider outage, quota exhaustion/reset, credential expiry, and recovery.

## Acceptance criteria

The zero-operations experience is not complete until all of the following are demonstrated:

- A new user can add one supported upstream credential and does not need to configure provider adapters, quota pools, routing rules, or worker processes manually.
- Credential validation and connection setup are automatic, idempotent, secure, and report actionable errors.
- The system automatically synchronizes accessible models, capabilities, and quota information.
- The app's documented default deployment starts the background worker without a manual command or hidden operator step.
- Worker jobs continue on schedule after application restart and recover after worker crashes.
- Long-running jobs cannot lose their lease while still legitimately executing.
- Discovery, quota refresh, health checks, connection revalidation, and offer reconciliation are observable and demonstrably running.
- A normal inference request automatically selects an eligible model and uses bounded fallback when appropriate.
- Paid usage, provider authorization, privacy choices, and policy exceptions always remain under explicit user control.
- Routine operations require no user attention; only genuine exceptions request intervention.
- Tests cover success, invalid credentials, provider outage, rate limiting, quota exhaustion/reset, stale offers, worker failure/restart, and no-eligible-route behavior.
- Documentation and UI describe the actual behavior; no background capability is advertised unless it is wired into the deployed application.

## Non-goal

Do not interpret fully autonomous as hiding all configuration or removing user control. The goal is zero routine operations, not zero consent. Users retain visibility, revocation, pause, privacy, and spending controls.
