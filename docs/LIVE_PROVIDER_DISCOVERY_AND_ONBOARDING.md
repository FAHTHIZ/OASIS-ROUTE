# Live Provider Discovery and Consent-First Onboarding

**Status:** Product and implementation specification  
**Applies to:** Discovery control plane, provider registry, connection assistant, quota intelligence, worker, console, and inference router  
**Primary goal:** Make OASIS Route continuously find, verify, explain, and prepare legitimate free inference options while keeping provider authorization explicit and the user's application configuration to one Oasis Key.

## 1. Product outcome

OASIS Route should feel like one intelligent gateway, not a collection of provider setup chores.

A user should be able to:

1. Install or open OASIS Route and receive an Oasis Key for applications to call the gateway.
2. See a continuously refreshed catalogue of free and trial inference offers discovered from permitted public sources.
3. See which offers are genuinely usable without an account, which require an official sign-in/OAuth approval, which require a user-created API key, and which are not available in the user's region or circumstances.
4. Complete each required provider authorization through the provider's official flow, ideally with one click and no copying of secrets.
5. Let OASIS Route validate the connection, discover models and capabilities, measure or synchronize quota, and configure sensible routing automatically.
6. Send all application requests to one OASIS Route endpoint using one Oasis Key. OASIS Route selects the strongest suitable route that is actually eligible, authorized, healthy, and within quota.

The target is **zero routine operations, not zero consent**. A user should not have to find providers, compare model catalogues, construct fallback chains, or maintain quota schedules. A provider must still be allowed to require an account, OAuth approval, acceptance of terms, age/region eligibility, or an API key. OASIS Route must not fake those steps.

### The key distinction

- **Oasis Key:** authenticates an application to the OASIS Route gateway.
- **Provider connection:** a separately authorized account, OAuth grant, API credential, or expressly permitted public endpoint.
- **Free offer:** a provider's published offer and its eligibility/usage terms.
- **Usable capacity:** capacity currently available to this user, for this project and task, after authorization, policy, health, and quota checks.

An Oasis Key does not create upstream provider accounts, entitlements, credit balances, or rate limits. Never represent it as doing so.

## 2. Discovery promise and limits

Build the broadest practical, continuously updated discovery system—not a crawler that claims to index literally the entire internet. No finite crawler can guarantee complete internet coverage. Search engines, official catalogues, documentation sites, repositories, announcements, and curated directories have incomplete and changing coverage.

Use a source graph and multiple discovery methods:

- Official provider documentation, pricing, rate-limit, free-tier, terms, and API pages.
- Official developer portals, model catalogues, API reference pages, product changelogs, and announcements.
- Official provider GitHub organizations, SDKs, examples, and public repositories.
- Publicly accessible inference gateways and model-hosting catalogues.
- Reputable public directories as leads, never as final proof.
- Community-submitted URLs, clearly marked unverified until independently checked.
- Permitted search/index APIs and feeds. Use their supported interfaces and terms rather than scraping search result pages when prohibited.

The crawler's job is to discover **offers and evidence**, not to acquire permission or secrets.

## 3. Source discovery engine

### 3.1 Source registry

Each source must have a durable record containing:

- Stable source ID, canonical URL, publisher, source class, and provider association.
- Allowed domain and path scope; supported content types and request methods.
- Source terms/robots review state and date, plus contact/user-agent identity.
- Crawl interval, per-host rate/concurrency budget, maximum response size, timeout, and redirect policy.
- Last attempt, last success, ETag/Last-Modified, content hash, next due time, error/backoff state, and freshness.
- Parser/extractor version and the evidence records produced from the source.

A source is not permitted merely because it is reachable. If terms, robots instructions, or access rules prohibit the intended fetch, pause that source or use an authorized API/feed instead.

### 3.2 Discovery sources and frontier

Implement pluggable source connectors, not a single hard-coded list. The initial seed catalogue should include the official sites and documentation for providers already represented in the repository, including Google Gemini API, NVIDIA API Catalog, OpenRouter, Groq, Mistral, Cerebras, SambaNova, Hugging Face, and any other existing provider adapters. Also discover new candidates through official provider announcements, developer docs, and permitted public catalogues.

A discovery cycle should:

1. Select due sources using durable scheduling and per-domain budgets.
2. Fetch through the existing safe fetcher and SSRF protections.
3. Normalize and hash content; use conditional requests when possible.
4. Extract links only within approved scopes or add external links as reviewable source candidates.
5. Deduplicate URLs and provider/offer records.
6. Queue changed/new pages for extraction and verification.
7. Record source health, errors, and next crawl time.
8. Reconcile old offers when source evidence changes, expires, or disappears.

