"""Operator commands against a running Oasis Route: providers, models, routes, quota, verify, preview,
doctor, decisions and explain.

Every command reads the management API with an Oasis Key (``$OASIS_API_KEY``), prints a compact table
and accepts ``--json`` for the raw response. Nothing here changes routing or spend settings; ``verify``
only re-validates credentials (non-billable model listing).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

ADVERTISED_NOTE = (
    "Imported catalogue entries (source omniroute) are advertised data: unverified, never routed as free "
    "until the provider's own API and price list confirm them."
)


def _client(args: argparse.Namespace) -> tuple[Any, str] | None:
    from oasis_route import cli

    return cli._api(args)


def _get(c: Any, path: str, **params: Any) -> Any:
    r = c.get(path, params={k: v for k, v in params.items() if v is not None} or None)
    if r.status_code != 200:
        raise _HTTPError(path, r.status_code, r.text)
    return r.json()


class _HTTPError(Exception):
    def __init__(self, path: str, status: int, text: str) -> None:
        super().__init__(f"{path}: HTTP {status}: {text[:300]}")
        self.status = status


def _table(rows: Iterable[Iterable[Any]], header: list[str]) -> str:
    data = [[_cell(v) for v in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in data]) for i, h in enumerate(header)]
    widths = [min(w, 60) for w in widths]
    lines = ["  ".join(h.upper().ljust(widths[i]) for i, h in enumerate(header)).rstrip()]
    for r in data:
        lines.append("  ".join(v[: widths[i]].ljust(widths[i]) for i, v in enumerate(r)).rstrip())
    return "\n".join(lines)


def _cell(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def _ago(ts: str | None) -> str:
    if not ts:
        return "-"
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    s = (t - datetime.now(UTC)).total_seconds()
    return _dur(s) if s >= 0 else f"{_dur(-s)} ago"


def _dur(s: float) -> str:
    s = int(s)
    if s < 120:
        return f"{s}s"
    if s < 7200:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _run(args: argparse.Namespace, fn: Callable[[Any, str, argparse.Namespace], tuple[Any, str, int]]) -> int:
    api = _client(args)
    if api is None:
        return 2
    c, project = api
    try:
        with contextlib.closing(c):
            raw, text, rc = fn(c, project, args)
    except _HTTPError as err:
        print(str(err), file=sys.stderr)
        return 1
    print(json.dumps(raw, indent=2, default=str) if args.json else text)
    return rc


# --- providers / models ---------------------------------------------------------------------------


def _free_claim(cat: dict[str, Any]) -> str | None:
    ft = cat.get("free_tier") or {}
    if not ft or ft.get("advertised") is False:
        return None
    pools = ft.get("pools") or []
    kind = ft.get("free_type") or (pools[0].get("free_type") if pools else None)
    return str(ft.get("note") or kind or "advertised")[:50]


def _providers(c: Any, project: str, args: argparse.Namespace) -> tuple[Any, str, int]:
    providers = _get(c, "/api/v1/providers")
    conns = _get(c, f"/api/v1/projects/{project}/connections")
    connected: dict[str, int] = {}
    for x in conns:
        if x["status"] != "revoked":
            connected[x["provider_id"]] = connected.get(x["provider_id"], 0) + 1
    rows = []
    for p in providers:
        cat = p.get("catalog") or {}
        # Curated entries win over an imported catalogue row with the same id: show them as curated/reviewed.
        curated = bool(cat.get("curated_override")) or not cat.get("source")
        source = "curated" if curated else cat["source"]
        claim = _free_claim(cat)
        n = connected.get(p["id"], 0)
        if args.connected and not n:
            continue
        if args.advertised and not claim:
            continue
        if args.search and args.search.lower() not in f"{p['id']} {p['name']}".lower():
            continue
        if args.status and p["status"] != args.status:
            continue
        verification = (
            "reviewed" if curated else (cat.get("provenance") or {}).get("verification") or "advertised"
        )
        rows.append((p, source, verification, claim, n))
    out = [
        {**p, "connected_accounts": n, "verification": v, "advertised_free_tier": claim}
        for p, _s, v, claim, n in rows
    ]
    text = _table(
        [(p["id"], p["name"], p["status"], s, v, claim, n or "") for p, s, v, claim, n in rows],
        ["id", "name", "status", "source", "verification", "advertised free tier", "connected"],
    )
    text += f"\n\n{len(rows)} providers, {sum(1 for r in rows if r[4])} connected. {ADVERTISED_NOTE}"
    return out, text, 0


def _price(pr: dict[str, Any]) -> str | None:
    a, b = pr.get("prompt_usd_per_mtok"), pr.get("completion_usd_per_mtok")
    if a is None and b is None:
        return None
    tag = "" if pr.get("verified") else " (unverified)"
    return f"${_cell(a)}/${_cell(b)} per Mtok{tag}"


def _models(c: Any, project: str, args: argparse.Namespace) -> tuple[Any, str, int]:
    models = _get(c, "/api/v1/models", provider=args.provider)
    sel = []
    for m in models:
        if args.routable and m["lifecycle"] in ("catalog", "removed"):
            continue
        if args.search and args.search.lower() not in f"{m['provider_id']}/{m['upstream_id']}".lower():
            continue
        sel.append(m)
    shown = sel[: args.limit] if args.limit else sel
    rows = []
    for m in shown:
        caps = m.get("capabilities") or {}
        flags = ",".join(k for k in ("tools", "vision", "json_mode", "reasoning") if caps.get(k) is True)
        state = "unconfirmed" if m["lifecycle"] == "catalog" else m["lifecycle"]
        rows.append(
            (
                f"{m['provider_id']}/{m['upstream_id']}",
                state,
                m.get("context_window"),
                flags,
                _price(m.get("pricing") or {}),
                _free_claim(m.get("catalog") or {}),
            )
        )
    text = _table(rows, ["model", "lifecycle", "context", "capabilities", "price", "advertised free"])
    more = f" (showing {len(shown)}; --limit 0 for all)" if len(shown) < len(sel) else ""
    text += (
        f"\n\n{len(sel)} models{more}. 'unconfirmed' models come from the imported catalogue and are not "
        "routed until a connected account's model list confirms them."
    )
    return sel, text, 0


# --- routes / quota -------------------------------------------------------------------------------


def _routes(c: Any, project: str, args: argparse.Namespace) -> tuple[Any, str, int]:
    data = _get(c, f"/api/v1/projects/{project}/routes")
    rs = [
        r
        for r in data["routes"]
        if (not args.state or r["state"] == args.state) and (not args.cost or r["cost_class"] == args.cost)
    ]
    rows = [
        (
            r["route"],
            r["cost_class"],
            r.get("free_evidence") or r.get("cost_basis"),
            r["state"],
            (r.get("reason") or "") + (f" (retry in {_dur(r['retry_in_s'])})" if r.get("retry_in_s") else ""),
        )
        for r in rs
    ]
    s = data["summary"]
    text = _table(rows, ["route", "cost", "evidence", "state", "reason"])
    text += (
        f"\n\n{s['total']} routes · cost {s['by_cost_class']} · state {s['by_state']} · "
        f"{s['eligible_free']} eligible free · spend policy {s['spend_policy']}"
    )
    if s.get("unconfirmed_catalog_models"):
        text += f"\n{s['unconfirmed_catalog_models']} catalogue models await confirmation by a model list."
    return {**data, "routes": rs}, text, 0


def _cooldowns(capacity: dict[str, Any], conns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for a in capacity.get("allocations") or []:
        for d in a.get("dimensions") or []:
            for p in d.get("pools") or []:
                if p.get("exhausted_until"):
                    out.append(
                        {
                            "kind": "quota",
                            "scope": f"{a['provider_id']}/{a.get('model_scope') or '*'}",
                            "unit": d["unit"],
                            "until": p["exhausted_until"],
                            "source": p.get("source"),
                        }
                    )
    for c in conns:
        for h in c.get("health") or []:
            if h.get("open_until"):
                out.append(
                    {
                        "kind": "circuit",
                        "scope": f"{c['provider_id']}/{h.get('model_scope') or '*'}",
                        "unit": h.get("last_error_category"),
                        "until": h["open_until"],
                        "source": "health",
                    }
                )
    return out


def _quota(c: Any, project: str, args: argparse.Namespace) -> tuple[Any, str, int]:
    cap = _get(c, f"/api/v1/projects/{project}/capacity")
    conns = _get(c, f"/api/v1/projects/{project}/connections")
    budgets = _get(c, f"/api/v1/projects/{project}/budgets")
    rows = []
    for a in cap.get("allocations") or []:
        for d in a.get("dimensions") or []:
            pools = d.get("pools") or []
            reset = min((p["next_reset"] for p in pools if p.get("next_reset")), default=None)
            rows.append(
                (
                    f"{a['provider_id']}/{a.get('account_scope')}",
                    a.get("model_scope") or "*",
                    d["unit"],
                    "unknown" if d.get("limit_unknown") else d.get("headroom_now"),
                    d.get("sustained_per_day"),
                    d.get("one_time_remaining"),
                    _ago(reset),
                    "yes" if a.get("routable_now") else "no",
                )
            )
    parts = [
        _table(
            rows, ["account", "models", "unit", "remaining", "per day", "one-time", "resets in", "routable"]
        )
    ]
    for u in cap.get("unmetered_connections") or []:
        parts.append(f"  unmetered: {u['provider_id']}/{u['account_scope']} (no published limits observed)")
    for u in cap.get("unknown_limits") or []:
        parts.append(f"  unknown limit: {json.dumps(u, default=str)[:120]}")
    cds = _cooldowns(cap, conns)
    parts.append("\nActive cooldowns")
    parts.append(
        _table(
            [(x["kind"], x["scope"], x["unit"], _ago(x["until"]), x["source"]) for x in cds],
            ["kind", "scope", "unit", "ends in", "source"],
        )
        if cds
        else "  none"
    )
    fc = cap.get("forecast") or {}
    risky = [i for i in fc.get("items") or [] if i.get("exhausts_before_reset")]
    parts.append(f"\nForecast ({fc.get('confidence', 'unknown')} confidence, {fc.get('method', '-')})")
    parts.append(
        "\n".join(
            f"  {i['provider_id']} {i['unit']}: exhausts {_ago(i.get('projected_exhaustion'))} before reset"
            for i in risky
        )
        or "  no allocation is projected to run out before its reset"
    )
    parts.append(f"\nBudgets (spend policy {budgets['spend_policy']})")
    if budgets["budgets"]:
        parts.append(
            _table(
                [
                    (
                        b["period"],
                        b.get("limit_usd"),
                        b.get("used_usd"),
                        b.get("reserved_usd"),
                        b.get("remaining_usd"),
                        _ago(b.get("resets_at")),
                    )
                    for b in budgets["budgets"]
                ],
                ["period", "limit $", "used $", "reserved $", "remaining $", "resets in"],
            )
        )
    else:
        parts.append("  none set (zero-budget projects never spend; caps only bound paid modes)")
    raw = {"capacity": cap, "cooldowns": cds, "budgets": budgets}
    return raw, "\n".join(parts), 0


# --- verify -----------------------------------------------------------------------------------------


def _verify(c: Any, project: str, args: argparse.Namespace) -> tuple[Any, str, int]:
    conns = [
        x
        for x in _get(c, f"/api/v1/projects/{project}/connections")
        if x["status"] != "revoked" and (not args.connection or x["id"] == args.connection)
    ]
    results = []
    for x in conns:
        r = c.post(f"/api/v1/projects/{project}/connections/{x['id']}/validate")
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        results.append({"connection": x, "http": r.status_code, "result": body})
    fresh = {y["id"]: y for y in _get(c, f"/api/v1/projects/{project}/connections")}
    routes = _get(c, f"/api/v1/projects/{project}/routes")
    evidence: dict[str, dict[str, int]] = {}
    for rt in routes["routes"]:
        e = evidence.setdefault(rt["provider_id"], {})
        key = f"{rt['cost_class']}:{rt.get('free_evidence') or rt.get('cost_basis') or '-'}"
        e[key] = e.get(key, 0) + 1
    rows = []
    bad = 0
    for res in results:
        x, body = res["connection"], res["result"] or {}
        now = fresh.get(x["id"], x)
        ok = res["http"] == 200 and body.get("status") == "active"
        bad += 0 if ok else 1
        seen = f"ok, {body.get('models_seen')} models" if ok else body.get("category") or res["http"]
        rows.append(
            (
                x["provider_id"],
                x["display_name"],
                now["status"],
                now["billing_mode"],
                seen,
                body.get("error") or now.get("last_error"),
                ", ".join(f"{k}={v}" for k, v in sorted(evidence.get(x["provider_id"], {}).items())),
            )
        )
    text = _table(rows, ["provider", "connection", "status", "billing", "check", "error", "route evidence"])
    if not results:
        text = "No connections. Run `oasis-route setup` or connect a key first."
    text += (
        "\n\nFree routes need evidence: a local server, a verified provider price list, or an account "
        "declared free tier. Anything else is classed paid or unknown and never used under zero-budget."
    )
    return {"results": results, "route_evidence": evidence}, text, 1 if bad else 0


# --- preview / decisions / explain --------------------------------------------------------------


def _explanation_text(e: dict[str, Any]) -> list[str]:
    out = []
    sel = e.get("selected")
    if sel:
        cost = sel.get("cost") or {}
        out.append(f"Selected   {sel['route']}  ({e.get('strategy')})")
        out.append(f"Reason     {sel.get('reason')}")
        out.append(
            f"Cost       {cost.get('status')} · est ${_cell(cost.get('usd'))}"
            f" · basis {cost.get('cost_basis')} · price verified {cost.get('price_verified')}"
        )
    else:
        out.append("Selected   none: no route passed every gate")
    v = e.get("verification") or {}
    if v:
        out.append(
            f"Verified   free={v.get('free')} · evidence {v.get('free_evidence') or v.get('cost_basis')}"
        )
    q = e.get("quota") or {}
    if q:
        frac = q.get("remaining_fraction")
        rem = "unmetered" if not q.get("metered") else f"{frac:.0%} left" if frac is not None else "unknown"
        reset = f" · resets in {_dur(q['reset_in_s'])}" if q.get("reset_in_s") else ""
        out.append(f"Quota      {rem}{reset}")
    sp = e.get("spend") or {}
    if sp:
        out.append(
            f"Spend      mode {sp.get('mode')} · paid {sp.get('paid')}"
            + (f" · {sp['label']}" if sp.get("label") else "")
        )
    alts = e.get("alternatives") or []
    if alts:
        out.append("Alternatives")
        out.extend(
            f"  {a['route']}  quality {_cell(a.get('quality') or a.get('prior'))} · "
            f"{(a.get('cost') or {}).get('status')}"
            for a in alts[:5]
        )
        if len(alts) > 5:
            out.append(f"  ... {len(alts) - 5} more")
    rej = e.get("rejected") or []
    if rej:
        out.append("Rejected")
        for g in rej:
            ex = ", ".join(r["route"] for r in g.get("routes", [])[:3])
            out.append(f"  [{g.get('gate')}] {g['reason']} x{g['count']}: {ex}")
    fc = e.get("fallback_chain") or {}
    if fc.get("planned"):
        out.append(
            "Fallback   " + " -> ".join(fc["planned"][:6]) + (" ..." if len(fc["planned"]) > 6 else "")
        )
    for a in fc.get("attempts") or []:
        out.append(
            f"  attempt {a['provider']}/{a['model']}: {a['outcome']}"
            + (f" ({a['category']})" if a.get("category") else "")
            + (f", retry in {_dur(a['retry_in_s'])}" if a.get("retry_in_s") else "")
        )
    act = e.get("actual_cost") or {}
    if act:
        out.append(f"Actual     ${_cell(act.get('usd'))} ({act.get('status')}, source {act.get('source')})")
    return out


def _preview(c: Any, project: str, args: argparse.Namespace) -> tuple[Any, str, int]:
    opts: dict[str, Any] = {}
    if args.task:
        opts["task"] = args.task
    if args.mode:
        opts["mode"] = args.mode
    if args.free_only:
        opts["free_only"] = True
    if args.require:
        opts["require"] = args.require
    body: dict[str, Any] = {"model": args.model, "messages": [{"role": "user", "content": args.prompt}]}
    if args.max_tokens:
        body["max_tokens"] = args.max_tokens
    if opts:
        body["oasis_route"] = opts
    r = c.post(f"/api/v1/projects/{project}/route-preview", json=body)
    if r.status_code != 200:
        raise _HTTPError("route-preview", r.status_code, r.text)
    data = r.json()
    lines = [
        f"Preview of model={args.model} · spend mode {data.get('spend_mode')} · "
        f"{len(data.get('candidates') or [])} candidates (no provider called, no quota reserved)"
    ]
    lines += _explanation_text(data.get("explanation") or {})
    for a in data.get("approval_candidates") or []:
        lines.append(f"Paid option needing approval: {a.get('provider_id')}/{a.get('upstream_model')}")
    return data, "\n".join(lines), 0 if data.get("candidates") else 3


def _decisions(c: Any, project: str, args: argparse.Namespace) -> tuple[Any, str, int]:
    ds = _get(c, f"/api/v1/projects/{project}/route-decisions", limit=args.limit)
    rows = [
        (
            _ago(d.get("created_at")),
            d["request_id"],
            d.get("requested_model"),
            f"{d.get('served_provider')}/{d.get('served_model')}" if d.get("served_provider") else None,
            d.get("outcome"),
            "yes" if d.get("fallback_used") else "",
            len(d.get("attempts") or []),
        )
        for d in ds
    ]
    text = _table(rows, ["when", "request", "requested", "served", "outcome", "fallback", "attempts"])
    return ds, text + "\n\noasis-route explain <request> shows why a request went where it did.", 0


def _explain(c: Any, project: str, args: argparse.Namespace) -> tuple[Any, str, int]:
    d = _get(c, f"/api/v1/projects/{project}/route-decisions/{args.request_id}")
    try:
        receipt = _get(c, f"/api/v1/projects/{project}/receipts/{args.request_id}")
    except _HTTPError as err:
        if err.status != 404:
            raise
        receipt = None
    served = f"{d.get('served_provider')}/{d.get('served_model')}" if d.get("served_provider") else "nothing"
    lines = [
        f"Request    {d['request_id']}  ({_ago(d.get('created_at'))})",
        f"Asked for  {d.get('requested_model')} · task {d.get('task')} · mode {d.get('mode')}",
        f"Outcome    {d.get('outcome')} · served by {served} · fallback {d.get('fallback_used')}"
        f" · downgraded {d.get('downgraded')}",
    ]
    lines += _explanation_text(d.get("explanation") or {})
    if receipt:
        lines.append(
            f"Receipt    tokens {receipt.get('input_tokens')} in / {receipt.get('output_tokens')} out "
            f"({receipt.get('usage_source')}) · estimated ${_cell(receipt.get('estimated_usd'))} · reserved "
            f"${_cell(receipt.get('reserved_usd'))} · actual ${_cell(receipt.get('actual_usd'))} · drift "
            f"${_cell(receipt.get('drift_usd'))}"
        )
        lines.append(
            f"           digest {str(receipt.get('digest'))[:16]}… valid={receipt.get('digest_valid')}"
            f" · spend mode {receipt.get('spend_mode')}"
        )
    else:
        lines.append("Receipt    none (the request was refused before any reservation)")
    return {"decision": d, "receipt": receipt}, "\n".join(lines), 0


# --- doctor -----------------------------------------------------------------------------------------


def _doctor(c: Any, project: str, args: argparse.Namespace) -> tuple[Any, str, int]:
    checks: list[dict[str, Any]] = []

    def add(name: str, level: str, detail: str) -> None:
        checks.append({"check": name, "level": level, "detail": detail})

    ready = c.get("/readyz")
    rj = ready.json() if ready.headers.get("content-type", "").startswith("application/json") else {}
    if ready.status_code == 200:
        add("server", "ok", f"ready, schema {rj.get('schema_revision')}")
        worker = rj.get("worker")
        add(
            "worker",
            "ok" if worker == "running" else "warn",
            f"worker {worker}"
            + ("" if worker == "running" else ": quota refresh and health probes are idle"),
        )
    else:
        add("server", "fail", f"/readyz HTTP {ready.status_code}: {ready.text[:120]}")
    me = _get(c, "/api/v1/me")
    scopes = me["key"].get("scopes") or []
    add(
        "oasis key",
        "ok" if "inference" in scopes else "warn",
        f"{me['key']['prefix']}… scopes {','.join(scopes)}"
        + ("" if "inference" in scopes else " (cannot call /v1)"),
    )
    if me["key"].get("expires_at"):
        add(
            "key expiry",
            "warn" if _ago(me["key"]["expires_at"]).endswith("ago") else "ok",
            f"expires {_ago(me['key']['expires_at'])}",
        )
    st = _get(c, f"/api/v1/projects/{project}/status")
    add(
        "project",
        "ok" if st["status"] == "Ready" else "warn" if st["status"] != "Not connected" else "fail",
        st.get("summary") or st["status"],
    )
    for a in st.get("attention") or []:
        add("attention", "warn", a if isinstance(a, str) else json.dumps(a, default=str)[:160])
    conns = _get(c, f"/api/v1/projects/{project}/connections")
    live = [x for x in conns if x["status"] != "revoked"]
    if not live:
        add("connections", "fail", "no provider connected; run `oasis-route setup`")
    for x in live:
        name = f"{x['provider_id']} ({x['display_name']})"
        if x["status"] != "active":
            add("credential", "fail", f"{name}: {x['status']} {x.get('last_error') or ''}".strip())
        elif not x.get("has_credential") and x["auth_method"] != "none":
            add("credential", "warn", f"{name}: no stored credential")
        else:
            add("credential", "ok", f"{name}: active, validated {_ago(x.get('last_validated_at'))}")
        for h in x.get("health") or []:
            if h.get("open_until"):
                add("circuit", "warn", f"{name} {h['model_scope']}: open, retry in {_ago(h['open_until'])}")
    routes = _get(c, f"/api/v1/projects/{project}/routes")["summary"]
    add(
        "free routes",
        "ok" if routes["eligible_free"] else "fail",
        f"{routes['eligible_free']} eligible free of {routes['total']} routes"
        + ("" if routes["eligible_free"] else "; run `oasis-route setup` for free keys or local models"),
    )
    if routes.get("eligible_demo") and not routes["eligible_free"]:
        add(
            "demo only",
            "warn",
            "only demo providers are connected: `auto` answers with canned echoes, not a real model",
        )
    add("spend policy", "ok", f"{routes['spend_policy']} (paid routes are never used silently)")
    budgets = _get(c, f"/api/v1/projects/{project}/budgets")["budgets"]
    for b in budgets:
        rem, lim = b.get("remaining_usd"), b.get("limit_usd")
        low = rem is not None and lim and rem < 0.1 * lim
        add("budget", "warn" if low else "ok", f"{b['period']}: ${_cell(rem)} of ${_cell(lim)} left")
    sym = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}
    text = "\n".join(f"  {sym[x['level']]}  {x['check']:<13} {x['detail']}" for x in checks)
    fails = sum(1 for x in checks if x["level"] == "fail")
    warns = sum(1 for x in checks if x["level"] == "warn")
    text += f"\n\n{fails} failing, {warns} warnings."
    return {"checks": checks}, text, 1 if fails else 0


# --- registration -----------------------------------------------------------------------------------


def register(sub: Any, add_api_args: Callable[[argparse.ArgumentParser], None]) -> None:
    def cmd(name: str, help_: str, fn: Callable[[Any, str, argparse.Namespace], tuple[Any, str, int]]) -> Any:
        p = sub.add_parser(name, help=help_)
        add_api_args(p)
        p.set_defaults(func=lambda a: _run(a, fn))
        return p

    p = cmd(
        "providers",
        "providers in the registry and catalogue, with connection and verification state",
        _providers,
    )
    p.add_argument("--connected", action="store_true", help="only providers you have connected")
    p.add_argument("--advertised", action="store_true", help="only providers advertising a free tier")
    p.add_argument("--status", choices=["active", "blocked", "unsupported"])
    p.add_argument("--search")

    p = cmd(
        "models", "model catalogue: lifecycle, context, capabilities, price and its verification", _models
    )
    p.add_argument("--provider")
    p.add_argument("--routable", action="store_true", help="hide unconfirmed catalogue models")
    p.add_argument("--search")
    p.add_argument("--limit", type=int, default=50)

    p = cmd(
        "routes", "every connection x model route: free/paid/unknown and eligible/blocked/exhausted", _routes
    )
    p.add_argument("--state", choices=["eligible", "blocked", "exhausted"])
    p.add_argument("--cost", choices=["free", "paid", "unknown"])

    cmd("quota", "remaining quota, reset times, active cooldowns, forecast and budgets", _quota)

    p = cmd(
        "verify", "re-validate connected credentials and show the evidence behind each free route", _verify
    )
    p.add_argument("--connection", help="one connection id")

    p = cmd("preview", "which route a request would take now, and why (calls no provider)", _preview)
    p.add_argument("prompt", nargs="?", default="ping")
    p.add_argument("--model", default="auto")
    p.add_argument("--task")
    p.add_argument("--mode")
    p.add_argument("--free-only", action="store_true")
    p.add_argument("--require", nargs="*", help="capabilities such as tools vision")
    p.add_argument("--max-tokens", type=int)

    p = cmd("decisions", "recent route decisions", _decisions)
    p.add_argument("--limit", type=int, default=20)

    p = cmd(
        "explain",
        "explain a past request: route, alternatives, rejections, fallback chain, receipt",
        _explain,
    )
    p.add_argument("request_id")

    cmd("doctor", "check server, key, credentials, circuits, free routes and budgets", _doctor)
