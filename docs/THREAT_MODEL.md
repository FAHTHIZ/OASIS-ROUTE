# Threat Model

Method: STRIDE over the main trust boundaries. Each threat lists the control that implements the mitigation and
where it is tested. This document is reviewed at every release (see `docs/RELEASE_CHECKLIST.md`).

## Assets
1. Oasis Route API keys (caller credentials).
2. Upstream provider credentials (API keys, OAuth refresh tokens).
3. User prompts and provider responses (in transit only; bodies are not stored by default).
4. Quota ledger integrity (financial/quota correctness).
5. Offer registry integrity (evidence, verification decisions).
6. Admin bootstrap token and KEK (key-encryption key).

## Trust boundaries
- B1 Internet caller → gateway (`/v1/*`).
- B2 Operator/admin → admin API (`/api/v1/*`) and console.
- B3 Gateway → upstream providers (outbound).
- B4 Worker → public internet (discovery fetcher, untrusted content).
- B5 Application → database.

## Threats and controls

| ID | STRIDE | Threat | Control | Test |
|---|---|---|---|---|
| T1 | S | Stolen/guessed API key | 256-bit random keys, HMAC-SHA256 keyed hash at rest, prefix lookup + constant-time compare, revocation, expiry, per-key rate limits | `tests/test_api_keys.py` |
| T2 | I | Upstream credential disclosure from DB dump | AES-256-GCM envelope encryption; per-secret DEK wrapped by KEK held outside DB; AAD binds ciphertext to connection id | `tests/test_vault.py` |
| T3 | I | Secrets in logs/errors/exports | Redaction filter on all log records, sanitized provider errors, response models never include secrets | `tests/test_redaction.py` |
| T4 | E | Tenant A uses tenant B's connection/pool | Every query scoped by project; router only loads the caller project's connections | `tests/test_tenant_isolation.py` |
| T5 | T | Quota overspend via concurrent requests | Atomic conditional updates in one transaction; idempotent reservation key | `tests/test_quota_concurrency.py` |
| T6 | T | Activation of unverified offers | Verification state machine; routing requires `ACTIVE` offer or explicit connection without offer + operator policy | `tests/test_discovery_review.py` |
| T7 | I/E | SSRF via user endpoints or discovery URLs | DNS resolution + private/link-local/metadata IP blocking, scheme allowlist, no credentialed redirects, size/time caps | `tests/test_ssrf.py` |
| T8 | T | Prompt injection in crawled pages | Extraction is deterministic pattern matching with no secrets, no code execution, no tool access; results are candidates only | `tests/test_extraction.py` |
| T9 | D | DoS of gateway | Request body size limit, per-key token bucket, bounded fallback hops, upstream timeouts | `tests/test_rate_limit.py` |
| T10 | R | Admin action without trail | Audit events for key, connection, vault, offer, policy, and kill-switch operations | `tests/test_audit.py` |
| T11 | E | Policy evasion (account cycling, rejected prompt retried on another identity) | POLICY_REJECTED / AUTH errors stop fallback; no identity creation code paths exist; prohibited-practice flags make offers INELIGIBLE | `tests/test_router_fallback.py` |
| T12 | I | Prompt sent to provider violating data policy | Project data-policy filter (no-training / no-retention / region allowlist) applied before dispatch | `tests/test_router_policy.py` |
| T13 | I | Gmail data over-collection | Disabled by default; production gate requires attestation; forwarded-message path stores only extracted facts; raw body discarded | `tests/test_notifications.py` |

## Residual risks
- A compromised host with access to both DB and KEK can decrypt credentials. Use a KMS-backed key provider in
  hosted deployments.
- Per-instance rate limits do not coordinate across replicas.
- Provider-reported usage may be absent; estimates are labelled and confidence is reduced, but overage is possible.
