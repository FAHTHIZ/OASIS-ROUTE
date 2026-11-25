# API reference

Oasis Route serves three surfaces from one process:

| Prefix | Purpose | Auth |
|---|---|---|
| `/v1` | OpenAI-compatible inference gateway | Oasis Key with the `inference` scope |
| `/api/v1` | Management API (connections, keys, capacity, status, discovery) | Oasis Key with `read` or `manage` scope; `/admin/*` requires an instance administrator |
| `/healthz`, `/readyz`, `/metrics` | Operations | none (keep `/metrics` private) |

The live, machine-readable schema is served at `/openapi.json` with interactive docs at `/docs`. This page is the human guide and the compatibility contract.

## Versioning

- `/v1` and `/api/v1` are stable. Within a major version, fields and endpoints are only added, never removed or renamed, and new optional request fields default to the previous behaviour.
- Breaking changes require a new prefix (`/v2`, `/api/v2`), announced in [CHANGELOG.md](../CHANGELOG.md) with at least one minor release of overlap.
- Unknown request fields on `/v1/chat/completions` that Oasis Route cannot pass through safely are rejected with `400 unsupported_parameter` instead of being silently dropped.

## Authentication: the Oasis Key

```bash
export OASIS_API_KEY="oasis_live_..."
curl http://localhost:8080/v1/models -H "Authorization: Bearer $OASIS_API_KEY"
```

An Oasis Key belongs to one project and carries scopes:

| Scope | Allows |
|---|---|
| `inference` | `/v1/*` |
| `read` | `GET` management endpoints for the key's project |
| `manage` | Everything in `read`, plus creating/changing connections, keys, pools and project settings |

Keys are shown once at creation, stored only as an HMAC, and can be rotated (`POST /api/v1/projects/{id}/keys/{key_id}/rotate`) or revoked (`DELETE`). An Oasis Key is never an upstream provider credential: provider keys are entered once on a connection and kept encrypted in the vault, and no endpoint returns them.

## Inference gateway

### `POST /v1/chat/completions`

OpenAI Chat Completions request and response format, including `stream: true` (Server-Sent Events), tools, and `response_format`. Use `"model": "auto"` to let the router pick the strongest suitable model, or name a model (`"mock-large"`, or `provider/model`) to pin it.

Optional routing controls go in an `oasis_route` object:

```json
{
  "model": "auto",
  "messages": [{"role": "user", "content": "Write a binary search in Go"}],
  "oasis_route": {
    "task": "coding",
    "mode": "strict_quality",
    "min_quality": 70,
    "allow_fallback": true,
    "allow_downgrade": false,
    "providers": ["mock", "mock_alt"],
    "require": ["tools"]
  }
}
```

