# ADR-0001: Stack and application shape

- Status: Accepted
- Date: 2026-11-17

## Context
The repository contained only architecture documents. The handoff requires one coherent backend, persistence
layer, scheduler approach, and UI strategy, as a modular monolith, with PostgreSQL authoritative for quota.

## Decision
- Python ≥ 3.12, FastAPI/Uvicorn, SQLAlchemy 2 async, Alembic, httpx, `cryptography`, Pydantic v2.
- PostgreSQL in production; SQLite allowed for local development and the fast unit-test tier.
- Quota reservations are single-statement conditional `UPDATE ... WHERE remaining >= n` operations executed
  inside one transaction across all dimensions a request touches. If any dimension fails, the transaction rolls
  back. This is atomic on PostgreSQL (row lock + re-evaluated predicate under READ COMMITTED) and on SQLite
  (database-level write lock), and is idempotent via a unique `(request_id)` reservation key.
- A single process image provides `serve` (API + gateway + static admin console) and `worker` (DB-leased jobs).
- No external broker. Redis is not required; in-process token-bucket rate limiting is used per instance and is
  documented as per-instance.
- Apache-2.0 license.

## Consequences
- One-command local start (`make dev` / `uv run oasis-route dev`) with SQLite and a bundled mock provider.
- CI runs the suite against both SQLite and PostgreSQL to prove ledger concurrency on the production engine.
- Multi-instance deployments must use PostgreSQL; per-instance rate limits are additive across instances.
