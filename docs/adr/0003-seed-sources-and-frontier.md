# ADR-0003: Reviewed seed sources and a bounded frontier instead of open crawling

- Status: Accepted
- Date: 2026-11-17

## Context
Live discovery should find new and changed free offers without an operator adding every URL, while respecting
provider terms, robots.txt and rate limits, and without turning page text into trusted facts.

## Decision
- Ship a small list of maintainer-reviewed official pages and public catalogue APIs (`discovery/seeds.py`).
  They are inserted only if missing, so an admin's decision to block or edit one is never overwritten.
- Expand only from official HTML pages, only to links on the provider's official domains, only for paths that
  look like pricing, limits, models, free tiers, terms or changelogs, never to account or privacy pages; depth 1
  and 25 pages per provider by default.
- robots.txt is checked before every fetch; an unreachable robots file means "do not fetch".
- Catalogue providers that are not yet in the registry become `ProviderCandidate` rows for an admin to accept.
- Every offer starts in `needs_review`. Verification requires citing evidence. Users only see reviewed offers.

## Consequences
- Coverage grows with the seed list and frontier rather than with a general crawler; new providers need either a
  catalogue mention or an admin-added source.
- No general web search is used. If a permitted search API is added later it should create sources in
  `pending` review, not fetch results directly.
