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
- The project's spend policy is `free_only` and the pair is not provably free (see §2.1).

### 2.1 Spend policy: never silently incur paid usage

Every project has a `spend_policy`. New projects start as **`free_only`**; projects that existed before
migration 0006 were backfilled as `allow_paid` so upgrades do not change behaviour.

Under `free_only` a (connection, model) pair is routable only when its `cost_basis` is one of:

| `cost_basis` | Meaning |
|---|---|
| `free_model` | The provider's own catalogue or authenticated model list prices the model at zero (`Model.pricing.free`, e.g. OpenRouter `:free` variants). Cleared automatically when the model is delisted as free. |
| `free_tier_account` | The upstream account cannot be billed: the owner declared `billing_mode: free_tier`, or the provider reported it (OpenRouter `GET /api/v1/key` `is_free_tier: true`). |
| `local` | A local development mock. |

Everything else is rejected with a visible reason: `paid_route_blocked` (connection is `billing_mode: paid`;
the default for billed-only providers such as OpenAI and Anthropic) or `cost_unknown`. Route previews and
`/projects/{id}/status` surface this ("Free-only mode is on and no connected model is confirmed free").

- Switching to `allow_paid` requires `acknowledge_paid_usage: true` and is audited as
  `project.spend_policy_changed`.
- A request may tighten the policy with `"oasis_route": {"free_only": true}`; it can never loosen it.
- Provider-reported account status may only fill an `unknown` billing mode or revoke a stale `free_tier`
  (when the account has purchased credits); it never relaxes a user-declared `paid`.

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

### Unrated models and name-based priors

Models discovered from a provider's `/models` listing carry no quality evidence, and Oasis Route never invents benchmark scores. Every rated model ranks ahead of every unrated one. Unrated models are then ordered among themselves by a deterministic, clearly labelled heuristic prior (`src/oasis_route/routing/priors.py`) derived only from the identifier:

- total parameter count parsed from the name (`70b`, `8x7b`; active-parameter suffixes such as `a22b` are ignored), or a short table of widely published open-weight sizes (DeepSeek R1/V3, Kimi K2, GLM-4.5), scored on a log scale;
- tier words (`opus`/`ultra`/`max`, `pro`/`large`, versus `mini`/`small`/`lite`/`nano`, `flash`);
- a small newer-generation bonus (`llama-3.3` over `llama-3.1`, release-date suffixes ignored);
- a task bonus for specialists (`coder`/`codestral` for `coding`, `r1`/`thinking`/`o3` for `reasoning`, `vl`/`vision` for `vision`).

A prior is an ordering hint, not evidence: it is reported as `prior` with `prior_source` in route previews and as `quality_prior` in `/v1/models`, it never satisfies `min_quality`, and it never makes a fallback look like a non-downgrade. Operators who want exact ordering attach evidence with `PATCH /api/v1/admin/models/{model_id}`, which immediately takes precedence.

A strict quality-first mode should exhaust eligible capacity at the highest suitable quality tier before descending. A balanced mode may trade a small quality difference for reliability or latency. Expose the mode to users.

### 3.1 Routing strategies

A strategy only orders routes that already passed every gate (authentication, authorization, verification, eligibility, budget, quota, health, capability, privacy). It never admits a route a gate rejected, so `lowest_cost` cannot reach a paid route under `free_only`.

| `routing_mode` | Orders by |
|---|---|
| `strict_quality` (default) | Quality evidence; cost and latency break ties. |
| `balanced` | Quality tier, then health and quota headroom. |
| `lowest_cost` | Estimated worst-case cost: free first, then verified prices, then estimated prices; unknown cost last. |
| `most_quota` | Largest remaining fraction of the tightest quota window; unmetered last. |
| `lowest_latency` | Observed latency (EWMA); unobserved last. |
| `reliability` | Observed success rate. |
| `context_fit` | Smallest context window that still fits the request, so large-context routes stay free for large requests. |
| `reset_aware` | Routes whose quota resets soonest first, spending capacity that would otherwise expire unused. |
| `diversity` | Quality order, then interleaved one route per provider so consecutive fallbacks hit different providers. |

Set it per project (`PATCH /api/v1/projects/{id}`) or per request (`oasis_route.mode`). A pinned model keeps its own order.

### 3.2 Explanations

Every route preview and persisted decision carries an `explanation` (see `routing/explain.py`): the selected route and reason, alternatives with their ranking factors, rejected routes grouped by reason and gate, verification (cost basis, free evidence, whether the price is verified), quota state, estimated and actual cost, and the fallback chain (planned order and attempts). `GET /api/v1/projects/{id}/route-decisions/{request_id}` returns it for a past request.

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
- RATE_LIMITED: temporary request/token window restriction; respect Retry-After. The cooldown applies to that model on that connection, because most providers enforce limits per model. If a second model on the same connection is rate limited while the first is still cooling down, the limit is evidently account-wide and the whole connection cools down as well. Within one request the router may try other models on the same account, but never another account of the same provider unless the provider permits multiple accounts.
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
