#!/usr/bin/env python3
"""Normalise an OmniRoute catalogue export into OASIS Route's catalogue data file.

Usage::

    # 1. in an OmniRoute checkout (MIT licence):
    npx -y tsx --tsconfig tsconfig.json /path/to/scripts/omniroute/export_catalog.mts > omniroute_raw.json
    # 2. here:
    python scripts/import_omniroute_catalog.py omniroute_raw.json --commit <omniroute-sha>

Writes ``src/oasis_route/registry/data/omniroute_catalog.json``. See docs/CATALOG.md.

Every imported fact is **advertised**, never verified: free-tier claims, prices, context windows and
capabilities come from a third-party research snapshot. Routing treats them accordingly (a model is free only
when the provider itself says so, models stay non-routable until the provider's own model list confirms them).
Entries that automate consumer web sessions, impersonate another product's CLI/IDE OAuth client, resell or
reverse-engineer other providers, or whose terms prohibit proxy use are imported as ``blocked`` with a reason
so users can see why they are not offered.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "src" / "oasis_route" / "registry" / "data" / "omniroute_catalog.json"
REPO = "https://github.com/alif2815/OmniRouter"
UPSTREAM = "https://github.com/diegosouzapw/OmniRoute"

# OmniRoute id -> OASIS Route provider id, where OASIS already seeds the provider by hand. Curated OASIS seeds
# keep their reviewed provider facts; the import only contributes model metadata and advertised free tiers.
ID_MAP = {
    "openai": "openai",
    "anthropic": "anthropic",
    "gemini": "gemini",
    "groq": "groq",
    "mistral": "mistral",
    "openrouter": "openrouter",
    "cerebras": "cerebras",
    "nvidia": "nvidia",
    "sambanova": "sambanova",
    "huggingface": "huggingface",
    "cohere": "cohere",
    "zai": "zai",
}

WEB_MARKERS = ("-web", "web-")
OAUTH_REASON = (
    "uses another product's CLI/IDE or subscription OAuth client; OASIS Route connects only official API "
    "credentials the account owner issued for API use"
)
PLAN_MARKERS = ("coding", "token-plan", "codingplan")
# Image/video/music generation services: no chat-completions API to route to.
MEDIA_ONLY = {"ideogram", "leonardo", "haiper", "suno", "udio", "freepik", "stability-ai", "segmind", "runway"}
TOS_RANK = {"ok": 0, "unknown": 1, "caution": 2, "ambiguous": 3, "avoid": 4}
FREE_TYPES_RECURRING = {"recurring-daily", "recurring-monthly", "recurring-credit"}


def _slug(omni_id: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", omni_id.lower()).strip("_")
    return ID_MAP.get(omni_id, s)[:64]


def _base_url(url: str, protocol: str) -> str:
    url = url.split("?", 1)[0].rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/messages"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    if protocol == "anthropic_messages" and url.endswith("/v1"):
        url = url[: -len("/v1")]
    return url


def _auth(entry: dict[str, Any], protocol: str) -> dict[str, Any]:
    header = str(entry.get("authHeader") or "bearer")
    auth_type = str(entry.get("authType") or "apikey")
    style = "bearer"
    if header.lower() == "x-api-key":
        style = "x-api-key"
    elif header.lower() not in ("bearer", "authorization"):
        style = f"header:{header}"
    if protocol == "anthropic_messages" and header.lower() == "bearer":
        style = "bearer"
    return {
        "style": style,
        "prefix": entry.get("authPrefix"),
        "required": auth_type == "apikey",
        "optional": auth_type == "optional",
    }


def _tos(omni_id: str, raw: dict[str, Any], free_rows: list[dict[str, Any]]) -> str:
    verdicts = [str(r.get("tos") or "unknown") for r in free_rows]
    if omni_id in raw["tos"]:
        verdicts.append(str(raw["tos"][omni_id]))
    if not verdicts:
        return "unknown"
    return max(verdicts, key=lambda v: TOS_RANK.get(v, 1))


def classify(omni_id: str, entry: dict[str, Any], tos: str) -> tuple[str, str | None, str | None]:
    """Return ``(status, protocol, reason)``. Status is ``active``, ``blocked`` or ``unsupported``."""
    fmt, executor, auth_type = entry.get("format"), entry.get("executor"), entry.get("authType")
    base = str(entry.get("baseUrl") or "")
    header = str(entry.get("authHeader") or "")
    if any(m in omni_id for m in WEB_MARKERS) or auth_type == "cookie" or header == "cookie":
        return "blocked", None, "automates a consumer web session (cookies/browser), not a documented API"
    if omni_id.startswith("g4f-"):
        return "blocked", None, "third-party reverse-engineered proxy of other providers' services"
    if auth_type == "oauth":
        return "blocked", None, OAUTH_REASON
    if tos == "avoid":
        return "blocked", None, "OmniRoute's terms review marks this provider 'avoid' (proxy or non-personal use)"
    if any(m in omni_id for m in PLAN_MARKERS):
        return (
            "blocked",
            None,
            "subscription/coding-plan quota restricted to the provider's officially supported tools",
        )
    if omni_id in MEDIA_ONLY:
        return "unsupported", None, "media generation service without a chat-completions API"
    if fmt == "openai":
        protocol = "openai_chat"
    elif fmt == "claude":
        protocol = "anthropic_messages"
    else:
        return "unsupported", None, f"protocol '{fmt}' has no OASIS Route adapter yet"
    if executor not in ("default", "openai-compatible"):
        return "unsupported", protocol, f"needs a dedicated adapter (OmniRoute executor '{executor}')"
    if auth_type == "none":
        return "blocked", protocol, "anonymous public endpoint; requires operator review of automated access"
    if not base.startswith("https://") or "{" in base:
        return "unsupported", protocol, "endpoint needs per-account configuration"
    if entry.get("urlBuilder"):
        return "unsupported", protocol, "endpoint URL is built per request"
    return "active", protocol, None


def _caps(m: dict[str, Any]) -> dict[str, Any]:
    caps: dict[str, Any] = {"streaming": True, "source": "omniroute_catalog"}
    if "toolCalling" in m:
        caps["tools"] = bool(m["toolCalling"])
    if "supportsVision" in m:
        caps["vision"] = bool(m["supportsVision"])
    if "supportsReasoning" in m:
        caps["reasoning"] = bool(m["supportsReasoning"])
    mid = str(m.get("id", "")).lower()
    if re.search(r"(embed|tts|whisper|transcri|image|dall-e|flux|stable-diffusion|rerank|moderation)", mid):
        caps["chat"] = False
    return caps


def _price(pricing: dict[str, Any], keys: list[str], model_id: str) -> dict[str, Any] | None:
    for k in keys:
        p = (pricing.get(k) or {}).get(model_id)
        if isinstance(p, dict) and isinstance(p.get("input"), int | float):
            return {
                "input_usd_per_mtok": float(p["input"]),
                "output_usd_per_mtok": float(p.get("output") or p["input"]),
            }
    return None


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def build(raw: dict[str, Any], commit: str) -> dict[str, Any]:
    curated = str(raw.get("free_catalog_curated_at") or "")
    free_by_provider: dict[str, list[dict[str, Any]]] = {}
    for r in raw["free_models"]:
        free_by_provider.setdefault(str(r["provider"]), []).append(r)
    providers: list[dict[str, Any]] = []
    for omni_id in sorted(raw["registry"]):
        entry = raw["registry"][omni_id]
        disp = raw["display"].get(omni_id) or {}
        free_rows = free_by_provider.get(omni_id, [])
        tos = _tos(omni_id, raw, free_rows)
        status, protocol, reason = classify(omni_id, entry, tos)
        base = _base_url(str(entry.get("baseUrl") or ""), protocol or "openai_chat")
        free_index = {str(r["modelId"]): r for r in free_rows}
        pricing_keys = [omni_id, str(entry.get("alias") or "")]
        default_ctx = entry.get("defaultContextLength")
        models = []
        seen: set[str] = set()
        for m in entry.get("models") or []:
            mid = str(m.get("id") or "").strip()
            if not mid or mid in seen or len(mid) > 200:
                continue
            seen.add(mid)
            row: dict[str, Any] = {
                "upstream_id": mid,
                "display_name": str(m.get("name") or mid)[:200],
                "context_window": m.get("contextLength") or m.get("maxInputTokens") or default_ctx,
                "max_output_tokens": m.get("maxOutputTokens"),
                "capabilities": _caps(m),
            }
            if m.get("aliases"):
                row["aliases"] = [str(a) for a in m["aliases"]][:10]
            fr = free_index.pop(mid, None)
            if fr is not None:
                row["free_tier"] = _free(fr)
            price = _price(raw["pricing"], pricing_keys, mid)
            if price is not None:
                row["price"] = price
            models.append(row)
        # Free-catalogue models the registry does not list are still advertised by the research snapshot.
        for mid, fr in sorted(free_index.items()):
            if len(mid) > 200:
                continue
            models.append(
                {
                    "upstream_id": mid,
                    "display_name": str(fr.get("displayName") or mid)[:200],
                    "context_window": None,
                    "max_output_tokens": None,
                    "capabilities": {"streaming": True, "source": "omniroute_catalog"},
                    "free_tier": _free(fr),
                }
            )
        website = disp.get("website")
        domains = sorted({h for h in (_host(base), _host(str(website or ""))) if h})
        advertised_free = bool(disp.get("hasFree")) or any(
            r.get("freeType") != "discontinued" for r in free_rows
        )
        providers.append(
            {
                "id": _slug(omni_id),
                "omniroute_id": omni_id,
                "name": str(disp.get("name") or omni_id)[:200],
                "status": status,
                "status_reason": reason,
                "protocol": protocol,
                "base_url": base if protocol else None,
                "models_url": entry.get("modelsUrl"),
                "auth": _auth(entry, protocol or "openai_chat"),
                "extra_headers": {
                    k: v
                    for k, v in {**(entry.get("headers") or {}), **(entry.get("extraHeaders") or {})}.items()
                    if isinstance(v, str) and k.lower() not in ("authorization", "cookie", "x-api-key")
                },
                "website": website,
                "official_domains": domains,
                "free_tier": {
                    "advertised": advertised_free,
                    "note": disp.get("freeNote"),
                    "tos_verdict": tos,
                    "pools": _pools(free_rows),
                },
                "auth_hint": disp.get("authHint") or disp.get("apiHint"),
                "models": models,
                "provenance": {
                    "source": "omniroute",
                    "source_url": f"{REPO}/tree/{commit}/open-sse/config/providers/registry",
                    "free_catalog_url": f"{REPO}/blob/{commit}/open-sse/config/freeModelCatalog.data.ts",
                    "curated_at": curated,
                    "review_state": "unreviewed",
                },
            }
        )
    local = [
        {
            "id": _slug(k),
            "omniroute_id": k,
            "name": str(v.get("name") or k),
            "default_base_url": v.get("localDefault"),
            "website": v.get("website"),
        }
        for k, v in sorted(raw["local"].items())
        if v.get("localDefault")
    ]
    return {
        "schema": 1,
        "source": {
            "repository": REPO,
            "upstream": UPSTREAM,
            "commit": commit,
            "license": "MIT",
            "free_catalog_curated_at": curated,
            "imported_at": datetime.now(UTC).date().isoformat(),
            "verification": "advertised",
        },
        "counts": {
            "providers": len(providers),
            "active": sum(p["status"] == "active" for p in providers),
            "blocked": sum(p["status"] == "blocked" for p in providers),
            "unsupported": sum(p["status"] == "unsupported" for p in providers),
            "models": sum(len(p["models"]) for p in providers),
        },
        "providers": providers,
        "local_servers": local,
    }


def _free(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "free_type": r.get("freeType"),
        "monthly_tokens": int(r.get("monthlyTokens") or 0) or None,
        "credit_tokens": int(r.get("creditTokens") or 0) or None,
        "pool_key": r.get("poolKey"),
        "tos_verdict": r.get("tos"),
        "recurring": r.get("freeType") in FREE_TYPES_RECURRING,
    }


def _pools(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per shared free pool (OmniRoute ``poolKey``); never summed per model."""
    pools: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = str(r.get("poolKey") or r.get("provider"))
        cur = pools.setdefault(
            key,
            {
                "pool_key": key,
                "free_type": r.get("freeType"),
                "monthly_tokens": 0,
                "credit_tokens": 0,
                "models": 0,
            },
        )
        cur["monthly_tokens"] = max(cur["monthly_tokens"], int(r.get("monthlyTokens") or 0))
        cur["credit_tokens"] = max(cur["credit_tokens"], int(r.get("creditTokens") or 0))
        cur["models"] += 1
    return sorted(pools.values(), key=lambda p: p["pool_key"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("raw", type=Path, help="output of scripts/omniroute/export_catalog.mts")
    ap.add_argument("--commit", required=True, help="OmniRoute commit the export was taken from")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    data = build(json.loads(args.raw.read_text()), args.commit)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    print(json.dumps(data["counts"]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
