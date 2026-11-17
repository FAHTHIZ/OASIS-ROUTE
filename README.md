# Oasis Route

**Open-source inference discovery, provider intelligence, quota-aware routing, and failover.**

Oasis Route is an independent inference infrastructure project. It discovers publicly documented inference offers, verifies their eligibility and terms, connects upstream accounts only with authorization, and exposes a unified API that routes requests to the strongest eligible model with available capacity.

It is designed to work with Oasis and with any application that can call an OpenAI-compatible API.

## Oasis Key

**Oasis Key** is the name of the unified API credential issued by Oasis Route. An application uses its Oasis Key to authenticate with the Oasis Route gateway; Oasis Route then selects among the user's separately authorized upstream provider connections. Applications do not need a different gateway key for every inference provider.

```bash
export OASIS_API_KEY="your-oasis-key"
```

Send it as a bearer token:

```http
Authorization: Bearer <OASIS_API_KEY>
```

An Oasis Key is not an upstream provider credential. Provider API keys and OAuth tokens remain separately authorized and protected in the credential vault. Keys should be scoped, shown only once at creation, revocable, and rotatable.

> **Important:** Oasis Route does not create provider identities, harvest credentials, bypass access controls, evade quotas, or reset free allocations through account rotation. It discovers legitimate offers and uses provider access explicitly authorized by the account owner and permitted by provider terms.

## What it does

- **Discover:** monitor official provider documentation, public announcements, repositories, model catalogues, and other permitted public sources.
- **Verify:** preserve evidence, check eligibility and terms, and distinguish advertised offers from verified usable capacity.
- **Connect:** let users authorize their own upstream accounts or enter their own API credentials.
- **Route:** rank models by capability and task fit, then select the strongest eligible endpoint with sufficient quota.
- **Recover:** handle rate limits, outages, expired credentials, and exhausted allocations with bounded, policy-aware failover.
- **Measure:** account for input/output tokens, rate limits, shared quota pools, expiration, concurrency, and actual usable capacity.

## Architecture at a glance

```text
Public sources ──> Discovery ──> Evidence & Verification ──> Offer Registry
                                                               │
User-authorized provider connections ──> Credential Vault ─────┤
                                                               ▼
Applications ──> Unified API Gateway ──> Policy & Task Analysis
                                           │
                                  Capability / Quota Router
                                           │
                                  Provider Adapter Layer
                                           │
                               Upstream inference providers
                                           │
                         Usage ledger / health / capacity metrics
```

The discovery control plane is separate from the inference data plane. Finding an offer never automatically grants access to it.

## Project status

Oasis Route is working software. The implementation roadmap in [docs/EXECUTION_PLAN.md](docs/EXECUTION_PLAN.md) is tracked phase by phase, and [docs/IMPLEMENTATION_AUDIT.md](docs/IMPLEMENTATION_AUDIT.md) records what is verified. The test suite runs on SQLite and PostgreSQL in CI.

| Capability | Status |
|---|---|
| OpenAI-compatible gateway (`/v1/chat/completions`, `/v1/models`), streaming, idempotency | Implemented |
| Anthropic Messages API (`/v1/messages`, streaming, tool use) for Claude Code and Anthropic SDKs | Implemented |
| OpenAI Responses API (`/v1/responses`, streaming, function calls; stateless) for Codex CLI and `client.responses` | Implemented |
| `auto` / `auto/<task>` model aliases; heuristic priors order unrated models strongest-first | Implemented |
| Adapters: OpenAI, Anthropic, Gemini (OpenAI-compatible endpoint), Groq, Mistral, OpenRouter, Cerebras, NVIDIA API Catalog, SambaNova, Hugging Face router, custom OpenAI-compatible (llama.cpp, Ollama, vLLM) | Implemented |
| Encrypted credential vault (envelope encryption, KEK rotation with automatic re-wrap) | Implemented |
| Atomic quota ledger: reservations, settlement, shared pools, windows, header learning | Implemented |
| Strongest-suitable-first routing with explainable decisions and route preview | Implemented |
| Automatic failover with circuit breakers, honest downgrade signalling | Implemented |
| Discovery: safe fetcher, extraction, evidence, verification workflow, offer lifecycle | Implemented |
| Capacity analytics and forecasting | Implemented |
| Supervised background worker (leases, backoff, timeouts, crash recovery, stall alerts) | Implemented |
| Web console with plain-language status | Implemented |
| Optional mail notices (forwarded mail; Gmail behind a compliance attestation) | Implemented, off by default |
| Official provider OAuth connections (OpenRouter PKCE; redirect and headless modes) | Implemented |
| Account data export and erasure (secret-free export; deletion destroys stored credentials) | Implemented |
| Install from wheel and upgrade from the previous release, verified in CI | Implemented |

### Known limitations

