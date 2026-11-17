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