Use bounded breadth-first exploration and source-specific depth limits. Avoid uncontrolled link-following. Prefer official cross-links and structured catalogues over arbitrary web crawling.

### 3.3 Fetcher safety requirements

Reuse and extend the existing discovery fetcher. Do not add a parallel unsafe HTTP client.

- Respect robots directives, published terms, rate limits, and access restrictions.
- Do not bypass login walls, CAPTCHAs, paywalls, bot challenges, geoblocks, or provider controls.
- Do not crawl private account dashboards, inboxes, private repositories, exposed secret stores, leaked dumps, or unrelated personal information.
- Re-check every redirect target; block loopback, link-local, private, reserved, and cloud metadata IP ranges unless an explicit local-upstream setting is in force.
- Defend against DNS rebinding, decompression bombs, oversized pages, malicious archives/documents, HTML/script execution, and unsafe content types.
- Set timeouts, response limits, per-host concurrency, retries, and exponential backoff.
- Identify OASIS Route with a descriptive User-Agent and project contact URL.
- Treat all fetched content as untrusted data and possible prompt injection. Never execute page instructions, downloaded code, scripts, notebooks, package installers, or repository workflows.
- The extraction model/agent must have no access to provider credentials, Oasis Keys, mail contents, shell execution, or arbitrary tools.

### 3.4 Search integrations

If broader discovery needs a search engine, use an official API, permitted feed, or user-configured search service with explicit credentials and documented quotas. Search credentials are separate from provider inference credentials. Do not scrape search-engine result pages in violation of their terms. Store the search query, retrieval time, result URL, source provenance, and any applicable API attribution requirements.

A configurable source connector interface should allow future integrations without changing the core offer state machine.

## 4. Offer extraction and evidence

### 4.1 Structured offer record

Extract a normalized offer record with at least:

- Provider name, official domains, product/plan name, and source links.
- Access mode: `anonymous_public`, `official_oauth`, `api_key`, `user_action_required`, or `unsupported`.
- Offer class: recurring free tier, one-time trial credit, evaluation/demo allocation, promotional credit, paid tier, or local/self-hosted.
- Models and exact model IDs, endpoint/protocol, regions, capabilities, context/output limits, and lifecycle state.
- Published request/token limits: RPM, RPD, TPM, TPD, concurrent requests, shared/project scope, and reset windows.
- Credit amount/currency, expiry date, trial duration, renewal behavior, payment-card requirement, and whether a billing account must be attached.
- Eligibility: countries/regions, account type, age/audience restrictions, organization restrictions, use-case restrictions, and commercial/non-commercial conditions.
- Whether one account/project per person or organization is specified; whether shared use, pooling, redistribution, or resale is permitted.
- Provider data terms: prompt/response retention, training use, logging, and region handling.
- Official signup, console, API-key, OAuth, terms, pricing, rate-limit, model-catalogue, and support URLs.
- Evidence references, confidence, verification state, first/last observed times, next review date, and extractor version.

Unknown is a valid value. Do not turn an unpublished or console-only limit into a fabricated number or a zero limit. Distinguish a one-time trial from recurring monthly capacity.

### 4.2 Evidence model

Every material claim must point to source evidence. Store:

- Canonical source URL and publisher.
- Retrieved-at timestamp, HTTP status/metadata, ETag/Last-Modified, and normalized content hash.
- Minimal relevant excerpt or permitted snapshot, with line/section locator where possible.
- Claim type and extracted value, e.g. free-tier availability, quota, model, expiry, eligible region, or restriction.
- Verification method, reviewer/automation identity, confidence, and next review date.

Retain only what is needed to verify the claim. Honour source licences and retention policy. A search snippet or third-party directory is a lead, not authoritative confirmation.

### 4.3 Extraction and validation pipeline

Use deterministic parsers for known provider pages and structured data first. An optional LLM extractor may propose values for unfamiliar pages, but it must:

- Return a validated schema, not arbitrary prose.
- Include exact evidence references for every field.
- Use `unknown` when evidence is absent or ambiguous.
- Be unable to activate offers or create connections.
- Be unable to access credentials or call arbitrary tools.
- Be tested against prompt injection, fabricated claims, stale pages, conflicting sources, and malicious HTML.

