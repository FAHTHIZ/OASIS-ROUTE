# Local models (llama.cpp, Ollama, vLLM)

Any server that speaks the OpenAI Chat Completions API can be an Oasis Route provider. A model running on your own machine is the only "free tier" that has no daily cap. It is a good last fallback when every hosted free tier is rate-limited.

Oasis Route reaches local servers through the built-in `custom` provider. Private and loopback addresses are blocked by default (SSRF protection), so turn them on first:

```bash
export OASIS_ROUTE_ALLOW_PRIVATE_UPSTREAMS=true   # only on a single-user host
```

Keep this setting off on shared or public deployments. See [CONFIGURATION.md](CONFIGURATION.md).

## llama.cpp

[llama.cpp's `llama-server`](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md) serves OpenAI-compatible chat, completions and embeddings routes. Prebuilt CPU binaries are attached to each [llama.cpp release](https://github.com/ggml-org/llama.cpp/releases) (`llama-<build>-bin-ubuntu-x64.tar.gz` on Linux x86-64).

```bash
# 1. Download a small GGUF model (about 490 MB) and start the server.
curl -L -o qwen.gguf \
  https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf
./llama-server -m qwen.gguf --host 127.0.0.1 --port 9200 -c 4096 \
  --alias qwen2.5-0.5b-instruct

# 2. Connect it to your Oasis Route project.
curl -X POST http://127.0.0.1:8080/api/v1/projects/$PROJECT_ID/connections \
  -H "Authorization: Bearer $OASIS_API_KEY" -H 'content-type: application/json' \
  -d '{"provider_id":"custom","api_key":"local-no-key",
       "base_url":"http://127.0.0.1:9200/v1","accept_terms":true}'
```

`llama-server` ignores the key, but Oasis Route requires a non-empty value, so any placeholder works. `--alias` sets the model ID that Oasis Route discovers from `/v1/models`.

## Ollama

Ollama's local server exposes the OpenAI-compatible API at `http://localhost:11434/v1` and ignores the API key ([Ollama docs](https://docs.ollama.com/api/openai-compatibility)).

```bash
ollama pull llama3.2
curl -X POST http://127.0.0.1:8080/api/v1/projects/$PROJECT_ID/connections \
  -H "Authorization: Bearer $OASIS_API_KEY" -H 'content-type: application/json' \
  -d '{"provider_id":"custom","api_key":"ollama",
       "base_url":"http://127.0.0.1:11434/v1","accept_terms":true}'
```

vLLM, LM Studio and other OpenAI-compatible servers work the same way: point `base_url` at the server's `/v1` prefix.

## How local models are ranked

Local models usually have no published benchmark score, so the router ranks them with the name-based prior described in [ROUTING_AND_QUOTAS.md](ROUTING_AND_QUOTAS.md#unrated-models-and-name-based-priors). Rated models always sort before unrated ones, and a prior never satisfies `min_quality`. In practice a small local model is chosen only when you ask for it by name (`custom/<model>`) or when every rated candidate has failed, which is the intended "last resort" behaviour.

To force it, call the model by its routed ID:

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer $OASIS_API_KEY" -H 'content-type: application/json' \
  -d '{"model":"custom/qwen2.5-0.5b-instruct",
       "messages":[{"role":"user","content":"Name three primary colors."}]}'
```

## Verified end-to-end run

On 2026-11-17 the following ran against `oasis-route dev` (two mock providers) plus a real Qwen2.5-0.5B-Instruct Q4_K_M model served by llama.cpp build b12466 on a 2-CPU machine:

| Check | Result |
| --- | --- |
| Connection validation | `active`, 1 model discovered |
| `/v1/models` | `custom/qwen2.5-0.5b-instruct` listed with `quality_prior` 29.25 |
| `/v1/chat/completions` (`custom/qwen2.5-0.5b-instruct`) | "Three primary colors are red, blue, and yellow." with `x-oasis-route-provider: custom` |
| `/v1/messages` (Anthropic shape) | "Bonjour!", `stop_reason: end_turn` |
| `/v1/messages` with `stream: true` | `message_start`, `content_block_*`, `message_delta`, `message_stop` events |
| `model: auto` with every mock model failing (429 or 503) | Answered by `custom/qwen2.5-0.5b-instruct`, `downgraded: true` |
