# Unified catalogue

OASIS Route ships two layers of provider and model data:

| Layer | Where | Trust | Routable |
|---|---|---|---|
| Curated seeds | `src/oasis_route/registry/catalog.py` | Reviewed against each provider's own terms and docs (`terms_url`, `data_policy` sources) | Yes, once connected |
| Imported catalogue | `src/oasis_route/registry/data/omniroute_catalog.json` | **Advertised**: a third-party research snapshot from [OmniRoute](https://github.com/diegosouzapw/OmniRoute) (MIT) | Only after the provider confirms the model |

The import gives you OmniRoute's reach (about 220 providers, 126 of them connectable through an API key, and about 2,000 models with context windows, capabilities, prices and advertised free tiers) without weakening any OASIS Route guarantee.

## Rules

- **Advertised is not verified.** Every imported provider, model and free-tier claim carries provenance: source repository, commit, file URL, curation date (`free_catalog_curated_at`), import time, licence and `review_state: "unreviewed"`. It is exposed under `catalog` on providers and models.
- **Imported models are not routable until confirmed.** They are stored with `lifecycle: "catalog"`, hidden from `/v1/models`, and rejected by the router with `catalog_model_unconfirmed`. When a connection's model sync finds the model in the provider's own authenticated model list, it becomes `active`.
- **Imported free tiers never make a route free.** `Model.pricing.free` is only set by the provider's own listing or an operator. Imported free-tier pools become offers in state `discovered` (or `ineligible` for blocked providers), confidence 0.2, with an `Evidence` row (`method: "catalog_import"`) and an `OfferTransition` by `system:catalog_import`. They count toward **advertised** capacity only and wait in the review queue; promoting one to `verified` needs a reviewer and a primary source.
- **Imported prices are estimates.** `pricing.prompt_usd_per_mtok` / `completion_usd_per_mtok` with `verified: false` and `source: "omniroute_catalog"`. They are used for worst-case cost estimates, never to claim a route is free. A missing price is unknown, not zero.
- **Curated seeds win.** For a provider that has a curated seed, the import only records its `catalog` facts and adds missing models as `catalog`. Reviewed fields are never overwritten. Operator edits are never overwritten either: the sync is additive.
- **Blocked stays blocked.** Providers OmniRoute reaches through web-session automation, CLI or IDE OAuth impersonation, reverse-engineered proxies, coding-plan quotas, or terms that prohibit proxy use are imported with `status: "blocked"` and the reason (for example `web_session`, `cli_oauth_impersonation`, `tos_prohibits_proxy`). Providers that need a protocol OASIS Route does not speak yet are `unsupported`. Neither can be connected or routed; they are listed so you can see why.

## Capacity layers

Imported offers feed the existing capacity layers ([ARCHITECTURE.md](ARCHITECTURE.md)) without crossing them:

| Layer | Includes imported data? |
|---|---|
| advertised | Yes: `discovered` imported offers |
| verified | No: needs review against a primary source |
| authorized | No: needs a connection you own |
| usable | No: needs a confirmed model on an active connection |
| consumed / forecast | Only from your own usage |

## Refreshing the snapshot

```bash
git clone https://github.com/diegosouzapw/OmniRoute /tmp/OmniRoute   # or the fork you track
cd /tmp/OmniRoute && npx -y tsx --tsconfig tsconfig.json \
  /path/to/OASIS-ROUTE/scripts/omniroute/export_catalog.mts > /tmp/omniroute_raw.json
cd /path/to/OASIS-ROUTE
python3 scripts/import_omniroute_catalog.py /tmp/omniroute_raw.json --commit "$(git -C /tmp/OmniRoute rev-parse HEAD)"
```

The importer maps OmniRoute ids onto curated ids where they exist, classifies each provider, strips path suffixes from base URLs, and records the auth style (`bearer`, `x-api-key` or a named header). Review the diff before committing. Starting the server (or `oasis-route migrate`) adds new rows; nothing is deleted.

Set `OASIS_ROUTE_CATALOG_IMPORT_ENABLED=false` to run with the curated seeds only.
