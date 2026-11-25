# Configuration

Every setting is an environment variable prefixed with `OASIS_ROUTE_` (or a line in a `.env` file in the working directory). Names are case-insensitive. Secrets are held as `SecretStr` and never rendered in logs or reprs. Invalid values stop startup with a clear validation error.

The defaults are chosen so that a fresh install works without configuration: the background worker runs inside the API process, unknown quota is treated conservatively, and private-network upstreams are refused.

## Required in production

`OASIS_ROUTE_ENVIRONMENT=production` refuses to start unless all of these are set:

| Variable | Purpose |
|---|---|
| `OASIS_ROUTE_MASTER_KEY` | Base64 of exactly 32 random bytes. Key-encryption key (KEK) for the credential vault. Generate with `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`. |
| `OASIS_ROUTE_API_KEY_PEPPER` | Random secret (32+ characters) used to HMAC Oasis Keys before storage. Changing it invalidates every Oasis Key. |
| `OASIS_ROUTE_DATABASE_URL` | PostgreSQL URL, e.g. `postgresql+asyncpg://oasis:pw@db:5432/oasis_route`. SQLite is refused in production. |

Production also refuses `OASIS_ROUTE_GMAIL_ENABLED=true` without `OASIS_ROUTE_GMAIL_COMPLIANCE_ATTESTATION`.

## Core

| Variable | Default | Notes |
|---|---|---|
| `OASIS_ROUTE_ENVIRONMENT` | `development` | `development`, `test` or `production`. Production enforces the requirements above and disables mock providers. |
| `OASIS_ROUTE_DATABASE_URL` | `sqlite+aiosqlite:///./oasis_route.db` | SQLite for local use; PostgreSQL 14+ for anything shared. |
| `OASIS_ROUTE_HOST` | `127.0.0.1` | Bind address. The container image sets `0.0.0.0`. |
| `OASIS_ROUTE_PORT` | `8080` | |
| `OASIS_ROUTE_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `OASIS_ROUTE_LOG_JSON` | `true` | Structured JSON logs with request ids; secrets are redacted. |
| `OASIS_ROUTE_PUBLIC_URL` | unset | External base URL, e.g. `https://route.example.com`. Used to build provider OAuth callback URLs (`<public_url>/api/v1/oauth/callback`). Must be `https://` in production. When unset, the URL of the incoming request is used. |

## Keys and bootstrap

