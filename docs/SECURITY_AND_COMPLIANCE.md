# Security, Privacy, and Provider Compliance

## 1. Security posture

Oasis Route handles application API keys, upstream provider credentials, user prompts, provider responses, and potentially email metadata. Treat all as sensitive.

## 2. Credentials

### Oasis Key

Oasis Key is the user-facing name for the unified API credential that applications present to the Oasis Route gateway. The documented client environment variable is `OASIS_API_KEY`, sent with `Authorization: Bearer <OASIS_API_KEY>`. A user can use this gateway credential across connected providers; it does not replace or expose the user's separate upstream provider credentials.

- Hash Oasis Keys with a password-grade one-way hash or keyed hash suitable for high-entropy tokens; store a prefix for lookup and show full value only once.
- Encrypt upstream API keys and refresh tokens at rest using envelope encryption with a separately managed key-encryption key.
- Support revocation, rotation, scopes, and expiry for Oasis Keys.
- Separate encryption keys by environment; never commit secrets.
- Redact credentials in logs, traces, exception reports, analytics, and exports.
- Restrict access to vault operations and record audit events.
- Prefer a managed secret store/KMS in hosted deployments.

## 3. User data and prompts

- Publish clear data-flow and retention documentation.
- Do not train on prompts or provider responses.
- Provide per-project provider allowlists/denylists and data handling controls.
- Avoid storing prompt/response bodies by default. Store minimal metadata and usage.
- Provide deletion and export paths.
- Do not send a prompt to an upstream provider that conflicts with the user's policy or required region.

## 4. Provider eligibility

Before activation, verify:
- who is eligible (including age/audience restrictions);
- country/region availability;
- permitted use and commercial/non-commercial conditions;
- account and project limits;
- whether credits/quotas are personal, transferable, poolable, or resellable;
- expiration and reset terms;
- automated access restrictions;
- model and endpoint availability.

If any material term is unclear, mark the offer `NEEDS_REVIEW` or `INELIGIBLE`. Never infer permission from an endpoint being publicly reachable.

## 5. Prohibited behavior

No credential harvesting, leaked-key use, identity/account farming, disposable-email registration, trial cycling, CAPTCHA bypass, rate-limit evasion, paywall/auth bypass, or use of accounts without the owner's authorization. Do not rotate accounts to defeat a quota. Failover may select another separately authorized and policy-eligible allocation; it must not evade a provider's intended per-user/project limits.

## 6. Gmail integration gate

Gmail is optional and must not be in the MVP critical path.

Google classifies `gmail.readonly` as a restricted scope. Google states that server-side storage or transmission of restricted-scope data requires a security assessment. Before implementing or enabling Gmail:
- review current Google API Services User Data Policy and OAuth verification requirements;
- choose the narrowest possible scope;
- obtain explicit, informed consent;
- explain exactly which messages/metadata are processed and why;
- process only provider-related notifications using narrowly scoped search/filter logic;
- avoid retaining message bodies; extract only required notification facts and delete raw content promptly;
- implement revocation, deletion, retention limits, encryption, access audit, and incident response;
- complete applicable OAuth verification and security assessment before production use.

Do not send email, register provider accounts, or accept terms on the user's behalf. Gmail can classify provider notices, expiry alerts, and onboarding messages; it is not a credential-harvesting or account-creation system.

## 7. Age and audience eligibility

Eligibility is provider-specific and may change. The registry must store audience/age restrictions and enforce them before connection or routing. Do not help a user bypass age requirements. If a provider's terms prohibit service for a particular audience, mark it unavailable to that audience.

## 8. Crawler security

- Treat pages, repositories, issue comments, model cards, and user-submitted URLs as untrusted data and prompt-injection sources.
- The extraction agent has no secrets and no ability to execute arbitrary page instructions.
- Do not run downloaded code, install packages, or execute repository scripts as part of discovery.
- Restrict outbound network destinations; defend against SSRF, redirects to private IPs, DNS rebinding, oversized content, archive bombs, and malicious documents.
- Use bounded fetch sizes, timeouts, concurrency, and content types.
- Keep crawler credentials separate from inference credentials.

## 9. Abuse and operations

Add per-user and per-key rate limits, spend/capacity limits where applicable, request size limits, audit logs, anomaly detection, admin kill switches, and incident runbooks. Protect the gateway against prompt abuse, denial of service, and credential stuffing.
