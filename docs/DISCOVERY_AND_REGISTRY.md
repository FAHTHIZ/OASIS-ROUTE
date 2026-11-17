# Discovery and Offer Registry

## 1. Goal

Continuously discover legitimate public inference offers and changes to existing offers. No crawler can guarantee coverage of the entire internet; use targeted source discovery, incremental crawling, search/index integrations where permitted, and community-reviewed submissions.

## 2. Source classes

Prioritize:
1. Official provider API documentation and pricing/quota pages.
2. Official developer announcements and changelogs.
3. Official provider GitHub organizations and SDK repositories.
4. Public model catalogues and inference platform documentation.
5. Public, reputable curated directories, treated as leads rather than authoritative evidence.
6. User-submitted URLs and evidence, clearly marked as unverified until reviewed.

Do not crawl private account pages, authenticated dashboards, leaked dumps, secret repositories, or unrelated personal data.

## 3. Source policy

Every source connector must declare:
- canonical domain and URL scope;
- allowed request methods;
- terms/robots review status;
- request rate and concurrency budget;
- crawl interval and retention;
- user-agent/contact identification where appropriate;
- conditional request support (ETag/Last-Modified);
- error and backoff policy.

Respect applicable site terms, robots directives, rate limits, access controls, and privacy law. Do not circumvent CAPTCHAs, logins, paywalls, blocks, or technical restrictions.

## 4. Offer lifecycle

`DISCOVERED -> NEEDS_REVIEW -> VERIFIED_ELIGIBLE -> CONNECTABLE -> ACTIVE`

Alternative terminal/intermediate states:
`REJECTED`, `INELIGIBLE`, `EXPIRED`, `SUSPENDED`, `STALE`, `REVOKED`.

A discovered page is not proof that a user qualifies. A verified offer is not an authorized connection. An authorized connection is not proof that quota remains.

## 5. Evidence record

Store:
- source URL and publisher;
- retrieval timestamp and HTTP metadata;
- relevant text excerpt or permitted snapshot;
- hash of captured evidence;
- claim being supported (quota, price, age/audience, region, model, expiry, use restrictions);
- reviewer/verification method;
- confidence and next review date.

Avoid retaining full pages where excerpts and hashes suffice. Honour source licences and retention requirements.

## 6. Suggested domain model

- Provider: identity, official domains, policy URL, status.
- Model: provider model ID, aliases, capabilities, context limits, lifecycle.
- Offer: access mode, quota claims, price/credit type, eligibility, expiry, terms, evidence references.
- Source: source connector, scope, crawl policy, last successful check.
- QuotaPool: shared accounting scope and limits.
- Connection: user-owned provider account reference and authorization state.
- Endpoint: base URL, protocol, region, health state, adapter key.
- Verification: evidence, reviewer, decision, rationale, timestamp.
- ChangeEvent: old/new values, source, severity, action required.

## 7. Deduplication

Deduplicate offers using provider identity, account/project scope, model family, quota scope, and reset period. If multiple credentials share one upstream project budget, they must not multiply aggregate capacity.

## 8. Change detection

On changed source content:
- diff normalized relevant sections;
- re-extract claims;
- mark material changes for review;
- suspend affected offer if eligibility or terms become unclear;
- notify connected users when an action is required.

## 9. Capacity estimates

Only count a pool in current usable capacity when the user's connection is valid, policy eligibility is confirmed, the endpoint is healthy, quota is available, and the offer has not expired. Show source and confidence alongside totals.
