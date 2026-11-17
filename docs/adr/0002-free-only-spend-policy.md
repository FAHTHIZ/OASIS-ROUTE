# ADR-0002: Free-only spend policy by default

- Status: Accepted
- Date: 2026-11-17

## Context
The product promise is free capacity through one Oasis Key. A provider key can be attached to a funded account,
and most provider APIs do not say in a response whether the call was billed. Routing to such a connection
without the owner noticing would silently incur paid usage.

## Decision
- Every project has a `spend_policy`: `free_only` (default for new projects) or `allow_paid`.
- Every connection has a `billing_mode`: `free_tier`, `paid` or `unknown`. The default comes from what the
  provider documents (`connections/access.py`); it is refined from the provider's own account API where one
  exists (OpenRouter `GET /api/v1/key` `is_free_tier`, Hugging Face `whoami-v2` `canPay`), or set by the owner.
- The router computes a `cost_basis` per candidate: `local`, `free_model` (the provider marks the model free,
  for example OpenRouter `:free`), `free_tier_account`, `paid` or `unknown`. Under `free_only` only the first
  three are eligible; others are rejected with `paid_route_blocked` or `cost_unknown`, visible in route preview.
- Switching a project to `allow_paid` requires `acknowledge_paid_usage: true` and is audited.

## Consequences
- A fresh install with only paid-default providers routes nothing until the owner confirms an account is free
  tier or allows paid usage. The status page says so ("Action required: free-only mode ...").
- `unknown` is treated as possibly paid. This is deliberately conservative.
