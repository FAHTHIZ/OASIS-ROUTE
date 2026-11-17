# Agent Execution Prompt — Build Live Provider Discovery

You are the coding agent working directly in **FAHTHIZ/OASIS-ROUTE**. The repository owner has authorized direct work on the default branch. Your job is to inspect the real codebase, implement the live provider-discovery and consent-first onboarding system, run the tests, and leave the repository in a working state. **Do not stop after writing another plan.**

Read these documents before changing code:

1. `AGENT_HANDOFF.md`
2. `README.md`
3. `docs/LIVE_PROVIDER_DISCOVERY_AND_ONBOARDING.md` — the feature specification and acceptance criteria
4. `docs/DISCOVERY_AND_REGISTRY.md`
5. `docs/ZERO_OPERATIONS_AUTONOMY.md`
6. `docs/SECURITY_AND_COMPLIANCE.md`
7. `docs/SECURITY_AND_COMPLIANCE.md`
8. `docs/FREE_TIER_CAPACITY.md`
9. `docs/PROVIDER_ADAPTERS.md`
10. `docs/ROUTING_AND_QUOTAS.md`
11. `docs/OPERATIONS.md`
12. `docs/CONFIGURATION.md`
13. `docs/IMPLEMENTATION_AUDIT.md`
14. The current execution plan, ADRs, tests, migrations, and all relevant source modules.

If a listed file has moved, find its current equivalent. Do not assume the docs are perfectly current; verify every statement against actual code.

## The product target

OASIS Route should give an application **one gateway endpoint and one Oasis Key**. Behind that gateway, the product should continuously discover legitimate public free inference offers, verify their terms and evidence, explain how to connect them, securely maintain user-authorized connections, synchronize quotas, and automatically select the strongest suitable eligible model.

The Oasis Key authenticates the application to OASIS Route. It does not create upstream accounts, provider credentials, free credits, or provider entitlements. Keep upstream provider connections separate and encrypted in the existing vault.

The experience should eliminate routine provider research and routing chores. It must not remove provider-required user consent.

## Non-negotiable boundaries

Build the feature described in `docs/LIVE_PROVIDER_DISCOVERY_AND_ONBOARDING.md`.

You may discover public offers, official provider documentation, model catalogues, signup/key-console pages, API endpoints, pricing and rate-limit information, and official OAuth/device authorization paths.

You must not build account/credit farming. Specifically, do not:
- Create provider accounts or disposable email accounts automatically.
- Automate mass registration, trial cycling, identity rotation, or quota-reset evasion.
- Circumvent CAPTCHA, phone verification, paywalls, login restrictions, geoblocks, robots/terms, or rate limits.
- Harvest or test API keys found in public repositories, logs, paste sites, leaked data, or another user's content.
- Use a user's inbox as a signup/verification workaround.
- Accept provider terms, enable billing, purchase credits, or agree to data sharing on the user's behalf.
- Pool or redistribute personal provider quotas unless the provider explicitly permits that exact use and the account owner has enabled it.
- Send user prompts to a provider that is not authorized for the project's data/privacy/region policy.

Do not add a generic provider email/password scraper or browser bot that signs into account dashboards. Use official OAuth/PKCE/device flows where supported. Otherwise, guide the user to the provider's official key-creation page and let the user supply the credential securely. Keep Gmail/mail optional and behind its existing compliance gate; mail may classify narrowly authorized provider notifications, not create identities or harvest secrets.

## Work method

1. Inspect the repository, current branch, latest commits, working tree, source modules, migrations, CI, dependency files, deployment configuration, and test suite. Preserve existing work.
2. Produce a concise gap map in your own working notes, then start implementation. Do not ask the owner to repeat requirements already in this prompt or repository.
3. Reuse the existing safe fetcher, source registry, evidence model, offer lifecycle, credential vault, OAuth implementation, worker scheduler, API patterns, console, quota ledger, and provider adapters. Extend them; do not make duplicate subsystems.
4. Work in small integrated slices. Keep the app runnable.
5. After each slice, run focused tests, formatter, lint, type checks, and relevant security checks. Then run the full test suite and CI-equivalent commands.
6. Inspect the final diff and verify the actual default-branch state. Never claim a test or feature passed unless you ran it.

