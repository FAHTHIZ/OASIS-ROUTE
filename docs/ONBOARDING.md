# Turnkey onboarding

From a fresh install to one OpenAI-compatible endpoint, without enabling anything that can bill you.

```bash
uvx oasis-route dev                       # 1. start (prints the console URL and an Oasis Key)
export OASIS_API_KEY=...                  #    from the dev output
oasis-route setup                         # 2-3, 6-8. detect local servers, list free providers, classify routes
oasis-route connect --from-env --accept-terms --free-tier   # 4-5. connect your own keys
oasis-route setup --dry-run               # re-check routes
```

| Step | What happens | API |
|---|---|---|
| 1. Start | `oasis-route dev` (single user, SQLite, private upstreams allowed) or `oasis-route serve`. | |
| 2. Detect local providers | Each local preset (Ollama `11434`, vLLM `8000`, LM Studio `1234`, llama.cpp / llamafile `8080`, Lemonade `13305`, Docker Model Runner `12434`, Xinference `9997`) is probed once at `http://127.0.0.1:<port>/v1/models` with a 1 s timeout and no credentials. The server's own port is skipped. Detection only runs when `OASIS_ROUTE_ALLOW_PRIVATE_UPSTREAMS=true`. | `GET /projects/{id}/setup` |
| 3. Show free providers | `connectable_providers.verified`: providers with a reviewed free offer (`verified_eligible`, `connectable`, `active`). `connectable_providers.advertised`: free tiers from the imported catalogue ([CATALOG.md](CATALOG.md)) that nobody has reviewed yet, labelled `advertised`. | same |
| 4. Connect | Paste keys (`oasis-route connect`, console **Paste your keys**), sign in with OAuth where offered, or let setup connect detected local servers. `<PROVIDER>_API_KEY` variables are recognised for every connectable imported provider. | `POST /projects/{id}/connections`, `.../connections/quick`, `POST /projects/{id}/setup` |
| 5. Validate and discover | Each credential is checked only with its issuer; models come from the provider's own list. Imported catalogue models become routable only when that list confirms them. | |
| 6. Classify | Every route gets `cost_class` (`free`, `paid`, `unknown`) with `free_evidence` (`local`, `provider_price_list`, `account_declared`) and `state` (`eligible`, `blocked`, `exhausted`) with the reason. | `GET /projects/{id}/routes` |
| 7. Default route set | New projects are zero-budget (`free_only`). Detected local servers are connected at priority `-10`, so hosted free tiers are used first and the uncapped local model is the last resort. | `POST /projects/{id}/setup` |
| 8. One endpoint | `OPENAI_BASE_URL=<server>/v1`, `OPENAI_API_KEY=$OASIS_API_KEY`, model `auto`. Anthropic clients use `<server>/v1/messages`. See [INTEGRATIONS.md](INTEGRATIONS.md). | `endpoint` in the setup response |

## What setup never does

- It never changes the project's spend policy or a connection's `billing_mode`, so no paid route becomes routable. Paid fallback needs an explicit change by the project owner (see [ROUTING_AND_QUOTAS.md](ROUTING_AND_QUOTAS.md)).
- It never marks an imported (advertised) free tier as verified, and never sends a key to a provider other than the one it was issued by.
- It never probes anything but loopback default ports, and only on a host that allows private upstreams.

`oasis-route setup` exits `0` when at least one free route is eligible, `3` when none is (connect a provider), `2` without an Oasis Key and `1` on an HTTP error. `--json` prints the full response; `--no-local` skips connecting local servers; `--dry-run` only reports.