- Inference endpoints are `POST /v1/chat/completions`, `POST /v1/responses` (stateless: no `previous_response_id`), `POST /v1/messages` and `GET /v1/models`. Embeddings, images, audio and legacy completions are not served.
- Streaming failover happens only before the first byte; a stream that fails midway ends with an error event rather than being spliced with another model's output.
- OAuth sign-in is available for OpenRouter only. Other providers use an API key that the user pastes once.
- Data-policy facts (training on inputs, retention) are unknown for seeded providers until an operator records them with evidence, so projects that require no-training or zero-retention exclude those providers. The exception is NVIDIA, whose own pages say inputs are used to improve its models; it is seeded as training on inputs.
- Discovery extraction is deterministic pattern matching. It proposes candidates for human review and never activates an offer by itself.
- Gmail is off by default and must stay off in production until Google's verification and security assessment are complete.
- `/metrics` is unauthenticated and must stay on a private network.

## Quickstart

Try it in one command, with deterministic mock providers and no upstream calls (needs [uv](https://docs.astral.sh/uv/)):

```bash
uvx --from git+https://github.com/FAHTHIZ/OASIS-ROUTE oasis-route dev
# prints the console URL, your project ID and your Oasis Key
```

Or with Docker, no Python needed (rolling image of `main`):

```bash
docker run --rm -p 8080:8080 ghcr.io/fahthiz/oasis-route:edge dev
```

Install it as a command instead (`oasis-route` on your PATH):

```bash
uv tool install git+https://github.com/FAHTHIZ/OASIS-ROUTE     # or: pipx install git+https://github.com/FAHTHIZ/OASIS-ROUTE
oasis-route dev --port 8080
```

From a clone, for development:

```bash
uv sync
uv run oasis-route dev
```

Add a real model in a minute with a free local server: see [local models](docs/LOCAL_MODELS.md).

Production-style, with PostgreSQL ([full guide](docs/DEPLOYMENT.md)):

```bash
# create .env with POSTGRES_PASSWORD, OASIS_ROUTE_MASTER_KEY, OASIS_ROUTE_API_KEY_PEPPER
docker compose up -d --build
docker compose run --rm api bootstrap --email you@example.com
```

Open `/console/`, sign in with the Oasis Key, add a provider API key under **Connections**, and call the gateway:

```bash
curl https://route.example.com/v1/chat/completions \
  -H "Authorization: Bearer $OASIS_API_KEY" -H 'content-type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"hello"}]}'
```

Any OpenAI SDK works by setting its base URL to `https://route.example.com/v1` and its API key to the Oasis Key.

## Documentation

- Using it: [API guide](docs/API.md), [SDKs and coding tools](docs/INTEGRATIONS.md) (Claude Code, Codex, Aider, Continue, Cline, OpenCode), [local models (llama.cpp, Ollama)](docs/LOCAL_MODELS.md), [free-tier capacity maths](docs/FREE_TIER_CAPACITY.md), [configuration](docs/CONFIGURATION.md)
- Running it: [deployment](docs/DEPLOYMENT.md), [operations runbook](docs/OPERATIONS.md), [zero-operations requirement](docs/ZERO_OPERATIONS_AUTONOMY.md)
- Building it: [development](docs/DEVELOPMENT.md), [architecture](docs/ARCHITECTURE.md), [routing and quotas](docs/ROUTING_AND_QUOTAS.md), [discovery and registry](docs/DISCOVERY_AND_REGISTRY.md), [provider adapter contract](docs/PROVIDER_ADAPTERS.md), [decision records](docs/adr/)
- Trust: [security and compliance](docs/SECURITY_AND_COMPLIANCE.md), [threat model](docs/THREAT_MODEL.md), [implementation audit](docs/IMPLEMENTATION_AUDIT.md), [release checklist](docs/RELEASE_CHECKLIST.md)
- Planning: [comparison with LiteLLM, OmniRoute and others](docs/COMPARISON.md), [execution plan](docs/EXECUTION_PLAN.md), [agent handoff](AGENT_HANDOFF.md), [research notes](docs/RESEARCH.md)

## Design principles

1. **Open source, not credential scraping.** Public information can be discovered; private credentials cannot be harvested.
2. **Strongest suitable model first.** Quality ranking is capability-aware, not just a static provider list.
3. **Quota correctness before throughput.** Reserve capacity atomically before dispatch.
4. **Fail over honestly.** Never hide a downgrade or silently violate task requirements.
5. **Evidence-backed registry.** Every offer has provenance, timestamps, status, and a review trail.
6. **Provider-neutral core.** Provider-specific behavior belongs in adapters, not routing business logic.
7. **Independent product.** Oasis Route can be used without Oasis.
8. **Observable and testable.** Every route decision and quota reservation must be explainable.
9. **Zero routine operations.** After adding an authorized provider credential, onboarding, discovery, quota synchronization, routing, health checks, and recovery should happen automatically in the background. See [the autonomy requirement](docs/ZERO_OPERATIONS_AUTONOMY.md).

## Contributing

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for setup and quality gates. Provider-specific behaviour belongs in adapters; do not wrap competing gateways or duplicate their routing engines.

## License

[Apache License 2.0](LICENSE).