## Implementation priorities

### 1. Make public discovery genuinely live and extensible

- Inspect existing discovery source connectors and scheduled jobs.
- Add/expand a provider seed catalogue for the providers already supported by the adapter layer (including Google Gemini API, NVIDIA API Catalog, OpenRouter, Groq, Mistral, Cerebras, SambaNova, Hugging Face, and any other existing adapter) and official-source discovery for new candidates.
- Support official docs, pricing/rate-limit pages, developer announcements, changelogs, public model catalogues, official repositories/SDKs, permitted search/index APIs, and reviewed community submissions.
- Implement bounded frontier/link discovery, canonical URL deduplication, conditional requests, content hashing, crawl intervals, per-domain budgets, robots/terms review, and incremental refresh.
- Re-check every redirect and enforce SSRF/private-network protections. Enforce response limits, content-type allowlists, timeouts, concurrency, retries, and backoff.
- Never crawl authenticated account dashboards, private repositories, inboxes, leaked dumps, unrelated personal data, or secret stores. Do not bypass access restrictions.
- Treat all fetched content as untrusted and possibly prompt-injected. Do not execute page instructions, code, scripts, notebooks, repositories, or downloads.

### 2. Extract complete, evidence-backed offers

The normalized offer must distinguish:
- Recurring free tier.
- One-time trial credit.
- Evaluation/demo allocation.
- Promotional credit.
- Paid plan.
- Anonymous/keyless endpoint.
- User-authorized OAuth/API-key connection.

Record model IDs, endpoint/protocol, capabilities, limits (RPM/RPD/TPM/TPD/concurrency), sharing scope, reset windows, expiry, regions, age/audience and account eligibility, billing/card requirements, commercial restrictions, account/project limits, pool/share/resale terms, training/retention/data policies, and official signup/docs/terms links.

For every extracted value, store source URL, publisher, retrieval time, content hash, evidence excerpt/locator, confidence, verification method, and next review date. Unknown must remain unknown; do not invent unpublished quotas or mark missing numbers as zero.

Prefer deterministic provider-specific parsers. If an LLM is used, constrain it to a typed schema with evidence references and no credentials/tools. Extraction cannot activate an offer.

### 3. Enforce the offer state machine

Preserve the existing state machine and extend it only if required:

`DISCOVERED -> NEEDS_REVIEW -> VERIFIED_ELIGIBLE -> CONNECTABLE -> ACTIVE`

Also handle `REJECTED`, `INELIGIBLE`, `EXPIRED`, `SUSPENDED`, `STALE`, and `REVOKED`.

A public marketing page is not proof of eligibility. An eligible offer is not an authorized connection. A connected provider is not proof that quota remains. Only an explicitly permitted anonymous offer or a validated user-authorized connection can become active.

### 4. Build the connection assistant

- Inspect the existing OpenRouter OAuth/PKCE code and connection UI.
- Reuse and generalize the OAuth flow only for providers that officially support it. Validate state, PKCE, callback/issuer, requested scopes, token refresh, and error cases.
- Add official device authorization where a provider supports it.
- For API-key-only providers, display the official key-console link, explain the required scope, accept a user-provided key, validate it against a documented low-impact endpoint, encrypt it in the existing vault, and immediately clear plaintext from transient state.
- Show clear states: anonymous/ready, OAuth available, API key required, user action required, unavailable, connected, validating, ready, invalid/expired.
- Never store provider passwords or browser session cookies. Never automate account registration or accept terms/billing consent.
- A provider connection must be user/project-scoped and auditable. Disconnect/revoke must remove it from routing and destroy stored credentials according to current vault semantics.

### 5. Make free-capacity intelligence real

- Separate advertised, verified, authorized, usable, reserved, consumed, expiring, and forecast capacity.
- Keep recurring free quotas distinct from one-off credits and temporary evaluation offers.
- Model quota scope correctly (account/project/org/model-family/shared pool) so multiple keys cannot double-count one shared allowance.
- Sync quota through official quota APIs, documented usage endpoints, response headers, or observed responses. Mark console-only or non-public quota as unknown until a user-authorized measurement makes it observable.
- Use conservative estimates and confidence. Respect resets, expiry, region, eligibility, endpoint health, and account restrictions.
- Implement a true free-only routing policy. A free-only request must never fall through to a paid model. Paid usage, billing enablement, or credit purchases always require prior explicit user consent.