| Variable | Default | Notes |
|---|---|---|
| `OASIS_ROUTE_MASTER_KEY` | unset | See above. Outside production an insecure fixed development key is used. |
| `OASIS_ROUTE_PREVIOUS_MASTER_KEYS` | `[]` | JSON list of retired KEKs still accepted for decryption during rotation, e.g. `'["base64..."]'`. See [OPERATIONS.md](OPERATIONS.md#master-key-rotation). |
| `OASIS_ROUTE_API_KEY_PEPPER` | unset | See above. |
| `OASIS_ROUTE_ADMIN_BOOTSTRAP_TOKEN` | unset | One-time token that allows `POST /api/v1/bootstrap` while no users exist. Remove it after bootstrapping. `oasis-route bootstrap` does not need it. |

## Gateway

| Variable | Default | Notes |
|---|---|---|
| `OASIS_ROUTE_MAX_REQUEST_BYTES` | `2000000` | Larger request bodies get `413`. |
| `OASIS_ROUTE_DEFAULT_MAX_OUTPUT_TOKENS` | `1024` | Used for quota reservation when a request omits `max_tokens`. |
| `OASIS_ROUTE_MAX_FALLBACK_HOPS` | `3` | Upper bound on failover attempts per request (a project can lower it). |
| `OASIS_ROUTE_UPSTREAM_CONNECT_TIMEOUT_S` | `10.0` | |
| `OASIS_ROUTE_UPSTREAM_READ_TIMEOUT_S` | `120.0` | |
| `OASIS_ROUTE_UPSTREAM_MAX_RESPONSE_BYTES` | `20000000` | Upstream responses larger than this are rejected. |
| `OASIS_ROUTE_RESERVATION_TTL_S` | `900` | Quota reservations not settled within this time are released by the worker. |
| `OASIS_ROUTE_RATE_LIMIT_PER_KEY_RPM` | `600` | Per-Oasis-Key requests per minute (a project can set a lower limit). |
| `OASIS_ROUTE_RATE_LIMIT_BACKEND` | `auto` | Where per-key counters live. `auto`: in the database on PostgreSQL, so the limit holds across every replica (sliding one-minute window, one upsert per request); in memory on SQLite. `database` or `memory` forces one. |
| `OASIS_ROUTE_CATALOG_IMPORT_ENABLED` | `true` | Seed the imported provider/model catalogue (about 200 providers and 2,000 models from the OmniRoute snapshot, see [CATALOG.md](CATALOG.md)). Imported data is advertised only: models stay non-routable until the provider's own model list confirms them, and imported free-tier claims never make a route free. |
| `OASIS_ROUTE_ALLOW_PRIVATE_UPSTREAMS` | `false` | Allow upstream base URLs on private or loopback networks (local model servers). Keep off on shared hosts: it disables SSRF protection for connections. |
| `OASIS_ROUTE_ENABLE_MOCK_PROVIDERS` | unset | Seeds the deterministic `mock` and `mock_alt` providers. Unset means on outside production, off in production. |

## Health and failover

| Variable | Default | Notes |
|---|---|---|
| `OASIS_ROUTE_BREAKER_FAILURE_THRESHOLD` | `5` | Consecutive retryable failures before a connection or model is taken out of rotation. |
| `OASIS_ROUTE_BREAKER_BASE_COOLDOWN_S` | `15.0` | First cooldown; doubles on each re-open. |
| `OASIS_ROUTE_BREAKER_MAX_COOLDOWN_S` | `900.0` | Cooldown cap. |

## Background worker

| Variable | Default | Notes |
|---|---|---|
| `OASIS_ROUTE_WORKER_MODE` | `embedded` | `embedded`: the API process supervises the worker and restarts it after a crash (default; nothing else to run). `external`: run `oasis-route worker` as a separate service. `off`: no background jobs (tests only; `/readyz` reports `disabled`). `oasis-route serve --no-worker` sets `external`. |
| `OASIS_ROUTE_WORKER_TICK_S` | `15.0` | How often a worker checks for due jobs and records its heartbeat. |
| `OASIS_ROUTE_WORKER_STALL_AFTER_S` | `180.0` | A worker without a heartbeat for this long is considered stalled; admins are notified once. |
| `OASIS_ROUTE_WORKER_MAX_BACKOFF_S` | `3600.0` | Cap for exponential retry backoff after a job fails. |
| `OASIS_ROUTE_WORKER_INTERVAL_S` | `30.0` | Discovery scheduling interval (each source also has its own interval). |

## Discovery

| Variable | Default | Notes |
|---|---|---|
| `OASIS_ROUTE_DISCOVERY_USER_AGENT` | `OasisRouteDiscovery/0.1 (+https://github.com/FAHTHIZ/OASIS-ROUTE)` | Identifies the fetcher to site owners. Keep a contact URL in it. |
| `OASIS_ROUTE_DISCOVERY_MAX_BYTES` | `2000000` | Per-fetch body limit. |
| `OASIS_ROUTE_DISCOVERY_TIMEOUT_S` | `20.0` | |
| `OASIS_ROUTE_DISCOVERY_MAX_REDIRECTS` | `3` | Every redirect hop is re-checked against SSRF rules and robots.txt. |
| `OASIS_ROUTE_DISCOVERY_ROBOTS_TTL_S` | `86400` | robots.txt cache lifetime. |
| `OASIS_ROUTE_DISCOVERY_SOURCES_PER_TICK` | `10` | Upper bound on sources fetched per worker run. |
| `OASIS_ROUTE_DISCOVERY_MAX_PENDING_SUBMISSIONS_PER_USER` | `20` | Limits unreviewed source submissions from non-admins. |
| `OASIS_ROUTE_OFFER_STALE_AFTER_DAYS` | `30` | Offers whose evidence has not been re-confirmed for this long are marked stale and stop being routable. |
| `OASIS_ROUTE_HF_OAUTH_CLIENT_ID` | empty | Client id of a public Hugging Face OAuth app used for "Sign in with Hugging Face" (scopes `openid profile read-billing inference-api`). When empty and `OASIS_ROUTE_PUBLIC_URL` is https, the server serves its own Client ID Metadata Document at `/.well-known/oauth-cimd` and uses that URL as the client id, so no registration is needed. Otherwise Hugging Face is connected with an API key. |
| `OASIS_ROUTE_DISCOVERY_SEED_SOURCES` | `true` | Register the built-in, maintainer-reviewed official provider pages and public model catalogues (`discovery/seeds.py`) at startup and daily. Existing rows are never modified; set `false` to run with only the sources you add. |
| `OASIS_ROUTE_DISCOVERY_FRONTIER_ENABLED` | `true` | Follow links from approved official pages to more access/pricing/limits/models pages on the same provider's official domains. |
| `OASIS_ROUTE_DISCOVERY_FRONTIER_MAX_DEPTH` | `1` | Link-following depth from a seed or admin-approved source (0 disables following). |
| `OASIS_ROUTE_DISCOVERY_FRONTIER_LINKS_PER_FETCH` | `5` | New sources one fetched page may add. |
| `OASIS_ROUTE_DISCOVERY_FRONTIER_MAX_PER_PROVIDER` | `25` | Total link-discovered sources per provider. |

## Optional mail integration

Gmail is off by default and must stay off in production until Google's OAuth verification and any required security assessment are complete. See [SECURITY_AND_COMPLIANCE.md](SECURITY_AND_COMPLIANCE.md).

| Variable | Default | Notes |
|---|---|---|
| `OASIS_ROUTE_GMAIL_ENABLED` | `false` | |
| `OASIS_ROUTE_GMAIL_COMPLIANCE_ATTESTATION` | unset | Reference to the completed verification/assessment. Required in production. |
| `OASIS_ROUTE_GMAIL_CLIENT_ID` | unset | Google OAuth client. Only the `gmail.metadata` scope is requested. |
| `OASIS_ROUTE_GMAIL_CLIENT_SECRET` | unset | |
| `OASIS_ROUTE_GMAIL_REDIRECT_URI` | unset | Must point at `/api/v1/mail/gmail/callback`. |
| `OASIS_ROUTE_MAIL_FACTS_RETENTION_DAYS` | `30` | Extracted notice facts are deleted after this many days. Raw mail is never stored. |
| `OASIS_ROUTE_MAIL_FORWARDING_ENABLED` | `true` | Accept provider notices that users forward to `POST /api/v1/mail/forwarded`. |

## Record retention

| Variable | Default | Notes |
|---|---|---|
| `OASIS_ROUTE_USAGE_RETENTION_DAYS` | `0` | Delete usage records older than this. `0` keeps them. |
| `OASIS_ROUTE_ROUTE_DECISION_RETENTION_DAYS` | `0` | Delete routing decisions older than this. `0` keeps them. |
| `OASIS_ROUTE_AUDIT_RETENTION_DAYS` | `0` | Delete audit events older than this. `0` keeps them. Keep at least as long as your incident-response policy requires. |

The `retention.records` job applies these hourly.

## Telemetry

| Variable | Default | Notes |
|---|---|---|
| `OASIS_ROUTE_OTEL_ENABLED` | `false` | Emit OpenTelemetry traces for gateway requests and upstream calls. Requires the `otel` extra (`pip install 'oasis-route[otel]'`; the container image includes it). Exporter settings use the standard `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` variables. Prompts and secrets are never recorded as span attributes. |

Prometheus metrics are always available at `GET /metrics` (do not expose it publicly).

## Local all-in-one (`oasis-route dev`)

`oasis-route dev` always runs with `OASIS_ROUTE_ENVIRONMENT=development` (it overrides the production value set in the container image), SQLite, private upstreams allowed and the two mock providers enabled. It is for trying Oasis Route, not for serving traffic.

| Variable | Default | Notes |
|---|---|---|
| `OASIS_ROUTE_DEV_STATE_DIR` | `.oasis-dev` (`/data/dev` in the image) | Where `dev` keeps its SQLite database and `admin.key`. `--state-dir` overrides it. |

## Per-project settings

Routing behaviour that varies per project is stored in the database and changed through `PATCH /api/v1/projects/{id}` rather than environment variables: `routing_mode` (`strict_quality`, `balanced`, `lowest_cost`, `most_quota`, `lowest_latency`, `reliability`, `context_fit`, `reset_aware` or `diversity`), `max_fallback_hops`, `allow_downgrade`, `allow_unknown_quota`, `spend_policy` (`free_only` / `zero_budget`, the default for new projects; `bounded_autonomy`; or `allow_paid` / `continuity`, which needs `acknowledge_paid_usage: true`), `data_policy` (no-training, zero-retention, allowed regions, provider allow/deny lists), `rate_limit_rpm`, and `kill_switch`. Background maintenance can be paused per project with `PUT /api/v1/projects/{id}/automation`.
