// Export OmniRoute's provider/model/free-tier/pricing catalogue as plain JSON.
//
// Run from the root of an OmniRoute checkout (MIT licence) so its tsconfig path aliases resolve:
//   npx -y tsx --tsconfig tsconfig.json /path/to/OASIS-ROUTE/scripts/omniroute/export_catalog.mts > omniroute_raw.json
// Then normalise it with scripts/import_omniroute_catalog.py. Only data is read; no OmniRoute code is copied.
import { REGISTRY } from "./open-sse/config/providers/index.ts";
import { FREE_MODEL_BUDGETS, FREE_CATALOG_CURATED_AT } from "./open-sse/config/freeModelCatalog.data.ts";
import { FREE_TIER_TOS } from "./open-sse/config/freeTierCatalog.ts";
import { APIKEY_PROVIDERS } from "./src/shared/constants/providers/apikey/index.ts";
import { LOCAL_PROVIDERS } from "./src/shared/constants/providers/local.ts";
import { DEFAULT_PRICING } from "./src/shared/constants/pricing/default-pricing.ts";

const plain = (o: unknown) => JSON.parse(JSON.stringify(o, (_k, v) => (typeof v === "function" ? "<function>" : v)));
const registry: Record<string, unknown> = {};
for (const [id, e] of Object.entries(REGISTRY as Record<string, any>)) {
  // OAuth client ids/secrets are never exported; only whether the entry relies on OAuth.
  registry[id] = plain({ ...e, oauth: e.oauth ? { present: true } : undefined });
}
process.stdout.write(
  JSON.stringify({
    registry,
    free_models: FREE_MODEL_BUDGETS,
    free_catalog_curated_at: FREE_CATALOG_CURATED_AT,
    tos: FREE_TIER_TOS,
    display: plain(APIKEY_PROVIDERS),
    local: plain(LOCAL_PROVIDERS),
    pricing: plain(DEFAULT_PRICING),
  }),
);