Provider-specific validators should verify endpoint/model metadata through public, documented catalogue APIs where permitted. Make a real inference probe only for an anonymous endpoint whose terms explicitly permit it, or through a user-authorized connection. Discovery alone must never probe a protected endpoint.

## 5. Offer lifecycle and eligibility

Use a state machine with explicit transitions:

`DISCOVERED -> NEEDS_REVIEW -> VERIFIED_ELIGIBLE -> CONNECTABLE -> ACTIVE`

Also support `REJECTED`, `INELIGIBLE`, `EXPIRED`, `SUSPENDED`, `STALE`, and `REVOKED`.

Rules:

- Extraction creates `DISCOVERED` or `NEEDS_REVIEW`, never `ACTIVE`.
- `VERIFIED_ELIGIBLE` requires adequate evidence for material terms.
- `CONNECTABLE` means a valid official access method exists; it does not mean the user's account is connected.
- `ACTIVE` requires either an explicitly permitted anonymous endpoint or a validated user-authorized provider connection, plus healthy endpoint and available quota.
- A material terms change, missing source, expired credit, region mismatch, invalid credential, or unclear eligibility must suspend the affected offer until re-verified.
- Keep an audit trail for state transitions and who/what caused them.

## 6. Consent-first provider connection assistant

The connection assistant should eliminate unnecessary manual work without impersonating the user.

### 6.1 Access modes

For each offer, the UI should explain one of these states:

1. **Ready without sign-in** — an anonymous/public endpoint is documented as permitting this automated use. OASIS Route may validate it within published limits.
2. **Connect with provider** — the provider supports an official OAuth/PKCE/device authorization flow. Open the provider's authorization page, use state/PKCE, verify redirect and issuer/audience, request least-privilege scopes, and store refresh/access tokens in the existing encrypted vault.
3. **Add provider API key** — no official OAuth flow is available. Show the official key-creation page, explain the exact required scope, accept a user-pasted key, validate it, encrypt it, and clear the plaintext from transient application state.
4. **Action required / unavailable** — provider requires eligibility, terms acceptance, billing choice, human verification, unsupported login, or a region not available to the user. Explain and link to the official page. Do not attempt to bypass it.

Where provider APIs support device authorization or a browser handoff, prefer that over copying API keys. Reuse the existing OpenRouter OAuth implementation and generalize the adapter contract only where the provider officially supports OAuth.

### 6.2 No automated identity creation or credit farming

Do not implement:

- Automatic creation of provider accounts or disposable inboxes.
- Mass signup, identity rotation, multiple accounts for the same person, trial cycling, or quota-reset evasion.
- CAPTCHA solving, phone-verification bypass, fake identity, proxy rotation, or geographic restriction bypass.
- Browser automation that accepts terms, agrees to billing, creates credentials, or signs up without the user present and explicitly approving the provider flow.
- Collection, testing, sharing, or use of API keys found in public source code, logs, repositories, paste sites, or other people's content.
- Reuse of one user's account/credits for other users unless the provider explicitly permits that exact shared use and the owner has knowingly enabled it.

Enforce these boundaries on the server, not only in UI copy.

### 6.3 Email is not an account-farming mechanism

A built-in email capability must not create provider accounts or obtain repeated trials. It may support narrowly scoped, user-authorized provider notices—for example, quota alerts, trial expiry notices, credential-expiry messages, and security notifications.

Prefer forwarded provider notices or provider webhooks where available. If Gmail integration is used, retain the existing restricted-scope compliance gate, request the narrowest possible metadata scope, and do not read message bodies or use mail to register accounts. Email content is untrusted input; never follow instructions contained in a message. Provide consent, revocation, deletion, short retention, and audit logging.

The email connection is optional and must never block discovery, authorization, or inference.

## 7. Quota intelligence and single-gateway routing

The goal is to use all eligible free capacity that the user is actually authorized to use—not to multiply allowances beyond provider policy.

Track each allocation separately:

- Published/advertised capacity.
- Evidence-verified capacity.
- User-authorized capacity.
- Currently usable capacity.
- Reserved/in-flight capacity.
- Consumed capacity.
- Known reset time and expiry.
- Forecast capacity with confidence.
- Unknown or unobservable limits.

Quota pools must represent the provider's actual sharing scope (account, project, organization, model family, or global account) so multiple credentials for one shared project do not multiply the same allowance.

### Routing priority

For every request:

