# Turnkey onboarding

From a fresh install to one OpenAI-compatible endpoint, without enabling anything that can bill you.

```bash
uvx --from git+https://github.com/FAHTHIZ/OASIS-ROUTE oasis-route dev --open
```

That one command starts the server, connects any local model server it finds (Ollama, LM Studio, vLLM, ...), and opens the console already signed in (the printed `console:` link carries the key in its `#key=` fragment, which never reaches the server and is removed from the address bar). The **Get started** page then shows, in order:

1. whether real models are connected (or only the built-in demo, whose answers are echoes);
2. a **Send test message** button that shows which provider answered;
3. links to create free, no-card keys at Groq, Gemini, OpenRouter, Cerebras, Mistral, SambaNova, NVIDIA, Cohere, Z.ai and Hugging Face;
4. a box to paste all those keys at once, with the two confirmations (your own accounts; no billing set up);
5. the endpoint and settings for coding tools.

The same from a terminal, in a second shell in the same directory (the dev key is picked up automatically):

```bash
oasis-route setup      # what is connected, where to get free keys, route counts
oasis-route connect    # paste keys (hidden), confirm, done
oasis-route billing groq free_tier   # only if you connected a free-plan key without saying so
```

Scripts use `oasis-route connect --from-env --accept-terms --free-tier` (or `--env-file .env`, `--stdin`).

The built-in demo providers (`mock`, `mock_alt`) answer `auto` only while nothing real is connected; once a local model or a hosted key is active they step aside (`demo_provider_superseded`), and you can still pin them with `model=mock/mock-small`.

| Step | What happens | API |
|---|---|---|
| 1. Start | `oasis-route dev` (single user, SQLite, private upstreams allowed) or `oasis-route serve`. | |
| 2. Detect local providers | Each local preset (Ollama `11434`, vLLM `8000`, LM Studio `1234`, llama.cpp / llamafile `8080`, Lemonade `13305`, Docker Model Runner `12434`, Xinference `9997`) is probed once at `http://127.0.0.1:<port>/v1/models` with a 1 s timeout and no credentials. The server's own port is skipped. Detection only runs when `OASIS_ROUTE_ALLOW_PRIVATE_UPSTREAMS=true`. | `GET /projects/{id}/setup` |
| 3. Show free providers | `free_keys`: where to create a no-card key for each reviewed hosted provider, with its environment variable and free-tier note, and whether it is connected. `connectable_providers.verified`: providers with a reviewed free offer (`verified_eligible`, `connectable`, `active`). `connectable_providers.advertised`: free tiers from the imported catalogue ([CATALOG.md](CATALOG.md)) that nobody has reviewed yet, labelled `advertised`. | same |
| 4. Connect | Paste keys (`oasis-route connect`, console **Paste your keys**), sign in with OAuth where offered, or let setup connect detected local servers. `<PROVIDER>_API_KEY` variables are recognised for every connectable imported provider. | `POST /projects/{id}/connections`, `.../connections/quick`, `POST /projects/{id}/setup` |
| 5. Validate and discover | Each credential is checked only with its issuer; models come from the provider's own list. Imported catalogue models become routable only when that list confirms them. | |
| 6. Classify | Every route gets `cost_class` (`free`, `paid`, `unknown`) with `free_evidence` (`local`, `provider_price_list`, `account_declared`) and `state` (`eligible`, `blocked`, `exhausted`) with the reason. | `GET /projects/{id}/routes` |
| 7. Default route set | New projects are zero-budget (`free_only`). Detected local servers are connected at priority `-10`, so hosted free tiers are used first and the uncapped local model is the last resort. | `POST /projects/{id}/setup` |
| 8. One endpoint | `OPENAI_BASE_URL=<server>/v1`, `OPENAI_API_KEY=$OASIS_API_KEY`, model `auto`. Anthropic clients use `<server>/v1/messages`. See [INTEGRATIONS.md](INTEGRATIONS.md). | `endpoint` in the setup response |

## What setup never does

- It never changes the project's spend policy or a connection's `billing_mode`, so no paid route becomes routable. Paid fallback needs an explicit change by the project owner (see [ROUTING_AND_QUOTAS.md](ROUTING_AND_QUOTAS.md)).
- It never marks an imported (advertised) free tier as verified, and never sends a key to a provider other than the one it was issued by.
- It never probes anything but loopback default ports, and only on a host that allows private upstreams.

`billing_attention` lists connections whose routes are held back only because nobody said whether billing is set up (`billing_mode=unknown`); `oasis-route billing <provider> free_tier` or the console button fixes it.

`oasis-route setup` exits `0` when at least one free route on a real model is eligible (demo routes do not count), `3` when none is (connect a provider), `2` without an Oasis Key and `1` on an HTTP error. `--json` prints the full response; `--no-local` skips connecting local servers; `--dry-run` only reports.
