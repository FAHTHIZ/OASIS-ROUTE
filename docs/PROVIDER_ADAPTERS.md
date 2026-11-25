# Provider Adapter Contract

## Purpose

Adapters isolate provider-specific protocols and quirks from the gateway, router, and quota engine. The core must not contain provider-specific branching scattered across request handlers.

## Required operations

Each adapter should expose equivalents of:
- `list_models(connection)` — return known model identifiers and metadata when supported.
- `get_capabilities(model)` — modalities, context limit, tool calling, structured output, streaming, and known constraints.
- `validate_connection(connection)` — validate credentials without leaking them.
- `estimate_usage(request)` — conservative token estimate; explicitly mark approximate.
- `reserve_or_check_quota(connection, model, estimate)` — query provider quota where supported; local ledger remains authoritative for Oasis Route reservations.
- `invoke(request, options)` — normalized non-streaming request.
- `stream(request, options)` — normalized streaming events and cancellation.
- `normalize_error(error)` — stable error categories and retry guidance.
- `extract_usage(response)` — input/output/total tokens, provider-reported flag, and raw safe metadata.
- `health_check(connection)` — bounded and non-destructive health check.

## Adapter metadata

Declare:
- stable adapter ID and version;
- supported API protocols and endpoint types;
- auth method;
- streaming and cancellation support;
- usage reporting quality;
- quota header/API support;
- retry/idempotency behavior;
- region and data processing notes;
- required secrets and scopes;
- tested models and compatibility notes.

## Security rules

- Credentials are injected at call time from the vault; adapters must not persist secrets.
- Never log authorization headers, API keys, prompts, or full provider errors containing secrets.
- Validate endpoint hostnames and block private/link-local metadata addresses when user-configurable endpoints are supported; prevent SSRF and DNS rebinding.
- Set strict connect/read/total timeouts and response-size limits.
- Do not follow arbitrary redirects with credentials attached.
- Validate TLS certificates.
- Provider errors are untrusted input; sanitize before logs/UI.
- Never treat an arbitrary discovered URL as an inference endpoint without explicit validation and user/policy authorization.

## Compatibility

Prefer a shared OpenAI-compatible adapter only when the provider genuinely implements the needed semantics. Maintain provider-specific adapters for auth, quotas, streaming, tool calling, and error behavior that differ. Do not advertise full compatibility based solely on accepting a similar JSON shape.

## Contract tests

Every adapter must pass deterministic tests for:
- success and malformed responses;
- streaming and cancellation;
- timeout and connection reset;
- 401/403, 404/model missing, 429, 5xx;
- Retry-After parsing;
- usage extraction and missing usage;
- quota headers and unknown quota;
- secret redaction;
- oversized responses and unsafe redirects.

## Adding a provider without touching the router

The router, ledger and gateway only see the `ProviderAdapter` protocol (`src/oasis_route/adapters/base.py`). A provider is resolved by `AdapterRegistry.for_provider(provider_id, adapter_key)` in this order:

1. **Curated profile** in `adapters/registry.py` `PROFILES`: reviewed quirks (stream usage support, rate-limit header period, model-list URL and JSON shape, probe model for providers without a list).
2. **Local-server preset** in `LOCAL_PROFILES`: Ollama, llama.cpp, vLLM, LM Studio, Lemonade, llamafile, Docker Model Runner, Xinference. No key is sent, and only streaming is assumed.
3. **Generated from the imported catalogue** ([CATALOG.md](CATALOG.md)): `profile_from_catalog` reads the entry's auth style (`bearer`, `x-api-key`, `header:<Name>`, optional prefix), extra headers and HTTPS model-list URL. Anthropic-compatible Messages APIs get an `AnthropicProfile` with the same auth fields, so MiniMax-style `Authorization: Bearer` works next to Anthropic's own `x-api-key`.
4. **Protocol defaults** otherwise.

Two protocols are implemented: `openai_chat` (`OpenAICompatibleAdapter`) and `anthropic_messages` (`AnthropicAdapter`). Gemini / AI Studio, OpenRouter, Groq, Mistral, Cerebras, NVIDIA, SambaNova, Hugging Face, Cohere and Z.ai use their documented OpenAI-compatible endpoints with curated profiles. A provider that needs a different wire protocol needs a new adapter class registered in `AdapterRegistry._build`; the router never branches on provider ids.

What each adapter is responsible for:

| Concern | Where |
|---|---|
| Authentication | `_headers` from the profile (`auth_header`, `auth_prefix`, `api_key_required`) |
| Request / streaming translation | `invoke` / `stream` (Anthropic: `to_anthropic`, SSE event translation) |
| Model discovery | `list_models` (profile `models_url`, `models_key`, `models_id_field`) or `probe_model` |
| Token usage extraction | `extract_usage`; stream usage requested when `stream_usage_option` |
| Cost calculation | `routing/cost.py` `estimate_cost`: free / priced (verified or estimate) / unknown, integer USD micros rounded up. Unknown is never zero |
| Rate-limit headers, quota and reset detection | `quota_observations`, `parse_retry_after`, `parse_reset` |
| Retryability and error normalisation | `normalize_error` → `UpstreamError(category, retryable, retry_after)` |
| Health checks | `health_check` (bounded, non-destructive) |
| Capability declaration | `metadata` (`AdapterMetadata`) and `get_capabilities` |

Local presets must point at a private or loopback address (checked by resolving the host at connect time) because they are priced as free; use the `custom` provider for a remote OpenAI-compatible server.
