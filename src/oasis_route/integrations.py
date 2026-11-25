"""Ready-to-paste client configurations for coding tools and SDKs.

Each recipe names the wire protocol the tool speaks (OpenAI Chat Completions, OpenAI Responses or
Anthropic Messages), which Oasis Route endpoint serves it, the environment variables and config files to
set, and known limits. Recipes never contain a secret: they reference ``$OASIS_API_KEY`` (or the tool's
own env-var syntax) so output can be pasted into shared docs or dotfiles safely.

Every endpoint below returns route metadata (``x-oasis-route-*`` headers and an ``oasis_route`` body
object where the format allows) and uses the same quota, budget, spend-mode and fallback rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

KEY_ENV = "OASIS_API_KEY"


@dataclass(frozen=True)
class Recipe:
    id: str
    name: str
    protocol: str  # openai_chat | openai_responses | anthropic_messages
    model: str
    env: dict[str, str] = field(default_factory=dict)
    files: list[dict[str, str]] = field(default_factory=list)  # {path, format, content}
    steps: list[str] = field(default_factory=list)
    run: str | None = None
    notes: list[str] = field(default_factory=list)
    docs_url: str | None = None


ENDPOINTS = {
    "openai_chat": "/v1/chat/completions",
    "openai_responses": "/v1/responses",
    "anthropic_messages": "/v1/messages",
}


def _recipes(base: str) -> list[Recipe]:
    v1 = f"{base}/v1"
    key = f"${KEY_ENV}"
    return [
        Recipe(
            "claude_code",
            "Claude Code",
            "anthropic_messages",
            "auto/coding",
            env={
                "ANTHROPIC_BASE_URL": base,
                "ANTHROPIC_AUTH_TOKEN": key,
                "ANTHROPIC_MODEL": "auto/coding",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": "auto",
            },
            run="claude",
            notes=[
                "claude-* names that are not connected route as auto, so the sonnet/opus/haiku aliases work.",
                "Anthropic server-side tools (web search) are unavailable through other providers.",
            ],
            docs_url="https://code.claude.com/docs/en/env-vars",
        ),
        Recipe(
            "codex",
            "OpenAI Codex CLI",
            "openai_responses",
            "auto/coding",
            env={KEY_ENV: "<your Oasis Key>"},
            files=[
                {
                    "path": "~/.codex/config.toml",
                    "format": "toml",
                    "content": (
                        'model = "auto/coding"\n'
                        'model_provider = "oasis"\n\n'
                        "[model_providers.oasis]\n"
                        'name = "Oasis Route"\n'
                        f'base_url = "{v1}"\n'
                        f'env_key = "{KEY_ENV}"\n'
                        'wire_api = "responses"\n'
                    ),
                }
            ],
            run="codex",
            notes=[
                "Codex sends the whole conversation each turn, which matches the stateless gateway.",
                "Hosted tools (web search) are dropped; function tools (shell, apply_patch) are routed.",
            ],
            docs_url="https://developers.openai.com/codex/config-advanced",
        ),
        Recipe(
            "cursor",
            "Cursor",
            "openai_chat",
            "auto",
            steps=[
                "Settings > Models: enable OpenAI API Key and paste your Oasis Key.",
                f"Enable Override OpenAI Base URL and set it to {v1} (must be public HTTPS, see notes).",
                "Add a custom model named auto and select it in chat or Agent.",
            ],
            notes=[
                "Cursor sends these requests from its own servers, so localhost and LAN addresses are "
                "refused. Use a deployed Oasis Route or an HTTPS tunnel (ngrok, Cloudflare Tunnel), and keep "
                "Oasis Key auth on because the endpoint becomes internet-reachable.",
                "Use the plain model name auto: Cursor may strip characters such as '/', '-' and ':' from "
                "custom model names.",
                "The override applies to every OpenAI-format model; Tab completion keeps Cursor's models.",
                "Prompts pass through Cursor's backend even with your own endpoint.",
            ],
            docs_url="https://docs.cursor.com/settings/api-keys",
        ),
        Recipe(
            "cline",
            "Cline",
            "openai_chat",
            "auto/coding",
            steps=[
                "API Provider: OpenAI Compatible.",
                f"Base URL: {v1}",
                "API Key: your Oasis Key.",
                "Model ID: auto/coding (or <provider>/<model> to pin one).",
            ],
            notes=["Cline needs tool calling; auto only picks models that declare tools support."],
            docs_url="https://docs.cline.bot/provider-config/openai-compatible",
        ),
        Recipe(
            "roo_code",
            "Roo Code",
            "openai_chat",
            "auto/coding",
            steps=[
                "API Provider: OpenAI Compatible.",
                f"Base URL: {v1}",
                "API Key: your Oasis Key.",
                "Model: auto/coding.",
            ],
            docs_url="https://docs.roocode.com/providers/openai-compatible",
        ),
        Recipe(
            "aider",
            "Aider",
            "openai_chat",
            "openai/auto/coding",
            env={"OPENAI_API_BASE": v1, "OPENAI_API_KEY": key},
            run="aider --model openai/auto/coding",
            notes=["The openai/ prefix tells Aider to use the OpenAI-compatible client."],
            docs_url="https://aider.chat/docs/llms/openai-compat.html",
        ),
        Recipe(
            "opencode",
            "OpenCode",
            "openai_chat",
            "oasis/auto/coding",
            files=[
                {
                    "path": "opencode.json",
                    "format": "json",
                    "content": (
                        "{\n"
                        '  "$schema": "https://opencode.ai/config.json",\n'
                        '  "provider": {\n'
                        '    "oasis": {\n'
                        '      "npm": "@ai-sdk/openai-compatible",\n'
                        '      "name": "Oasis Route",\n'
                        f'      "options": {{ "baseURL": "{v1}", "apiKey": "{{env:{KEY_ENV}}}" }},\n'
                        '      "models": { "auto/coding": { "name": "auto (coding)" } }\n'
                        "    }\n"
                        "  }\n"
                        "}\n"
                    ),
                }
            ],
            run="opencode",
            docs_url="https://opencode.ai/docs/providers/",
        ),
        Recipe(
            "continue",
            "Continue",
            "openai_chat",
            "auto/coding",
            files=[
                {
                    "path": "~/.continue/config.yaml",
                    "format": "yaml",
                    "content": (
                        "models:\n"
                        "  - name: Oasis Route (auto)\n"
                        "    provider: openai\n"
                        "    model: auto/coding\n"
                        f"    apiBase: {v1}\n"
                        f"    apiKey: ${{{{ secrets.{KEY_ENV} }}}}\n"
                    ),
                }
            ],
        ),
        Recipe(
            "openai_sdk",
            "OpenAI SDKs (Python, Node) and any OpenAI-compatible client",
            "openai_chat",
            "auto",
            env={"OPENAI_BASE_URL": v1, "OPENAI_API_KEY": key},
            notes=[
                "Streaming, tools, response_format and stream_options.include_usage are passed through.",
                "Route metadata: x-oasis-route-* headers and the oasis_route body field.",
            ],
        ),
        Recipe(
            "anthropic_sdk",
            "Anthropic SDKs",
            "anthropic_messages",
            "auto",
            env={"ANTHROPIC_BASE_URL": base, "ANTHROPIC_API_KEY": key},
        ),
    ]


ALIASES = {"claude": "claude_code", "roo": "roo_code", "roocode": "roo_code", "openai": "openai_sdk"}


def recipes(base_url: str) -> list[dict[str, Any]]:
    return [_out(r, base_url.rstrip("/")) for r in _recipes(base_url.rstrip("/"))]


def recipe(tool: str, base_url: str) -> dict[str, Any] | None:
    tid = ALIASES.get(tool.lower().replace("-", "_"), tool.lower().replace("-", "_"))
    return next((r for r in recipes(base_url) if r["id"] == tid), None)


def _out(r: Recipe, base: str) -> dict[str, Any]:
    return {
        "id": r.id,
        "name": r.name,
        "protocol": r.protocol,
        "endpoint": base + ENDPOINTS[r.protocol],
        "model": r.model,
        "env": r.env,
        "files": r.files,
        "steps": r.steps,
        "run": r.run,
        "notes": r.notes,
        "docs_url": r.docs_url,
    }


def render(r: dict[str, Any]) -> str:
    """Human-readable, paste-ready text for one recipe."""
    lines = [f"# {r['name']}  ({r['protocol']} via {r['endpoint']})"]
    if r["env"]:
        lines.append("")
        lines += [f'export {k}="{v}"' for k, v in r["env"].items()]
    for f in r["files"]:
        lines += ["", f"# {f['path']}", f["content"].rstrip()]
    if r["steps"]:
        lines.append("")
        lines += [f"{i}. {s}" for i, s in enumerate(r["steps"], 1)]
    if r["run"]:
        lines += ["", r["run"]]
    if r["notes"]:
        lines.append("")
        lines += [f"note: {n}" for n in r["notes"]]
    if r["docs_url"]:
        lines.append(f"docs: {r['docs_url']}")
    return "\n".join(lines)
