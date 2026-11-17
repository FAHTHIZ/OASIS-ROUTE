# Development

## Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/) 0.5 or newer
- Optional: PostgreSQL 16 (to run the suite against PostgreSQL), Docker (to build the image)

## Five-minute local run

```bash
git clone https://github.com/FAHTHIZ/OASIS-ROUTE.git
cd OASIS-ROUTE
uv sync
uv run oasis-route dev
```

`oasis-route dev` gives you a complete working instance in one command:

1. Creates `.oasis-dev/` with a SQLite database and applies migrations.
2. Starts two deterministic, OpenAI-compatible mock providers on ports 9100 and 9101 (no network, no cost).
3. Creates an administrator and saves the Oasis Key to `.oasis-dev/admin.key` (mode 0600; line 1 is the project id, line 2 the key).
4. Connects and validates both mock providers, which synchronizes their models automatically.
5. Starts the API, gateway and console on port 8080, with the background worker supervised in the same process.

It prints the console URL and a ready-to-run request:

```bash
export OASIS_API_KEY=$(sed -n 2p .oasis-dev/admin.key)
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer $OASIS_API_KEY" -H 'content-type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"hello"}]}'
```

Open <http://127.0.0.1:8080/console/> and paste the key to see status, connections, capacity, route previews and usage.

Options: `--port`, `--mock-port`, `--state-dir`. Delete the state directory to start fresh. Re-running `dev` with an existing state directory keeps your data.

### Connecting a real provider locally

In the console, open **Connections**, choose a provider, paste your own API key and accept the provider terms. Oasis Route validates it with a non-billable model-list call, synchronizes models, and starts routing to it. To point a connection at a local model server (Ollama, vLLM, llama.cpp) set `OASIS_ROUTE_ALLOW_PRIVATE_UPSTREAMS=true` first; this is off by default because it disables SSRF protection for connections. See [LOCAL_MODELS.md](LOCAL_MODELS.md).

## Other commands

```bash
uv run oasis-route serve --migrate      # API + embedded worker using your .env / environment
uv run oasis-route serve --no-worker    # API only; run `oasis-route worker` separately
uv run oasis-route worker               # standalone worker (safe to run several)
uv run oasis-route worker --once        # run every due job once and print results
uv run oasis-route migrate up|down      # schema migrations
uv run oasis-route bootstrap --email you@example.com   # first administrator, prints the Oasis Key once
uv run oasis-route rotate-kek           # re-wrap credentials after a master key rotation
uv run oasis-route mock-provider --port 9100            # a single mock provider
```

## Quality gates

```bash
make fmt        # ruff format + autofix
make lint       # ruff format --check + ruff check
make type       # mypy --strict
make test       # pytest on SQLite (about two minutes)
make test-pg    # same suite on PostgreSQL (OASIS_TEST_DATABASE_URL)
make check      # lint + type + test
```

CI runs lint, the full suite on SQLite and on PostgreSQL 16, a container build with a vulnerability scan (Trivy) and a smoke test of the default compose deployment, and supply-chain checks: dependency audit (pip-audit), license compatibility (`scripts/check_licenses.py`), secret scanning over the full history (gitleaks) and a CycloneDX SBOM. Run the same checks locally with `make audit`. Tagged releases (`v*`) build distributions and a GHCR image with SBOM and signed build provenance.

To run the PostgreSQL suite locally:

```bash
createuser -s oasis; createdb -O oasis oasis_route_test   # or use docker run postgres:16
OASIS_TEST_DATABASE_URL=postgresql+asyncpg://oasis:oasis@localhost/oasis_route_test uv run pytest
```

Every test session applies the real Alembic migrations, so migrations are tested on both databases.

## Repository layout

```text
src/oasis_route/
  api/            HTTP routers (gateway, management, status, discovery, mail)
  adapters/       provider adapters (OpenAI-compatible profiles incl. Gemini/Groq/Mistral/OpenRouter/Cerebras, Anthropic)
  gateway/        request parsing, failover engine, streaming relay, idempotency
  routing/        candidate building and strongest-suitable-first ranking
  quota/          atomic reservation ledger, windows, observations
  health/         circuit breakers
  capacity/       capacity analytics and forecasting
  connections/    connection lifecycle, validation, eligibility
  discovery/      safe fetcher, extraction, evidence, offer lifecycle
  registry/       seeded provider and model catalogue
  notifications/  in-app notices, optional mail ingestion
  security/       vault, Oasis Key hashing, SSRF, redaction, rate limiting
  worker/         supervised lease-coordinated background jobs
  static/         web console (no build step)
  db/             models and Alembic migrations
tests/            unit, integration and end-to-end tests (deterministic, no network)
docs/             architecture, operations and API documentation
```

## Conventions

- Provider-specific behaviour belongs in an adapter (`docs/PROVIDER_ADAPTERS.md`), never in routing code.
- Tests never touch the network: upstreams are in-process mock providers, and discovery uses a fake web.
- Secrets never appear in logs, errors, API responses or test snapshots; use `security.redaction` for anything that echoes provider text.
- Schema changes need an Alembic migration that works on SQLite (`batch_alter_table`) and PostgreSQL.
- Background work is a `Job` in `worker/runner.py`: idempotent, bounded, and safe to run concurrently on several hosts.
