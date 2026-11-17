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
| `task` | `general` | `general`, `coding`, `reasoning`, `creative`, `extraction`, `vision`. Selects which quality evidence ranks models. |
| `mode` | project `routing_mode` | `strict_quality`: strongest first, cost and latency only break ties. `balanced`: quality still dominates, with more weight on health and headroom. |
| `min_quality` | none | Never route below this score. |
| `allow_fallback` | `true` | Fail over to other eligible candidates on retryable errors. |
| `allow_downgrade` | project `allow_downgrade` | Allow falling back to a lower-quality model. A project setting of `false` cannot be overridden per request. |
| `providers` | all | Restrict routing to these provider ids. |
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

### `GET /v1/models`

Lists models reachable through the project's active connections, plus `auto`, in OpenAI list format.

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

### Providers, models and connections

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/providers` | Provider registry: terms, data policy, regions, eligibility rules, endpoints. |
| `PATCH` | `/admin/providers/{provider_id}` | Administrators update provider facts and status. |
| `GET` | `/models` | Model catalogue with capabilities, context window, quality evidence and lifecycle. |
| `PATCH` | `/admin/models/{model_id}` | Administrators update model facts and quality evidence. |
| `POST` | `/projects/{id}/connections` | Connect a provider with the user's own credential: `{"provider_id", "api_key", "base_url", "display_name", "upstream_account_ref", "accept_terms"}`. Validated immediately (non-billable model listing), models synchronized, and eligibility checked; the response reports the result. |
| `GET` | `/projects/{id}/connections`, `/projects/{id}/connections/{cid}` | Connections with status and health. Never includes the secret. |
| `PATCH` | `/projects/{id}/connections/{cid}` | Change name, priority, allowed models. |
| `POST` | `/projects/{id}/connections/{cid}/validate` | Re-check now. |
| `POST` | `/projects/{id}/connections/{cid}/rotate-credential` | Replace the stored provider credential. |
| `DELETE` | `/projects/{id}/connections/{cid}` | Revoke the connection and destroy its stored credential. |
| `GET`, `POST` | `/projects/{id}/quota-pools` | Quota pools. Pools are learned automatically from provider rate-limit headers; manual pools are optional. |
| `PATCH`, `DELETE` | `/projects/{id}/quota-pools/{pool_id}` | Adjust or remove a pool. |
| `POST` | `/projects/{id}/route-preview` | Same body as a chat request. Returns ordered candidates, rejections with reasons and retry times, downgrade flags and the fallback budget, without calling a provider or reserving quota. |

### Capacity, usage and notifications

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/projects/{id}/capacity` | Advertised, verified, authorized, usable, consumed (confirmed vs estimated) and forecast capacity, per provider account and unit. Unknown limits are reported as unknown, never as unlimited. |
| `GET` | `/projects/{id}/usage` | Per-attempt usage records (`?limit`, `?since`). |
| `GET` | `/projects/{id}/route-decisions` | Recent routing decisions with candidates, rejections and attempts. |
| `GET` | `/notifications` | Notifications for the caller (administrators also see instance-wide ones). |
| `POST` | `/notifications/{id}/read` | Mark read. |
| `DELETE` | `/notifications/{id}` | Delete. |

### Discovery and offers

| Method | Path | Purpose |
|---|---|---|
| `GET`, `POST` | `/sources` | Administrators register approved sources; other users submit leads for review. |
| `POST` | `/admin/sources/{id}/review` | Approve or reject a source. |
| `POST` | `/admin/sources/{id}/run` | Fetch now (robots, budgets and SSRF checks still apply). |
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
| `GET /readyz` | `200 {"status":"ready","schema_revision":"0002","worker":"running"}` when the database is reachable and migrated; `503` otherwise. `worker` is `running`, `degraded`, `stalled` or `disabled` and never fails readiness, because the gateway keeps working without background jobs. |
| `GET /metrics` | Prometheus metrics: HTTP, gateway requests, upstream attempts and latency, fallbacks, tokens, reservations, open circuits, discovery fetches, worker job runs and restarts. |
