# Command line

`oasis-route` runs the server and also inspects a running one. The inspection commands below read the management API with an Oasis Key and change nothing, except `verify`, which re-validates stored credentials with a non-billable model listing.

## Connecting the CLI to a server

| Setting | Default | Notes |
|---|---|---|
| `--url` | `$OASIS_ROUTE_URL` or `http://127.0.0.1:8080` | Any Oasis Route instance. |
| `--oasis-key` | `$OASIS_API_KEY` | Pasting the whole two-line `.oasis-dev/admin.key` also works: the key is picked out. With neither set, the CLI uses `.oasis-dev/admin.key` from the current directory (or `$OASIS_ROUTE_DEV_STATE_DIR`). |
| `--project` | the key's project | |
| `--json` | off | Print the raw API response instead of a table. |

An unreachable server or a rejected key is reported in one line with the likely cause, not a traceback.

## Commands

| Command | Shows | API |
|---|---|---|
| `oasis-route providers [--connected] [--advertised] [--status S] [--search T]` | Every registry and catalogue provider: status, source (`curated` or `omniroute`), verification (`reviewed` or `advertised`), advertised free tier, connected accounts. Imported entries are always labelled advertised and unverified. | `/api/v1/providers`, `/connections` |
| `oasis-route models [--provider P] [--routable] [--search T] [--limit N]` | Models with lifecycle (`unconfirmed` for catalogue models no connected account has listed yet), context window, capabilities, price (marked `(unverified)` unless it comes from a verified price list) and advertised free tier. | `/api/v1/models` |
| `oasis-route routes [--state S] [--cost C]` | Every connection × model route: free, paid or unknown with the evidence, and eligible, blocked or exhausted with the reason and retry time. | `/projects/{id}/routes` |
| `oasis-route quota` | Remaining quota per account and unit, reset times, active cooldowns (rate-limited pools and open circuits), forecast exhaustion, and USD budgets. | `/capacity`, `/connections`, `/budgets` |
| `oasis-route verify [--connection ID]` | Re-validates each connection and shows the evidence behind its routes (`free:local`, `free:provider_price_list`, `paid:...`). Exit code 1 if any check fails. | `POST /connections/{cid}/validate`, `/routes` |
| `oasis-route preview [PROMPT] [--model M] [--task T] [--mode M] [--free-only] [--require tools vision]` | Which route a request would take now, why, the alternatives, every rejection by gate, and the fallback chain. Calls no provider and reserves nothing. Exit code 3 when no route is eligible. | `POST /route-preview` |
| `oasis-route decisions [--limit N]` | Recent route decisions: requested and served model, outcome, fallback, attempts. | `/route-decisions` |
| `oasis-route explain REQUEST_ID` | One past request: selected route and reason, cost evidence, quota, alternatives, rejections, each attempt (with retry/reset after a 429), and the receipt (tokens, estimated, reserved and actual cost, drift, digest check). | `/route-decisions/{rid}`, `/receipts/{rid}` |
| `oasis-route doctor` | Server readiness and schema, worker, key scopes and expiry, project status, each credential, open circuits, eligible free routes, spend policy, budgets. Exit code 1 on any failure. | several |
| `oasis-route integrate [TOOL] [--check]` | Paste-ready settings for coding tools and SDKs ([INTEGRATIONS.md](INTEGRATIONS.md)). | `/api/v1/integrations` |
| `oasis-route setup` | Turnkey onboarding ([ONBOARDING.md](ONBOARDING.md)). | `/projects/{id}/setup` |

Request ids appear in every response (`x-request-id` and `oasis_route.request_id`), so `oasis-route explain "$(curl -si ... | grep -i x-request-id | cut -d' ' -f2)"` explains a request you just made.

## Console

The web console at `/console/` shows the same information: **Routes** (classification), **Capacity** (quota, active cooldowns, budgets, forecast), **Usage** (recent decisions with an **Explain** panel), **Spend** (budgets and receipts), **Route preview** (with the full explanation) and **Integrations** (per-tool settings).