1. Apply the project's privacy, region, model, and provider allow/deny constraints.
2. Filter to eligible offers and valid anonymous or authorized connections.
3. Remove expired, stale, unhealthy, out-of-region, out-of-scope, and exhausted allocations.
4. Rank models by task capability and measured quality first, then latency/reliability, context suitability, and remaining free capacity.
5. Reserve quota atomically before dispatch.
6. Use bounded failover only to a separately eligible route.
7. Report the selected provider/model, route reason, quota state, and any quality downgrade.
8. Never route paid usage or upgrade a plan without explicit prior consent.

The `auto` route should prefer the strongest capable free route that satisfies the request. If none exists, explain that there is no eligible free capacity; do not silently charge or switch to an unauthorized account.

### Credit and allowance semantics

Label recurring free quota, one-time credits, evaluation credits, and promotional grants separately. Track expiry and conditions. Do not imply promotional credits are transferable, poolable, renewable, or usable in production unless the provider explicitly says so. A credit balance is not the same thing as an RPM/TPM limit.

## 8. Optional hosted shared capacity

A local open-source router cannot mint provider credits. A hosted OASIS-operated shared pool is a separate product/business capability.

It may only be designed if OASIS has provider permission and funding for that shared use. Before implementation, require documented provider authorization for pooling/resale/redistribution, explicit user terms, budget controls, per-user quotas, abuse detection, isolation, billing disclosures, and an operator kill switch. Do not pool personal trial allocations merely because multiple users can connect accounts.

This option is not a shortcut around provider restrictions and is not required for the self-hosted router.

## 9. UX requirements

The console should have a simple **Discover** / **Connections** experience:

- Global discovery status: last successful scan, next scan, source health, offers found/changed/expired, and any crawl limitation.
- Search and filters: provider, model, free/paid/trial, region, access method, use restrictions, quota visibility, and freshness.
- Offer cards: model names, free-tier/trial distinction, published limit or “not published”, expiry, regions, requirements, terms summary, evidence date, and official links.
- One prominent action: **Use this provider**. It launches the correct OAuth flow or guides the user to the official key page.
- Clear states: Discovered, Needs review, Verified, Ready to connect, Connected, Ready, Exhausted, Expiring, Stale, or Action required.
- Connection progress that reports validation and model/quota synchronization.
- An automatically generated routing profile after the first eligible connection.
- A route preview that explains which model would be selected and why.
- A quiet background status area; routine crawling and synchronization should not interrupt normal work.
- Controls to pause discovery, disconnect a provider, revoke credentials, export data, delete data, and disable any connection.

Never present a provider as “free and ready” solely because a marketing page uses the word free.

## 10. Worker jobs and observability

Extend the existing durable worker and job registry. Suggested jobs:

- `discovery.seed_sources`: refresh official source catalogue.
- `discovery.frontier`: fetch due approved sources within budget.
- `discovery.extract`: parse changed pages and create candidate offers.
- `discovery.verify`: recheck evidence and policy claims.
- `discovery.reconcile`: detect stale/expired/withdrawn offers and update state.
- `providers.catalog_sync`: sync models/capabilities for authorized connections.
- `providers.connection_check`: revalidate authorization with documented low-impact checks.
- `providers.quota_sync`: ingest documented quota APIs/headers and reconcile usage.
- `providers.health_check`: probe only endpoints that may lawfully be probed.
- `capacity.forecast`: recompute capacity and route eligibility.

Jobs must be idempotent, leased, retry-bounded, observable, and safe across restarts. Respect per-source and per-provider budgets. Add metrics for fetch outcomes, crawl depth, source freshness, extraction confidence, review queue size, stale offers, connection success, quota sync age, free capacity, and failover reasons. Never put secrets, prompts, or raw email content in logs or metrics.

## 11. Data model and API surface

First inspect existing models, migrations, services, and endpoints. Extend the current domain model; do not create duplicate provider/offer/connection/quota concepts.

Likely entities or extensions:

- `DiscoverySource`: source scope and crawl policy.
- `DiscoveryRun`: run metadata, budget use, and outcome.
- `Evidence`: provenance, claim, excerpt/hash, confidence, and review.
- `Provider` / `Model`: canonical identities and capability metadata.
- `Offer`: access mode, offer type, terms, limits, expiry, eligibility, evidence, and lifecycle state.
- `Connection`: user/project-scoped authorization reference and validation state.
- `QuotaPool`: actual provider allocation scope, limit, reset, and observed use.
- `ChangeEvent`: evidence diff and required user/operator action.

