# Research Notes and Technical References

Research was used to inform architecture, not to copy another project's implementation. Re-check upstream docs during implementation because APIs, terms, and quota rules change.

## Open-source routing and gateway references

- [LiteLLM Router reliability](https://docs.litellm.ai/docs/router/learn/reliability) — useful reference for retries, cooldowns, and fallback behavior.
- [LiteLLM Router selection](https://docs.litellm.ai/docs/router/learn/selection) — useful reference for model/provider selection concepts.
- [OmniRoute repository](https://github.com/cheaper-inference/OmniRoute) — reference for a catalogue of free-tier offers, quota-aware routing, and fallback. Treat aggregate capacity claims in project documentation as claims, not independently verified totals.
- [OmniRoute free-tier methodology](https://github.com/cheaper-inference/OmniRoute/blob/release/v3.8.55/docs/reference/FREE_TIERS.md)
- [OmniRoute Radar](https://github.com/cheaper-inference/OmniRoute/blob/release/v3.8.55/docs/frameworks/RADAR.md) — useful distinction between a signed/curated catalogue client and an autonomous discovery crawler.
- [Free LLM API](https://github.com/MaurizioCasciano/Free-LLM-API) — reference for quota accounting, provider-wide limits, in-flight leases, fallback, and bandit-inspired routing. A catalogue entry alone does not create a provider adapter.
- [Free LLM API architecture](https://github.com/MaurizioCasciano/Free-LLM-API/tree/main/docs/en/architecture) — review routing, quota, degraded-mode, and catalogue-sync concepts.
- [OpenAI API compatibility](https://platform.openai.com/docs/api-reference) — reference for common API shapes; compatibility must be tested endpoint by endpoint.

## Gmail and Google

- [Gmail API scopes](https://developers.google.com/workspace/gmail/api/auth/scopes) — current Google documentation classifies `gmail.readonly` as a restricted scope and describes verification/security assessment implications.
- [Gmail API push notifications](https://developers.google.com/workspace/gmail/api/guides/push) — reference for mailbox change notifications. Push subscriptions have lifecycle/renewal requirements; implement renewal and reconciliation if used.
- [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy) — required policy review before any Google user-data integration.
- [Gemini API rate limits](https://ai.google.dev/gemini-api/docs/rate-limits) — provider quotas are project/model scoped and can vary by tier; do not assume one universal token allowance.
- [Gemini API terms](https://ai.google.dev/gemini-api/terms) — check current eligibility, permitted use, and audience restrictions before integration. Enforce restrictions; do not bypass them.

## Crawling and telemetry

- [RFC 9309 — Robots Exclusion Protocol](https://www.rfc-editor.org/rfc/rfc9309) — crawler policy reference; robots rules are not authorization to ignore other terms or access controls.
- [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/) — evolving conventions for model/provider telemetry. Pin a version and avoid emitting sensitive prompt content.

## Lessons to preserve

1. A curated provider catalogue is useful, but it is not a crawler.
2. A discovery crawler can find an offer, but cannot authorize use.
3. A gateway can expose one API, but cannot magically support every provider without an adapter.
4. Quota is multidimensional and often shared at project/account level.
5. Concurrent reservations are necessary to avoid overspending.
6. Quality ranking should be task-specific and evidence-backed.
7. Gmail is a high-compliance optional integration, not a shortcut for mass registration.
8. Claimed aggregate tokens must be labelled by evidence and actual user eligibility.