| Field | Default | Meaning |
|---|---|---|
| `task` | `general` | `general`, `coding`, `reasoning`, `creative`, `extraction`, `vision`. Selects which quality evidence ranks models. The model names `auto/coding`, `auto/reasoning`, ... are shorthand for `"model": "auto"` plus this task, for clients that can only set a model name. |
| `mode` | project `routing_mode` | `strict_quality`: strongest first, cost and latency only break ties. `balanced`: quality still dominates, with more weight on health and headroom. Strategies (after every policy gate): `lowest_cost`, `most_quota`, `lowest_latency`, `reliability`, `context_fit`, `reset_aware`, `diversity`; see [ROUTING_AND_QUOTAS.md](ROUTING_AND_QUOTAS.md#31-routing-strategies). |
| `min_quality` | none | Never route below this score. |
| `allow_fallback` | `true` | Fail over to other eligible candidates on retryable errors. |
| `allow_downgrade` | project `allow_downgrade` | Allow falling back to a lower-quality model. A project setting of `false` cannot be overridden per request. |
| `providers` | all | Restrict routing to these provider ids. |
| `free_only` | project `spend_policy` | `true` restricts this request to provably free capacity. It can only tighten a project's policy, never loosen it (see [ROUTING_AND_QUOTAS.md](ROUTING_AND_QUOTAS.md#21-spend-policy-never-silently-incur-paid-usage)). |
| `require` | inferred | Extra capabilities to require (`tools`, `vision`, `json_mode`, ...). Required features are also inferred from the request itself. |

Every successful response says who actually served it, both in headers and in the body:

```http
x-oasis-route-provider: mock_alt
x-oasis-route-model: mock-large
x-oasis-route-fallback: false
x-oasis-route-downgraded: false
x-oasis-route-attempts: 1
x-request-id: req_...
```

The body's `oasis_route.route` object summarises the decision (`strategy`, `reason`, `cost_basis`, `free`, `estimated_cost_usd`, `actual_cost_usd`, `fallback_chain`) and links to the full explanation at `explain`.

```json
"oasis_route": {
  "request_id": "req_...",
  "provider": "mock_alt",
  "model": "mock-large",
  "fallback": false,
  "downgraded": false,
  "attempts": [{"provider": "mock_alt", "model": "mock-large", "outcome": "success", "category": null, "latency_ms": 41}]
}
```

For streams, the first chunk carries `oasis_route.provider` and `oasis_route.model`, and the response headers carry the same values. Failover happens only before the first byte is sent; a stream that fails midway ends with an `error` event followed by `data: [DONE]`, and is never spliced with another model's output.

**Idempotency.** Send `Idempotency-Key: <unique string>` on non-streaming requests to make retries safe. A repeat with the same key and body returns the stored response with `idempotent-replayed: true`. The same key with a different body gets `409 idempotency_key_reused`, and a repeat while the first is still running gets `409 idempotency_in_progress`. Keys are kept for 24 hours.

**Request ids.** Send `x-request-id` (8 to 64 alphanumeric characters or dashes) to correlate logs; otherwise one is generated. It is echoed in the response headers and in every error body.

### Errors

Management endpoints (`/api/v1`) and authentication failures use `{"error": {"code", "message", "request_id", "details"}}`.

Gateway errors use the OpenAI error envelope plus routing metadata:

```json
{
  "error": {"message": "...", "type": "rate_limit_error", "code": "capacity_exhausted"},
  "oasis_route": {"request_id": "req_...", "attempts": [...]}
}
```

| HTTP | `code` | When |
|---|---|---|
| 400 | `invalid_request`, `invalid_json`, `unsupported_parameter`, `invalid_idempotency_key` | The request itself is wrong. Not retried upstream. |
| 400 | provider policy category | An upstream refused the content on policy grounds. Not retried on other providers. |
| 401 / 403 | `unauthorized` / `forbidden` | Missing, invalid, revoked or under-scoped Oasis Key. |
| 404 | `model_not_found` | A pinned model is unknown or not reachable through this project's connections. |
| 409 | `idempotency_key_reused`, `idempotency_in_progress` | See idempotency. |
| 413 | `payload_too_large` | Body over `OASIS_ROUTE_MAX_REQUEST_BYTES`. |
| 429 | `rate_limited` | The Oasis Key exceeded its per-minute limit. `Retry-After` is set. |
| 429 | `capacity_exhausted` | Every eligible candidate is out of quota or cooling down. `Retry-After` is the earliest known recovery time. |
| 502 | `upstream_failed` | Every attempt failed with upstream errors. |
| 503 | `no_eligible_route` | Nothing eligible can serve the request (no active connection, policy excludes all candidates, required capability missing). |
| 503 | `project_disabled` | The project kill switch is on. |

### `POST /v1/responses` (OpenAI Responses API)

OpenAI Responses request and response format, for `client.responses.create` in the OpenAI SDKs and for tools such as Codex CLI (`wire_api = "responses"`). Requests are translated to the internal chat-completion shape and routed exactly like `/v1/chat/completions` (same quota, health, fallback, audit and `x-oasis-route-*` headers), so any connected provider can serve them.

- Translated: `input` as a string or as items (`message` with `input_text` / `output_text` / `input_image` parts, `function_call`, `function_call_output`), `instructions`, `developer` and `system` roles, function `tools`, `tool_choice`, `text.format` (`json_object`, `json_schema`), `max_output_tokens`, `temperature`, `top_p`, `user`, and `stream: true` (`response.created`, `response.in_progress`, `response.output_item.added/done`, `response.content_part.added/done`, `response.output_text.delta/done`, `response.function_call_arguments.delta/done`, then `response.completed`, `response.incomplete` or `response.failed`, each with a `sequence_number`).
- Stateless: responses are not stored. `previous_response_id` and `background` return `400`; send the whole conversation in `input` (Codex does this). `GET /v1/responses/{id}` is not served.
- Not translated: hosted tools (`web_search`, `file_search`, `code_interpreter`, computer use) are dropped because no other provider can run them; `input_file` parts and `item_reference` items return `400`; `reasoning` items in `input` are ignored and responses contain no reasoning items.
- A response truncated by `max_output_tokens` has `status: "incomplete"` and `incomplete_details.reason: "max_output_tokens"`. Errors use the OpenAI error shape, as for chat completions.

### `POST /v1/messages` (Anthropic Messages API)

Anthropic Messages request and response format, for Anthropic SDKs and tools such as Claude Code. Requests are translated to the internal chat-completion shape and routed exactly like `/v1/chat/completions` (same quota, health, fallback, audit and `x-oasis-route-*` headers), so any connected provider can serve them. Authenticate with `x-api-key: <Oasis Key>` or `Authorization: Bearer <Oasis Key>`.

- Translated: `system` (string or text blocks), text and image (`base64`/`url`) content, `tool_use` / `tool_result` blocks, custom `tools` with `input_schema`, `tool_choice` (`auto`, `any`, `tool`, `none`), `stop_sequences`, `temperature`, `top_p`, `max_tokens`, `metadata.user_id`, and `stream: true` (`message_start`, `content_block_start/delta/stop` with `text_delta` and `input_json_delta`, `message_delta`, `message_stop`).
- Model names: a `claude-*` name that is not a connected model is routed as `auto`, so clients with hard-coded Claude names work against any provider. Other unknown names return `404 not_found_error`. `auto/<task>` works here too, and so does an `oasis_route` object.
- Not translated: Anthropic server tools (web search, computer use), documents/PDF blocks, prompt-cache controls. Thinking blocks in history are dropped, and responses contain no thinking blocks.
- Errors use the Anthropic shape: `{"type": "error", "error": {"type": "invalid_request_error", "message": "..."}}`.

### `POST /v1/messages/count_tokens`

Returns `{"input_tokens": N}` for a Messages request, using the same estimate the router uses for admission (about four characters per token plus per-message overhead). This is an estimate, not the upstream tokenizer.

### `GET /v1/models`

Lists models reachable through the project's active connections, plus `auto` and the `auto/<task>` aliases, in OpenAI list format. Each model's `oasis_route` object carries `quality_general` and `quality_source` when quality evidence exists, otherwise `quality_prior`: the heuristic ordering hint described in [Routing](ROUTING_AND_QUOTAS.md#unrated-models-and-name-based-priors).

## Status and automation

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/projects/{id}/status` | Plain-language readiness: `Ready`, `Checking provider`, `Temporarily degraded`, `Action required` or `Not connected`, with a setup checklist (credential validated, models synchronized, routing ready, Oasis Key issued), the model the next request would use, the last model that served one, capacity (`Healthy`, `Exhausted`, `Unknown`), items needing attention, and background maintenance state. |
| `PUT` | `/api/v1/projects/{id}/automation` | `{"paused": true}` pauses background credential checks, health probes and quota refresh for the project. Routing continues. |
| `GET` | `/api/v1/admin/jobs` | Administrators: live workers, and per job the last success, duration, next run, consecutive failures and last error. |
| `POST` | `/api/v1/admin/jobs/{name}/{pause\|resume\|run}` | Administrators: pause, resume or trigger a job. |

## Management API

All paths are under `/api/v1`. Project-scoped endpoints require the key's own project unless the caller is an administrator.

### Identity and keys

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/bootstrap` | Create the first administrator with `OASIS_ROUTE_ADMIN_BOOTSTRAP_TOKEN`. Disabled once any user exists. |
| `GET` | `/me` | The calling user, project and key. |
| `GET`, `POST` | `/users` | Administrators list and create users. A new user gets a project and an Oasis Key. |
| `PATCH` | `/users/{user_id}` | Administrators update a user (`is_active`, `country`, `declared_age`, `display_name`). |
| `GET`, `POST` | `/projects` | List or create projects. |
| `GET`, `PATCH` | `/projects/{id}` | Read or change project routing settings (see [CONFIGURATION.md](CONFIGURATION.md#per-project-settings)). |
| `POST` | `/projects/{id}/members` | Add a member to a project. |
| `GET`, `POST` | `/projects/{id}/keys` | List keys, or create one: `{"name", "scopes", "expires_in_days"}`. The response contains `secret` exactly once. |
| `POST` | `/projects/{id}/keys/{key_id}/rotate` | Issue a replacement key and revoke the old one. |
| `DELETE` | `/projects/{id}/keys/{key_id}` | Revoke a key. |
| `GET` | `/projects/{id}/audit`, `/audit` | Audit trail for a project, or the whole instance (administrators). |
| `GET` | `/projects/{id}/export` | Project owners: download everything stored for the project (settings, members, key metadata, connection metadata, pools, usage, routing decisions, notifications, audit). Never contains Oasis Key hashes, provider credentials or OAuth material. |
| `DELETE` | `/projects/{id}` | Project owners: body `{"confirm": "<project id>"}`. Destroys every stored provider credential, revokes the project's Oasis Keys and deletes its data. Irreversible. Returns counts of what was removed. |
| `GET` | `/users/{id}/export` | The user themself or an administrator: profile, memberships, notifications, mail grants, audit events they performed, and a full export of every project they own. |
| `DELETE` | `/users/{id}` | The user themself or an administrator: body `{"confirm": "<user id>"}`. Deletes the account, its owned projects, its keys, mail grants and stored credentials. `409` if an owned project has other members or if it is the last active administrator. |

### Providers, models and connections

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/providers` | Provider registry: terms, data policy, regions, eligibility rules, endpoints. |
| `PATCH` | `/admin/providers/{provider_id}` | Administrators update provider facts and status. |
| `GET` | `/models` | Model catalogue with capabilities, context window, quality evidence and lifecycle. |
| `PATCH` | `/admin/models/{model_id}` | Administrators update model facts and quality evidence. |
| `POST` | `/projects/{id}/connections` | Connect a provider with the user's own credential: `{"provider_id", "api_key", "base_url", "display_name", "upstream_account_ref", "accept_terms"}`. Validated immediately (non-billable model listing), models synchronized, and eligibility checked; the response reports the result. |
| `GET` | `/.well-known/oauth-cimd` | Public OAuth [Client ID Metadata Document](https://huggingface.co/docs/hub/oauth) for this deployment (client name, redirect URI, scopes, `token_endpoint_auth_method: none`). Served only when `public_url` is https; it lets Hugging Face sign-in work without registering an app. No secrets. |
| `POST` | `/projects/{id}/connections/oauth/start` | Connect through a provider's official OAuth flow instead of pasting a key: `{"provider_id", "accept_terms": true, "display_name", "priority", "mode"}`. Returns `authorization_url` to open in the user's browser and a `flow_id`. `mode` is `redirect` (default: the provider sends the browser back to `/oauth/callback`) or `manual` (headless installs: the provider shows a code to paste). Providers that support it have `oauth_available: true` in `/providers`; today that is OpenRouter ([PKCE key exchange](https://openrouter.ai/docs/guides/overview/auth/oauth)) and, when this server has an https `public_url` or `OASIS_ROUTE_HF_OAUTH_CLIENT_ID` is set, Hugging Face ([OAuth 2.0 + PKCE](https://huggingface.co/docs/hub/oauth), scopes `openid profile read-billing inference-api`, redirect mode only). Hugging Face access tokens are refreshed by the `connections.oauth_refresh` worker job 30 minutes before they expire; a refused refresh suspends the connection with `auth_expired` instead of retrying. Flows expire after 10 minutes and are single use. A `redirect` flow is bound to the browser that started it with an HttpOnly, `SameSite=Lax` cookie scoped to the callback path, so a link started by one person cannot be completed by another; start it from the console (or any same-origin browser session) and finish in that browser. Clients without a browser use `manual`. |
| `POST` | `/projects/{id}/connections/oauth/{flow_id}/poll` | Check a `device` flow once (start it with `"mode": "device"`; providers that support it have `oauth_device: true` in `/providers`, today Hugging Face per its [device code OAuth](https://huggingface.co/docs/hub/oauth)). The start response carries `user_code` and `verification_uri` to show the user; the device code itself never leaves the server. Returns `pending` with the `interval` in seconds to wait before polling again (raised when the provider answers `slow_down`), or the final flow status: `completed` with `connection_id`, `failed` (declined) or `expired`. Verification addresses on any host other than the provider's are refused. |
| `GET` | `/projects/{id}/connections/oauth/{flow_id}` | Flow status: `pending`, `exchanging`, `completed` (with `connection_id`), `failed` (with `error`) or `expired`. |
| `POST` | `/projects/{id}/connections/oauth/{flow_id}/complete` | Finish a `manual` flow: `{"code"}`. Creates and validates the connection; returns `{connection_id, validation}`. A rejected code returns `502 oauth_failed`; start a new flow to retry. |
| `GET` | `/oauth/callback` | Browser redirect target for `redirect` flows (no Oasis Key; authenticated by the single-use `state`). Shows a short confirmation page and links back to the console. The callback base URL comes from `OASIS_ROUTE_PUBLIC_URL` when set. |
| `POST` | `/projects/{id}/connections/quick` | One-step onboarding: connect every key the user holds at once. Body `{"text", "keys": [{"api_key", "provider_id"?}], "providers_by_line": {"<line>": "<provider_id>"}, "accept_terms", "free_tier", "dry_run"}`. A dry run reports each key's `line` in `text`; `providers_by_line` names the provider for keys whose format several providers share. `text` may be pasted keys or a whole `.env` (only recognised provider variables such as `GROQ_API_KEY` are read; other lines are counted in `ignored_lines` and discarded). Each key's provider is detected locally from its variable name or prefix; ambiguous keys return `needs_provider` with `candidates` and are never tried against several providers. Every detected key is validated only with the provider that issued it, models are synchronised and that provider's approved discovery sources are refreshed. `dry_run: true` reports detection without storing or sending anything. `free_tier` declares no billing is set up; it is ignored for providers whose API is paid by default (OpenAI, Anthropic). Response: `{connected, already_connected, needs_attention, free_models_available, ignored_lines, results: [{key_hint, provider_id, outcome, detected_by, message, connection_id, status, billing_mode, models, free_models, candidates}]}` where `outcome` is `connected`, `already_connected`, `needs_provider`, `rejected`, `invalid` or `error`. At most 50 keys and 64 KB per request. Secrets are never echoed; `key_hint` is the last four characters. |
| `GET` | `/projects/{id}/connections`, `/projects/{id}/connections/{cid}` | Connections with status and health. Never includes the secret. |
| `PATCH` | `/projects/{id}/connections/{cid}` | Change name, priority, allowed models, or `billing_mode` (`free_tier`, `paid`, `unknown`). Connections also report `account_status` when the provider documents an account endpoint (OpenRouter). |
| `POST` | `/projects/{id}/connections/{cid}/validate` | Re-check now. |
| `POST` | `/projects/{id}/connections/{cid}/rotate-credential` | Replace the stored provider credential. |
| `DELETE` | `/projects/{id}/connections/{cid}` | Revoke the connection and destroy its stored credential. |
| `GET`, `POST` | `/projects/{id}/quota-pools` | Quota pools. Pools are learned automatically from provider rate-limit headers; manual pools are optional. |
| `PATCH`, `DELETE` | `/projects/{id}/quota-pools/{pool_id}` | Adjust or remove a pool. |
| `GET` | `/projects/{id}/routes` | Every route (connection × model) classified: `cost_class` (`free`, `paid`, `unknown`), `cost_basis`, `free_evidence` (`local`, `provider_price_list`, `account_declared`), `state` (`eligible`, `blocked`, `exhausted`), `reason`, `retry_in_s`, plus a `summary` (counts, `eligible_free`, spend policy, `unconfirmed_catalog_models`). Read-only. |
| `GET` | `/projects/{id}/setup` | Turnkey onboarding report ([ONBOARDING.md](ONBOARDING.md)): `steps`, detected `local_servers` (only when private upstreams are allowed), `connectable_providers` split into `verified` and `advertised`, classified `routes`, and the single OpenAI-compatible `endpoint`. Probes loopback default ports only; connects nothing. |
| `POST` | `/projects/{id}/setup` | Body `{"connect_local": true}`. Connects reachable local servers at priority `-10` (last resort), validates them and returns the same report with `connected_local`. Never changes the spend policy or any billing mode. Audited as `project.setup_applied`. |
| `POST` | `/projects/{id}/route-preview` | Same body as a chat request. Returns ordered candidates, rejections with reasons and retry times, downgrade flags and the fallback budget, without calling a provider or reserving quota. |

### Capacity, usage and notifications

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/projects/{id}/capacity` | Advertised, verified, authorized, usable, consumed (confirmed vs estimated) and forecast capacity, per provider account and unit. Unknown limits are reported as unknown, never as unlimited. |
| `GET` | `/projects/{id}/usage` | Per-attempt usage records (`?limit`, `?since`). |
| `GET` | `/projects/{id}/route-decisions` | Recent routing decisions with candidates, rejections, attempts and explanation. |
| `GET` | `/projects/{id}/route-decisions/{request_id}` | Explain one past request: selected route and reason, alternatives, rejections by gate, verification, quota state, estimated and actual cost, fallback chain. |
| `GET` | `/notifications` | Notifications for the caller (administrators also see instance-wide ones). |
| `POST` | `/notifications/{id}/read` | Mark read. |
| `DELETE` | `/notifications/{id}` | Delete. |

### Discovery and offers

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/discover` | The Discover catalogue in one call. Optional `project_id` (defaults to the key's project). Returns `status` (last discovery run, next scan, source counts, verified / pending / expired offers, provider candidates, and for the project its spend policy, free-tier connections and providers needing action) and `providers[]`, most useful first. Each provider has `access_path` (`official_oauth`, `api_key`, `anonymous_public`), `oauth_available`, `free_tier_hint`, `card_required` with `card_required_source` (`evidence`, `documented` or `unknown`), `default_billing_mode`, official `links` (signup, console, API key page, docs, pricing, rate limits, terms), model counts (`total`, `free`), `offers[]` (state, class, quotas, models, billing, expiry, evidence count and URLs, `verified`), `pending_review`, the project's `connection` and a `next_action` (`connect_oauth`, `connect_api_key`, `connected`, `confirm_billing`, `fix_connection`, `learn_more`, `unavailable`) with a label. Non-admins only see offers a reviewer made `connectable` or `active`; unreviewed ones are only counted. Read-only: it never fetches or changes anything. |
| `GET`, `POST` | `/sources` | Administrators register approved sources; other users submit leads for review. |
| `POST` | `/admin/sources/{id}/review` | Approve or reject a source. |
| `POST` | `/admin/sources/{id}/run` | Fetch now (robots, budgets and SSRF checks still apply). |
| `POST` | `/admin/discovery/run` | Run every due approved source now (same scheduler as the worker). Returns per-run stats. |
| `GET` | `/admin/provider-candidates` | Providers seen in public catalogues (for example the Hugging Face router) that are not yet registered. |
| `POST` | `/admin/provider-candidates/{id}` | Mark a candidate `promoted` or `dismissed`. Audited; never creates a provider or connection by itself. |
| `GET` | `/offers`, `/offers/{id}` | Non-administrators see only reviewed, connectable offers. |
| `PATCH` | `/admin/offers/{id}` | Correct candidate facts before review. |
| `POST` | `/admin/offers/{id}/transition` | Move an offer through its lifecycle (`discovered`, `needs_review`, `verified_eligible`, `connectable`, `active`, `rejected`, `ineligible`, `expired`, `suspended`, `stale`, `revoked`). |
| `GET` | `/admin/change-events` | Detected changes to sources and offers. |

### Mail (optional)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/mail/status` | Whether forwarding and Gmail are available, and why not. |
| `POST` | `/mail/forwarded` | Submit a forwarded provider notice. Only facts from official provider domains are kept. |
| `DELETE` | `/mail/data` | Delete every mail-derived notice for the caller. |
| `POST` | `/mail/gmail/consent` | Start Gmail consent (metadata scope only). Disabled unless the compliance gate is satisfied. |
| `GET` | `/mail/gmail/callback` | OAuth redirect target. |
| `POST` | `/mail/gmail/grants/{id}/sync` | Sync now. |
| `DELETE` | `/mail/gmail/grants/{id}` | Revoke the grant (`?purge=true` also deletes derived notices). |

## Operations endpoints

| Path | Response |
|---|---|
| `GET /healthz` | `200 {"status":"ok"}` while the process is up. |
| `GET /readyz` | `200 {"status":"ready","schema_revision":"0004","worker":"running"}` when the database is reachable and migrated; `503` otherwise. `worker` is `running`, `degraded`, `stalled` or `disabled` and never fails readiness, because the gateway keeps working without background jobs. |
| `GET /metrics` | Prometheus metrics: HTTP, gateway requests, upstream attempts and latency, fallbacks, tokens, reservations, open circuits, discovery fetches, worker job runs and restarts. |
