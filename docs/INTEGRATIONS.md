# Integrations: SDKs and coding tools

Oasis Route speaks two client protocols on the same port, both authenticated with an **Oasis Key**:

| Protocol | Base URL to configure | Endpoints |
|---|---|---|
| OpenAI Chat Completions | `http://localhost:8080/v1` | `POST /v1/chat/completions`, `GET /v1/models` |
| Anthropic Messages | `http://localhost:8080` (the client appends `/v1/messages`) | `POST /v1/messages`, `POST /v1/messages/count_tokens` |

Use `auto` as the model to get the strongest suitable eligible model with failover, `auto/coding` (or `auto/reasoning`, `auto/vision`, ...) to rank by task-specific evidence, or `provider/model` to pin a model. Every response carries `x-oasis-route-provider` and `x-oasis-route-model` so you can see which upstream served it. Details: [API reference](API.md).

The examples assume a local stack (`uv run oasis-route dev` prints the key and stores it in `.oasis-dev/admin.key`):

```bash
export OASIS_API_KEY="$(cat .oasis-dev/admin.key)"
```

## Quickstart snippets

### cURL

```bash
curl -s http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $OASIS_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "auto", "messages": [{"role": "user", "content": "Say hello"}]}' -i
```

### Python (OpenAI SDK)

```python
from openai import OpenAI
import os

client = OpenAI(base_url="http://localhost:8080/v1", api_key=os.environ["OASIS_API_KEY"])
raw = client.chat.completions.with_raw_response.create(
    model="auto/coding",
    messages=[{"role": "user", "content": "Write a binary search in Go"}],
)
print(raw.headers["x-oasis-route-provider"], raw.headers["x-oasis-route-model"])
print(raw.parse().choices[0].message.content)
```

### Node.js (OpenAI SDK)

```js
import OpenAI from "openai";

const client = new OpenAI({ baseURL: "http://localhost:8080/v1", apiKey: process.env.OASIS_API_KEY });
const stream = await client.chat.completions.create({
  model: "auto",
  messages: [{ role: "user", content: "Stream a haiku" }],
  stream: true,
});
for await (const chunk of stream) process.stdout.write(chunk.choices[0]?.delta?.content ?? "");
```

### Python (Anthropic SDK)

```python
import anthropic, os

client = anthropic.Anthropic(base_url="http://localhost:8080", api_key=os.environ["OASIS_API_KEY"])
msg = client.messages.create(
    model="auto", max_tokens=512, messages=[{"role": "user", "content": "Explain idempotency keys"}]
)
print(msg.content[0].text)
```

## Coding tools

### Claude Code

Claude Code reads its gateway settings from environment variables ([Claude Code environment variables](https://code.claude.com/docs/en/env-vars)):

```bash
export ANTHROPIC_BASE_URL=http://localhost:8080
export ANTHROPIC_AUTH_TOKEN="$OASIS_API_KEY"     # sent as Authorization: Bearer
export ANTHROPIC_MODEL=auto/coding
export ANTHROPIC_DEFAULT_HAIKU_MODEL=auto          # background tasks
claude
```

`claude-*` model names that are not connected are routed as `auto`, so the built-in `sonnet`/`opus`/`haiku` aliases also work. Claude Code features that depend on Anthropic server-side tools (for example its web search tool) are not available through other providers.

### Aider

```bash
export OPENAI_API_BASE=http://localhost:8080/v1
export OPENAI_API_KEY="$OASIS_API_KEY"
aider --model openai/auto
```

### Continue (`config.yaml`)

```yaml
models:
  - name: Oasis Route (auto)
    provider: openai
    model: auto/coding
    apiBase: http://localhost:8080/v1
    apiKey: ${{ secrets.OASIS_API_KEY }}
```

### Cline / Roo Code

Choose the **OpenAI Compatible** API provider, then set Base URL `http://localhost:8080/v1`, API key = your Oasis Key, Model ID `auto/coding`.

### OpenCode (`opencode.json`)

```json
{
  "provider": {
    "oasis": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Oasis Route",
      "options": { "baseURL": "http://localhost:8080/v1", "apiKey": "{env:OASIS_API_KEY}" },
      "models": { "auto/coding": { "name": "auto (coding)" } }
    }
  }
}
```

### Open WebUI

```bash
OPENAI_API_BASE_URL=http://localhost:8080/v1 OPENAI_API_KEY="$OASIS_API_KEY" open-webui serve
```

### OpenAI Codex CLI

Current Codex releases document custom providers with `wire_api = "responses"` ([Codex advanced configuration](https://developers.openai.com/codex/config-advanced)), which needs the OpenAI Responses API. Oasis Route does not implement `/v1/responses` yet, so Codex is not supported today.

## Seeing multiple providers at work

```bash
# Which candidates would serve this request right now, and why others were rejected:
curl -s -X POST http://localhost:8080/api/v1/projects/$PROJECT_ID/route-preview \
  -H "Authorization: Bearer $OASIS_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "auto"}' | jq '.candidates[] | {provider_id, upstream_model, quality, prior}'

# Remaining free capacity across every connected account, normalised per period:
curl -s http://localhost:8080/api/v1/projects/$PROJECT_ID/capacity -H "Authorization: Bearer $OASIS_API_KEY" | jq
```