### 6. Make the intelligent router use the discoveries

- Use project policy and data/privacy/region requirements as hard filters.
- Rank remaining candidates by task/model capability and measured quality first; then context fit, reliability/health, latency, quota, and expiry as appropriate.
- Reserve quota atomically before dispatch.
- Use bounded, explainable failover only to a separately authorized eligible route.
- Return useful route metadata and tell the truth about provider/model selection and any downgrade.
- When no free eligible capacity exists, return a clear capacity response; never fabricate capacity or silently charge.

### 7. Wire background autonomy into the actual deployment

Use the existing durable worker/job framework. Add or improve scheduled jobs for:
- Source seed refresh.
- Due-source crawling.
- Extraction.
- Evidence/terms verification.
- Offer reconciliation and stale/expiry handling.
- Provider model/capability sync.
- Authorized connection checks.
- Quota synchronization.
- Allowed endpoint health checks.
- Capacity forecasts.

Jobs must be idempotent, leased, retry-bounded, restart-safe, observable, and controlled by crawl/provider budgets. Ensure the default deployment actually starts the worker.

### 8. Finish the console and user experience

Add a discoverable **Discover** experience with search and filters for provider, model, free/trial/paid, region, access mode, quota visibility, expiry, and freshness.

Each offer should show:
- What it offers and which models it exposes.
- Whether it is recurring free, trial, evaluation, or promotional.
- Published limits or an honest unknown label.
- Regions, eligibility, billing/card requirements, and use restrictions.
- Last verified time and source evidence.
- The official next action.

Provide one clear **Use this provider** action that launches OAuth or the official key-console flow. After authorization, automatically validate, sync models/capabilities/quota, and add the connection to the automatic routing profile. Do not expose worker internals to normal users.

Add status for last discovery run, next scan, source health, offers updated/expired, connected providers, free capacity, and action-required states. Preserve revoke, pause, delete/export, privacy, and spending controls.

### 9. Tests and security

Add unit, integration, migration, API, UI/smoke, and worker tests for at least:
- New source discovery and canonical deduplication.
- Robots/terms/budget/backoff enforcement.
- SSRF, private IPs, redirect attacks, DNS/URL edge cases, oversized content, malicious HTML, and prompt injection.
- Conflicting and stale evidence; unknown quotas; expiry and withdrawn offers.
- OAuth state/PKCE failures, callback errors, scope mismatch, token refresh, and credential redaction.
- Invalid API keys, provider outage, quota headers, and connection revocation.
- Shared project/account quota not counted twice.
- Free-only routing under quota exhaustion and proof that it cannot select paid routes.
- Policy mismatch for region, training, retention, and model restrictions.
- Worker crash/restart, lease recovery, idempotency, and source failure.
- No account creation, no trial cycling, no inbox-based signup, and no CAPTCHA/paywall/auth bypass.

Check dependency licenses and maintenance before adding libraries. Do not introduce a new search/crawler dependency if the current stack already handles the requirement safely.

## Definition of done

Do not stop with a design document or a list of tasks. Implement the highest-value complete slices in the actual application, with migrations, UI/API wiring, tests, configuration, and documentation. If the entire backlog is too large for one execution, complete coherent slices and leave an exact remaining-work list with files, tests, and blockers—never label unfinished features complete.

Before finishing:
- Run formatter, lint, type-check, unit/integration tests, migration checks, and CI-equivalent/security checks available in the repository.
- Inspect the final diff and ensure no credentials, tokens, or private user data were added.
- Update README capability/limitation statements so they reflect reality.
- Update the implementation audit and execution plan only with verified completion evidence.
- Commit the work directly to the default branch, following the existing repository policy.
- Report the commit SHA, exact completed functionality, tests actually run, results, and remaining limitations.

Start by inspecting the code now. Then implement.
