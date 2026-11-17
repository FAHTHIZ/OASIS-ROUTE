# Routing, Quotas, and Recovery

## 1. Routing objective

Select the strongest **suitable and eligible** model for each request, not simply the model with the highest generic leaderboard score. Task fit and hard constraints precede preference ranking.

## 2. Candidate filtering

Reject a candidate before scoring if any of these apply:
- No user-authorized connection or no explicitly permitted public access.
- Offer is expired, suspended, unverified, or ineligible.
- Provider terms do not permit the intended use.
- Model lacks required modality, context window, tool calling, structured output, or other required feature.
- Region, age/audience, data residency, or privacy policy conflicts.
- Provider is unhealthy, in cooldown, or has no reservable quota.
- Request exceeds model or provider limits.

## 3. Quality-first ranking

Use a configurable task-specific quality score based on maintained benchmark evidence and declared capabilities. Benchmarks must record source, date, methodology, model version, and confidence. Do not invent a single universal intelligence score.

Suggested lexicographic ranking:
1. Hard policy and task suitability (must pass).
2. User's explicit model/provider preferences.
3. Task-specific quality tier/score.
4. Reliability and recent success rate.
5. Latency and throughput.
6. Quota scarcity and reset horizon.
7. Cost (free eligible capacity may be preferred only after the preceding constraints).

A strict quality-first mode should exhaust eligible capacity at the highest suitable quality tier before descending. A balanced mode may trade a small quality difference for reliability or latency. Expose the mode to users.

## 4. Quota model

Represent limits independently:
- requests per minute/hour/day/month;
- input tokens per minute/day/month;
- output tokens per minute/day/month;
- total tokens per period;
- concurrent requests;
- context/input size;
- hard credit balance and expiry;
- rolling-window or calendar reset schedule.

A quota pool is the accounting unit. Multiple credentials that draw from the same provider/project/team budget must reference the same pool. Never sum duplicate views of one shared allocation.

Each pool has:
- provider and provider account/project reference (non-secret);
- scope (model, project, organization, endpoint, or shared);
- limit and unit;
- period and reset rule/time zone;
- observed remaining amount;
- source of truth (provider API, response headers, user declaration, estimate);
- confidence and last verified time;
- reservation total and reconciliation drift;
- eligibility and policy reference.

Unknown values remain unknown. Do not infer unlimited quota from a missing header.

## 5. Atomic reservation

Before dispatch, reserve estimated input plus a configurable output budget in a database transaction. Use row locking or an equivalent serializable/atomic mechanism. Reservation records must be idempotent by request ID.

On completion:
- reconcile with provider usage if reported;
- otherwise use a labelled estimate;
- release unused reservation;
- record overage/drift and reduce confidence;
- never count estimated tokens as provider-confirmed usage.

For concurrency, reservations must be made against the shared quota-pool row, not individual credential rows.

## 6. Failure classification

Normalize upstream errors into:
- QUOTA_EXHAUSTED: pool depleted until reset or replenishment.
- RATE_LIMITED: temporary request/token window restriction; respect Retry-After.
- AUTH_INVALID / AUTH_EXPIRED: suspend connection pending user action.
- PROVIDER_UNAVAILABLE: transient provider outage.
- MODEL_UNAVAILABLE: model removed or endpoint unavailable.
- REQUEST_UNSUPPORTED: candidate cannot satisfy request; do not retry same candidate.
- POLICY_REJECTED: stop; do not evade the provider decision.
- UNKNOWN_UPSTREAM: bounded retry only when safe and idempotent.

## 7. Fallback policy

- Use a finite retry budget and maximum provider hops per request.
- Respect Retry-After and provider cooldown.
- Do not retry non-idempotent operations without a safe idempotency mechanism.
- Do not retry policy/authentication rejection across alternate identities.
- Preserve streaming semantics and cancellation.
- Tell the caller which model actually served the request and whether a fallback occurred, subject to safe metadata exposure.
- If no candidate meets required quality/capability, return a clear unavailable response instead of silently downgrading.

## 8. Reset and recovery

A reset schedule makes a pool eligible for recheck, not automatically usable. Refresh from provider quota APIs/headers or a safe validation request. Re-enable after health and quota checks. Repeated failures increase cooldown using bounded exponential backoff with jitter.

## 9. Capacity reporting

Publish distinct values:
- **Advertised:** provider's published statement, not verified.
- **Verified:** independently checked offer and terms; may not yet be connected.
- **Authorized:** connected by user and eligible for their use.
- **Currently usable:** available now after reservations, health, limits, and expiry.
- **Consumed:** provider-confirmed usage, separated from estimates.
- **Forecast:** modelled future capacity with assumptions and confidence.

Monthly capacity must not be summed with daily capacity without normalizing periods. One-time credits must not be presented as recurring. Input and output token pools must remain separate if provider limits separate them.
