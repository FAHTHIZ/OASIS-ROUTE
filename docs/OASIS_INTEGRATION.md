# OASIS integration

OASIS Route is the first-party, optional inference-routing companion for the OASIS agent runtime.

- **OASIS** owns the agent loop, tools, sessions, subagents, workflows, and task verification.
- **OASIS Route** owns upstream provider connections, provider eligibility and spend policy, quota reservations, inference routing, and inference failover.
- **The connection** is a standard OpenAI-compatible API endpoint. No private wire protocol is required.

Route remains a separate service. OASIS can run without it and continue using direct providers or another gateway; Route can serve other OpenAI- and Anthropic-compatible clients.

## Run Route locally

From the OASIS-ROUTE repository:

```bash
uv sync
uv run oasis-route dev --port 8080
```

The development command creates a local SQLite state directory and an Oasis Key. By default it also starts deterministic mock providers, so the first run can be checked without an upstream account or inference charge.

The local key is stored in `.oasis-dev/admin.key`. The second line is the bearer token:

```bash
export OASIS_API_KEY="$(sed -n 2p .oasis-dev/admin.key)"
```

Before expecting real inference, connect at least one provider account you are authorized to use in **Connections** in the Route console. The mock providers make the development instance testable; they do not represent real upstream capacity.

## Point OASIS at Route

In the same shell where OASIS runs:

```bash
export OASIS_BASE_URL="http://127.0.0.1:8080/v1"
export OASIS_API_KEY="<the Oasis Key printed by Route>"
export OASIS_MODEL="auto"

oasis
```

OASIS treats this as a custom OpenAI-compatible endpoint. The `auto` model alias is resolved by OASIS Route, which chooses an eligible upstream model for each request. Keep the Route service bound to loopback for local development; for remote use, follow the TLS and reverse-proxy guidance in [Deployment](DEPLOYMENT.md).

## Current integration boundary

The generic OpenAI-compatible connection works today. OASIS does **not yet** ship a named `oasis-route` provider entry, dedicated setup preset, or Route-aware free-budget classification. Those are the next native-integration tasks in the OASIS repository.

In particular, OASIS `--zero-budget` intentionally rejects unknown local gateway endpoints: it cannot infer which downstream provider an arbitrary router selects. Route's own `free_only` spend policy is a separate server-side control and does not currently change that OASIS decision. Do not label `auto` as free merely to bypass the guard; the native integration should make the trust boundary explicit and preserve both systems' spend protections.

## Production

For production, follow [Deployment](DEPLOYMENT.md): use PostgreSQL, configure the required master key and API-key pepper, put the service behind HTTPS, and protect the Route API key. Do not expose a development key or an unauthenticated local gateway to a public network.
