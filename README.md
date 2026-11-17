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

This repository currently contains the architecture and implementation handoff. The runtime is **not yet implemented**. The documents define the target system, non-goals, safety boundaries, interfaces, data model, milestones, and acceptance criteria. Do not describe planned features as working software.

## Start here

1. [Architecture](docs/ARCHITECTURE.md)
2. [Routing and quota engine](docs/ROUTING_AND_QUOTAS.md)
3. [Discovery and offer verification](docs/DISCOVERY_AND_REGISTRY.md)
4. [Security and compliance](docs/SECURITY_AND_COMPLIANCE.md)
5. [Provider adapter contract](docs/PROVIDER_ADAPTERS.md)
6. [Execution plan](docs/EXECUTION_PLAN.md)
7. [Agent handoff](AGENT_HANDOFF.md)
8. [Zero-operations autonomy requirement](docs/ZERO_OPERATIONS_AUTONOMY.md)
9. [Research notes and references](docs/RESEARCH.md)

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

## Development

The implementation agent should follow [AGENT_HANDOFF.md](AGENT_HANDOFF.md) and execute [docs/EXECUTION_PLAN.md](docs/EXECUTION_PLAN.md) in order. It must inspect the repository and choose a coherent stack before adding dependencies. Prefer maintained OSS components where they solve a real problem, but do not wrap multiple competing gateways or duplicate their routing engines.

## License

No license has been selected in this architecture commit. Before public release, choose an OSI-approved license, add its complete license text, and ensure all dependencies are license-compatible.