Expose authenticated APIs consistent with existing patterns for:

- Listing/searching offers and their evidence.
- Viewing source freshness and discovery status.
- Triggering or pausing an authorized discovery run (admin-scoped).
- Starting/callback/completing supported OAuth flows.
- Adding, validating, disconnecting, and revoking a provider connection.
- Reading the project's usable capacity and route preview.

Do not leak secrets or make untrusted content executable through the API. Add pagination, per-user/project authorization, rate limits, audit events, and response schemas.

## 12. Implementation sequence

Work from the existing codebase and current execution plan. Do not rewrite working modules.

**Slice A — audit and gap map**
- Read the repository documents and inspect all existing discovery, fetcher, extractor, source registry, worker, offer state machine, vault, OAuth, provider adapters, quota, console, and tests.
- Map each requirement in this document to existing code, partial code, or missing work.
- Correct stale docs and add an ADR only for genuine architectural decisions.

**Slice B — source discovery and evidence**
- Improve the source seed catalogue and public-source connectors.
- Implement bounded frontier exploration, conditional fetch, parsing, evidence provenance, deduplication, and change detection.
- Add fixtures and tests for hostile/untrusted pages, injection, redirects, robots, crawl limits, and stale sources.

**Slice C — offer intelligence**
- Add the complete offer schema for access mode, free/trial class, quota, expiry, region, restrictions, billing conditions, and evidence.
- Add deterministic provider extractors for the existing adapter catalogue.
- Add review and reconciliation; extraction must not activate offers.

**Slice D — connection assistant**
- Inventory official OAuth/API-key options for each provider.
- Generalize official OAuth/PKCE/device flows where actually supported; preserve provider-specific differences.
- Improve the UI so the user sees the official access path and only takes actions the provider requires.
- Keep credentials encrypted and provider-scoped; do not use a shared mailbox or generic login/password store.

**Slice E — quota-aware intelligence**
- Reconcile advertised and observed limits without guessing.
- Prevent duplicate/shared pools from being counted twice.
- Improve task-fit and quality ranking while preserving project privacy policies and explicit free-only mode.
- Make automatic failover explainable and bounded.

**Slice F — operations and hardening**
- Wire every scheduled job into the default deployment and test crash/restart behavior.
- Add operational metrics, source controls, retention, abuse protection, rate limits, and audit logs.
- Run formatting, lint, type-check, full tests, security/dependency checks, and production smoke tests where available.

## 13. Acceptance criteria

This feature is not complete until:

- A fresh install automatically runs its scheduled public discovery process without a user manually finding sources.
- The source catalogue is extensible and includes existing providers plus newly discovered official providers.
- Every discovered claim has source provenance, retrieval time, confidence, and a freshness state.
- The crawler obeys source policy, robots, crawl budgets, SSRF protections, and backoff.
- The system can discover a provider without falsely treating the offer as authorized or usable.
- The UI distinguishes keyless, official OAuth, API-key, and unavailable access paths.
- The user can complete supported OAuth flows with PKCE/state validation; API-key fallback is clear and secure.
- No account is created, no trial is cycled, no CAPTCHA is bypassed, and no provider terms are accepted for the user.
- The system never accesses an inbox or credential source outside a specific, informed, user-authorized integration.
- Free recurring quota, one-time credits, trials, and evaluation allocations are represented separately.
- Shared account/project quota is counted once and reservations are concurrency-safe.
- The router prefers the strongest suitable eligible free route and never silently incurs paid usage.
- Provider disconnection/revocation removes the allocation from routing and destroys stored credentials according to the vault design.
- All worker jobs recover from crashes and expose health/freshness metrics.
- Tests cover malicious pages, stale/contradictory evidence, expired offers, OAuth failures, invalid credentials, shared quota, exhausted capacity, paid-route blocking, and worker restarts.
- README and UI claims match the implemented behavior; no “all internet” or “unlimited free credits” guarantee is made.

## 14. Definition of the desired user experience

The user configures their application once:

```bash
export OASIS_API_KEY="your-oasis-key"
```

The user's application calls one gateway. OASIS Route continuously discovers offers, explains required authorization, maintains connections the user has approved, synchronizes quota, chooses the strongest suitable eligible model, and fails over safely.

The user should not have to hunt the internet for free providers or maintain routing and quota spreadsheets. The system does that work. The user's account, provider consent, private credentials, and spending decisions remain theirs.
