# Threat Model

Method: STRIDE over the main trust boundaries. Each threat lists the control that implements the mitigation and
where it is tested. This document is reviewed at every release (see [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md)).

## Assets
1. Oasis Route API keys (caller credentials).
2. Upstream provider credentials (API keys, OAuth refresh tokens).
3. User prompts and provider responses (in transit only; bodies are not stored by default).
4. Quota ledger integrity (financial/quota correctness).
5. Offer registry integrity (evidence, verification decisions).
6. Admin bootstrap token and KEK (key-encryption key).
7. Pending OAuth authorizations (state, PKCE verifier) and the credentials they produce.
8. Background maintenance (the worker keeps credentials, quotas and offers correct).
9. The release artifacts (wheel, sdist, container image).

## Trust boundaries
- B1 Internet caller → gateway (`/v1/*`).
- B2 Operator/admin → admin API (`/api/v1/*`) and console.
- B3 Gateway → upstream providers (outbound).
- B4 Worker → public internet (discovery fetcher, untrusted content).
- B5 Application → database.
- B6 Provider OAuth redirect → callback (`/api/v1/oauth/callback`, unauthenticated by design).
- B7 Browser → console (`/console/`, holds an Oasis Key in session storage).
- B8 CI → published artifacts.

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
| T14 | S/E | OAuth consent phishing: attacker starts a flow and lures a victim into approving, attaching the victim's provider account to the attacker's project | Redirect flows bound to the starting browser by an HttpOnly `SameSite=Lax` cookie (hash stored); callback refuses without it; manual mode only completable by an authenticated project member | `tests/test_oauth_connections.py::test_redirect_flow_is_bound_to_the_starting_browser` |
| T15 | T/S | OAuth code or state replay, forged codes, concurrent callbacks | 256-bit state stored as SHA-256, single use, 10-minute expiry; PKCE S256 with the verifier sealed in the vault and shredded on first use; atomic claim so only one exchange happens; constant exchange URL on the provider's official domain | `tests/test_oauth_connections.py` |
| T16 | I/T | XSS or clickjacking in the console exposing the Oasis Key | No framework or inline script; server data rendered as text nodes only; strict CSP (`script-src 'self'`, `frame-ancestors 'none'`), `X-Frame-Options: DENY`, `no-referrer`; key kept in session storage only | `tests/test_zero_ops.py::test_console_is_served_with_strict_csp` |
| T17 | D | Maintenance silently stops (hung job, crashed loop, dead worker) so credentials, quotas and offers drift | Supervised worker with restart; per-job timeouts and lease renewal; exponential backoff; heartbeats; `/readyz` worker state; deduplicated admin alert on stall or repeated failure | `tests/test_zero_ops.py` |
| T18 | I/D | Master key rotation leaves credentials undecryptable, or the old key is kept forever | Previous keys accepted for decryption; automatic `vault.rewrap` job and `rotate-kek` report `remaining`; runbook removes the old key only at zero | `tests/test_kek_rotation.py` |
| T19 | T | Compromised dependency, leaked secret in history, or vulnerable base image in a release | CI pip-audit, license policy, gitleaks over full history, Trivy image scan (fails on fixable HIGH/CRITICAL), CycloneDX SBOM; releases carry signed build provenance | `.github/workflows/ci.yml`, `.github/workflows/release.yml` |
| T20 | D/T | Failover storm or partial upstream failure corrupts the ledger | Every reservation reaches a terminal state on success, failure, timeout or cancellation; seeded fault injection under concurrency | `tests/test_resilience.py` |
| T21 | I/E | Account export leaks secrets, or erasure leaves credentials behind / lets one tenant delete another | Export is built from an explicit column exclusion list (no key hashes, credentials, OAuth state/verifier, browser bindings) and audited; deletion requires owner role, `manage` scope and a `confirm` equal to the id, destroys vault rows before deleting, refuses the last administrator and shared projects; other tenants get 404 | `tests/test_data_rights.py` |

## Residual risks
- A compromised host with access to both DB and KEK can decrypt credentials. Use a KMS-backed key provider in
  hosted deployments.
- Per-instance rate limits do not coordinate across replicas (quota reservations do; they are global).
- Provider-reported usage may be absent; estimates are labelled and confidence is reduced, but overage is possible.
- `/metrics` has no authentication; deployments must keep it on a private network (documented in DEPLOYMENT.md).
- An Oasis Key in browser session storage is readable by any script that runs on the console origin; the CSP
  is the control. Do not serve other applications from the same origin.
- Only one redirect OAuth flow per browser can be pending at a time (a newer start replaces the binding cookie).

## Review log

| Date | Version | Reviewer | Outcome |
|---|---|---|---|
| 2026-11-17 | 0.1.0 | Release review | T14 found during review (OAuth consent phishing) and fixed before release. No open critical or high findings. Dependency audit clean, no secrets in history, container scan clean of fixable HIGH/CRITICAL. |
| 2026-11-17 | 0.1.0 + unreleased | Release checklist | T21 added with account export/erasure. Install from wheel and upgrade from 0.1.0 with live data verified and enforced in CI. No open critical or high findings. |
