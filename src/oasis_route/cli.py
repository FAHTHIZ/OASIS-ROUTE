"""Command-line entry point: ``oasis-route``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from oasis_route.config import Settings, get_settings


def _cmd_migrate(args: argparse.Namespace) -> int:
    from oasis_route.db import migrate

    url = get_settings().database_url
    if args.direction == "up":
        migrate.upgrade(url, args.revision or "head")
    else:
        migrate.downgrade(url, args.revision or "-1")
    print(f"migrations {args.direction} complete")
    return 0


def _context(s: Settings) -> Any:
    from oasis_route.context import AppContext
    from oasis_route.db.session import Database

    return AppContext(settings=s, db=Database(s.database_url))


def _cmd_serve(args: argparse.Namespace) -> int:
    """Run the API. By default the process also supervises the background worker (zero-ops)."""
    import uvicorn

    from oasis_route.app import create_app

    if args.no_worker:
        os.environ["OASIS_ROUTE_WORKER_MODE"] = "external"
        get_settings.cache_clear()
    s = get_settings()
    if args.migrate:
        from oasis_route.db import migrate

        migrate.upgrade(s.database_url)
    uvicorn.run(
        create_app(s),
        host=args.host or s.host,
        port=args.port or s.port,
        log_config=None,
        proxy_headers=True,
    )
    return 0


def _cmd_worker(args: argparse.Namespace) -> int:
    import json

    from oasis_route.worker.runner import run_forever, run_once

    s = get_settings()

    async def _run() -> None:
        from oasis_route.registry.catalog import sync_catalog

        ctx = _context(s)
        try:
            await sync_catalog(ctx.db, s)
            if args.once:
                print(json.dumps(await run_once(ctx), default=str, indent=2))
            else:
                import signal

                stop = asyncio.Event()
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGTERM, signal.SIGINT):
                    with contextlib.suppress(NotImplementedError):
                        loop.add_signal_handler(sig, stop.set)
                await run_forever(ctx, stop, mode="standalone")
        finally:
            await ctx.aclose()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())
    return 0


def _cmd_rotate_kek(_args: argparse.Namespace) -> int:
    """Re-wrap all credentials under the current master key (run after moving the old key to previous)."""
    import json

    from oasis_route.worker.runner import rewrap_credentials

    s = get_settings()

    async def _run() -> dict[str, int]:
        ctx = _context(s)
        try:
            total = {"rewrapped": 0, "failed": 0, "remaining": 0}
            while True:
                r = await rewrap_credentials(ctx)
                total["rewrapped"] += r["rewrapped"]
                total["failed"] = r["failed"]
                total["remaining"] = r["remaining"]
                if r["rewrapped"] == 0:
                    return total
        finally:
            await ctx.aclose()

    out = asyncio.run(_run())
    print(json.dumps(out))
    return 0 if out["remaining"] == 0 else 2


def _cmd_mock_provider(args: argparse.Namespace) -> int:
    import uvicorn

    from oasis_route.devtools.mock_provider import create_mock_provider

    uvicorn.run(create_mock_provider(), host=args.host, port=args.port, log_level="warning")
    return 0


async def _bootstrap_admin(ctx: Any, email: str, project_name: str) -> tuple[str, str] | None:
    """Create the first administrator directly in the database. Returns (project_id, oasis_key)."""
    from oasis_route.domain.enums import Role
    from oasis_route.identity import audit
    from oasis_route.identity import service as ids

    async with ctx.db.transaction() as s:
        if await ids.user_count(s) > 0:
            return None
        user = await ids.create_user(s, email, role=Role.ADMIN)
        project = await ids.create_project(s, user, project_name)
        _, secret = await ids.issue_api_key(
            s,
            project=project,
            user=user,
            pepper=ctx.settings.pepper_bytes(),
            name="bootstrap-admin",
            scopes=ids.ALL_SCOPES,
        )
        await audit.record(s, "instance.bootstrapped", actor_user_id=user.id, project_id=project.id)
        return project.id, secret


def _cmd_bootstrap(args: argparse.Namespace) -> int:
    s = get_settings()

    async def _run() -> tuple[str, str] | None:
        ctx = _context(s)
        try:
            return await _bootstrap_admin(ctx, args.email, args.project)
        finally:
            await ctx.aclose()

    out = asyncio.run(_run())
    if out is None:
        print("instance already bootstrapped; nothing changed", file=sys.stderr)
        return 1
    print(f"project_id: {out[0]}")
    print(f"OASIS_API_KEY={out[1]}")
    print("This Oasis Key is shown once. Store it in a secret manager.", file=sys.stderr)
    return 0


def _connect_text(args: argparse.Namespace) -> str:
    """Collect candidate keys: provider variables from the environment and/or a .env file."""
    from oasis_route.connections.key_detect import ENV_NAMES

    lines: list[str] = []
    if args.from_env:
        lines += [f"{n}={os.environ[n]}" for n in ENV_NAMES if os.environ.get(n)]
    for path in args.env_file or []:
        lines.append(Path(path).read_text(encoding="utf-8"))
    if args.stdin:
        lines.append(sys.stdin.read())
    return "\n".join(lines)


def _read_pasted_keys() -> str:
    """Read keys pasted into a terminal without echoing them; an empty line ends the input."""
    import getpass

    print(
        "Paste your provider keys (one per line, KEY=value or the bare key; a whole .env works too).\n"
        "Nothing is shown while you paste. Press Enter on an empty line when done.",
        file=sys.stderr,
    )
    lines: list[str] = []
    while True:
        try:
            line = getpass.getpass("")
        except EOFError:
            break
        if not line.strip():
            break
        lines.append(line)
    return "\n".join(lines)


def _ask(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _cmd_connect(args: argparse.Namespace) -> int:
    """Send keys to a running Oasis Route over its API (one-step onboarding).

    In a terminal with no flags it asks for the keys and the two confirmations; scripts pass
    ``--from-env`` / ``--env-file`` / ``--stdin`` with ``--accept-terms`` and ``--free-tier``.
    """
    interactive = sys.stdin.isatty() and not args.stdin
    text = _connect_text(args)
    if not text.strip():
        if not interactive:
            print(
                "no keys found: use --from-env, --env-file PATH or --stdin, or run `oasis-route connect` "
                "in a terminal to paste them",
                file=sys.stderr,
            )
            return 2
        text = _read_pasted_keys()
        if not text.strip():
            print("no keys pasted; nothing changed", file=sys.stderr)
            return 2
    api = _api(args)
    if api is None:
        return 2
    c, project = api
    path = f"/api/v1/projects/{project}/connections/quick"
    with contextlib.closing(c):
        accept, free_tier = args.accept_terms, args.free_tier
        if not accept:
            r = c.post(path, json={"text": text, "accept_terms": False, "dry_run": True})
            if r.status_code != 200:
                print(f"HTTP {r.status_code}: {r.text[:500]}", file=sys.stderr)
                return 1
            found = r.json()["results"]
            for res in found:
                print(f"{res['key_hint']:>7}  {res.get('provider_id') or '?':<12} {res.get('detected_by')}")
            if not found:
                print("No provider keys recognised in that text.", file=sys.stderr)
                return 2
            if not interactive:
                print(
                    "\nNothing was stored. Re-run with --accept-terms to confirm these accounts are yours "
                    "and connect them.",
                    file=sys.stderr,
                )
                return 0
            accept = _ask(
                f"\nConnect {len(found)} key(s)? They must be your own accounts, and you accept each "
                "provider's terms."
            )
            if not accept:
                print("Nothing was stored.", file=sys.stderr)
                return 0
        if free_tier is None:
            free_tier = (
                not _ask("Is billing (a card or paid credits) set up on any of these accounts?")
                if interactive
                else False
            )
        r = c.post(
            path,
            json={"text": text, "accept_terms": True, "free_tier": bool(free_tier), "dry_run": False},
        )
    if r.status_code != 200:
        print(f"HTTP {r.status_code}: {r.text[:500]}", file=sys.stderr)
        return 1
    out = r.json()
    for res in out["results"]:
        provider = res.get("provider_id") or "?"
        outcome = res.get("outcome") or res.get("detected_by")
        extra = f" · {res['free_models']} free models" if res.get("free_models") else ""
        note = f" · {res['message']}" if res.get("message") else ""
        print(f"{res['key_hint']:>7}  {provider:<12} {outcome}{extra}{note}")
    print(
        f"\n{out['connected']} connected, {out['already_connected']} already connected, "
        f"{out['needs_attention']} need attention.",
        file=sys.stderr,
    )
    if not free_tier and out["connected"]:
        print(
            "Billing was not declared absent, so only models the provider itself prices at zero are used. "
            "For a free-plan account: oasis-route billing <provider> free_tier",
            file=sys.stderr,
        )
    return 0 if out["needs_attention"] == 0 else 3


def _cmd_billing(args: argparse.Namespace) -> int:
    """Declare whether billing is set up on a provider account (decides which of its routes are free)."""
    api = _api(args)
    if api is None:
        return 2
    c, project = api
    with contextlib.closing(c):
        conns = c.get(f"/api/v1/projects/{project}/connections")
        if conns.status_code != 200:
            print(f"HTTP {conns.status_code}: {conns.text[:300]}", file=sys.stderr)
            return 1
        targets = [x for x in conns.json() if x["provider_id"] == args.provider and x["status"] != "revoked"]
        if not targets:
            print(f"no connection for provider '{args.provider}' in this project", file=sys.stderr)
            return 2
        for x in targets:
            r = c.patch(f"/api/v1/projects/{project}/connections/{x['id']}", json={"billing_mode": args.mode})
            if r.status_code != 200:
                print(f"HTTP {r.status_code}: {r.text[:300]}", file=sys.stderr)
                return 1
            print(f"{x['display_name']}: billing_mode={args.mode}")
    if args.mode == "paid":
        print("Paid accounts are only used if the project's spend mode allows it.", file=sys.stderr)
    return 0


def _clean_key(raw: str) -> str:
    """Accept a key pasted with whitespace or the whole two-line ``admin.key`` file (project id, key)."""
    parts = raw.split()
    keys = [p for p in parts if p.startswith("oasis_")]
    return keys[-1] if keys else (parts[-1] if parts else "")


def _dev_key() -> str:
    """The key `oasis-route dev` saved locally (line 2 of admin.key), if any."""
    path = Path(os.environ.get("OASIS_ROUTE_DEV_STATE_DIR", ".oasis-dev")) / "admin.key"
    try:
        return _clean_key(path.read_text(encoding="utf-8"))
    except OSError:
        return ""


def _api(args: argparse.Namespace) -> tuple[Any, str] | None:
    """An HTTP client for a running Oasis Route plus the project id (from --project or the Oasis Key)."""
    import httpx

    raw = getattr(args, "oasis_key", None) or os.environ.get("OASIS_API_KEY", "")
    oasis_key = _clean_key(raw) if raw else _dev_key()
    if not oasis_key:
        print(
            "No Oasis Key: set OASIS_API_KEY or pass --oasis-key (local dev: `oasis-route dev` saves one in "
            ".oasis-dev/admin.key, which is used automatically from that directory)",
            file=sys.stderr,
        )
        return None
    c = httpx.Client(
        base_url=args.url.rstrip("/"), headers={"authorization": f"Bearer {oasis_key}"}, timeout=120
    )
    try:
        me = c.get("/api/v1/me")
    except httpx.TransportError as exc:
        print(
            f"Cannot reach Oasis Route at {args.url}: {exc}. Is the server running? (--url)", file=sys.stderr
        )
        c.close()
        return None
    if me.status_code != 200:
        hint = " (revoked, expired or for another server?)" if me.status_code == 401 else ""
        print(f"Oasis Key rejected: HTTP {me.status_code}{hint}", file=sys.stderr)
        c.close()
        return None
    return c, getattr(args, "project", None) or me.json()["project"]["id"]


def _add_api_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--url", default=os.environ.get("OASIS_ROUTE_URL", "http://127.0.0.1:8080"))
    p.add_argument("--oasis-key", help="defaults to $OASIS_API_KEY")
    p.add_argument("--project", help="defaults to the Oasis Key's project")
    p.add_argument("--json", action="store_true", help="print the raw JSON response")


def _counts(d: dict[str, int]) -> str:
    return ", ".join(f"{v:,} {k}" for k, v in sorted(d.items(), key=lambda kv: -kv[1])) or "none"


def _print_setup(out: dict[str, Any]) -> None:
    local = out["local_servers"]
    print("Local models")
    if not local["enabled"]:
        print(f"  detection off: {local['reason']}")
    for srv in local["servers"]:
        if srv["reachable"]:
            state = "connected" if srv.get("connected") else "found"
            print(f"  {srv['provider_id']:<20} {state:<10} {srv['base_url']}  {len(srv['models'])} models")
    if local["enabled"] and not any(x["reachable"] for x in local["servers"]):
        print("  none running (optional: install Ollama from https://ollama.com and `ollama pull qwen2.5`)")
    for c in out.get("connected_local") or []:
        print(f"  connected {c['provider_id']}: {c['validation'].get('status')}")

    keys = out.get("free_keys") or []
    missing = [k for k in keys if not k["connected"]]
    have = [k for k in keys if k["connected"]]
    print("\nFree hosted providers (create a key on the provider's site, no card needed)")
    for k in have:
        print(f"  {k['provider_id']:<12} connected")
    for k in missing:
        print(f"  {k['provider_id']:<12} {k['get_key_url']:<46} {k['env_var'] or ''}")
    if missing:
        print("  then run: oasis-route connect   (paste the keys when asked)")
    cp = out["connectable_providers"]
    if cp["verified"]:
        print("  reviewed free offers: " + ", ".join(e["provider_id"] for e in cp["verified"]))
    if cp["advertised"]:
        print(
            f"  {len(cp['advertised'])} more advertised free tiers, unreviewed: "
            "oasis-route providers --advertised"
        )

    for a in out.get("billing_attention") or []:
        print(
            f"\n  ! {a['provider_id']}: {a['blocked_routes']} routes held back: its billing is unknown."
            f"\n    If no billing is set up on that account: oasis-route billing {a['provider_id']} free_tier"
        )

    rs = out["routes"]["summary"]
    print("\nRoutes")
    print(f"  {rs['total']:,} total: {_counts(rs['by_cost_class'])}; {_counts(rs['by_state'])}")
    n = rs["eligible_free"]
    print(
        f"  {n:,} free route{'' if n == 1 else 's'} ready on real models · spend policy {rs['spend_policy']}"
    )
    aside = sum(1 for r in out["routes"]["routes"] if r.get("reason") == "demo_provider_superseded")
    if aside:
        print(
            f"  {aside} demo routes set aside: a real model is connected, so `auto` no longer uses the demo"
        )
    if rs.get("eligible_demo") and not rs["eligible_free"]:
        print(
            f"  only demo providers are connected ({rs['eligible_demo']} routes): `auto` answers with echoes "
            "until you connect a real provider"
        )
    ep = out["endpoint"]
    print("\nOne OpenAI-compatible endpoint")
    print(f"  OPENAI_BASE_URL={ep['base_url']}  OPENAI_API_KEY=$OASIS_API_KEY  model=auto")
    print("  coding tools: oasis-route integrate")


def _cmd_setup(args: argparse.Namespace) -> int:
    """Turnkey onboarding against a running server: detect local servers, connect them, classify routes."""
    api = _api(args)
    if api is None:
        return 2
    c, project = api
    with contextlib.closing(c):
        if args.dry_run:
            r = c.get(f"/api/v1/projects/{project}/setup")
        else:
            r = c.post(f"/api/v1/projects/{project}/setup", json={"connect_local": not args.no_local})
    if r.status_code != 200:
        print(f"HTTP {r.status_code}: {r.text[:500]}", file=sys.stderr)
        return 1
    out = r.json()
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        _print_setup(out)
    return 0 if out["routes"]["summary"]["eligible_free"] > 0 else 3


def _check_integration(args: argparse.Namespace, r: dict[str, Any]) -> int:
    """Smoke-test the endpoint a tool will use: model listing, one chat turn with route metadata."""
    api = _api(args)
    if api is None:
        return 2
    c, _project = api
    model = r["model"]
    for prefix in ("openai/", "oasis/"):
        model = model.removeprefix(prefix)
    with contextlib.closing(c):
        m = c.get("/v1/models")
        n = len(m.json().get("data", [])) if m.is_success else 0
        print(f"  GET /v1/models: HTTP {m.status_code}, {n} models")
        msgs = [{"role": "user", "content": "Reply with OK."}]
        if r["protocol"] == "anthropic_messages":
            resp = c.post("/v1/messages", json={"model": model, "max_tokens": 16, "messages": msgs})
        elif r["protocol"] == "openai_responses":
            resp = c.post(
                "/v1/responses", json={"model": model, "max_output_tokens": 16, "input": "Reply with OK."}
            )
        else:
            resp = c.post("/v1/chat/completions", json={"model": model, "max_tokens": 16, "messages": msgs})
    served = f"{resp.headers.get('x-oasis-route-provider')}/{resp.headers.get('x-oasis-route-model')}"
    paid = resp.headers.get("x-oasis-route-paid")
    print(f"  {r['endpoint']}: HTTP {resp.status_code}, served by {served}, paid={paid}")
    if not resp.is_success:
        print(f"  {resp.text[:400]}", file=sys.stderr)
        return 1
    return 0


def _cmd_integrate(args: argparse.Namespace) -> int:
    """Print paste-ready settings for a coding tool or SDK (no secrets; uses $OASIS_API_KEY)."""
    from oasis_route import integrations

    base = args.url.rstrip("/")
    if not args.tool:
        rows = integrations.recipes(base)
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        for r in rows:
            print(f"  {r['id']:<14} {r['name']:<34} {r['protocol']:<18} model {r['model']}")
        print("\noasis-route integrate <tool>  prints the settings; add --check to test them.")
        return 0
    found = integrations.recipe(args.tool, base)
    if found is None:
        ids = ", ".join(x["id"] for x in integrations.recipes(base))
        print(f"unknown tool '{args.tool}'; choose one of: {ids}", file=sys.stderr)
        return 2
    print(json.dumps(found, indent=2) if args.json else integrations.render(found))
    return _check_integration(args, found) if args.check else 0


async def _dev_connect_env(
    ctx: Any, project_id: str, *, free_tier: bool, age: int | None = None, country: str | None = None
) -> None:
    """Connect every provider key found in the environment (GROQ_API_KEY, HF_TOKEN, ...).

    Passing ``--connect-env`` is the owner's confirmation that the keys are theirs. Re-running is
    idempotent: keys already connected are reported, not duplicated. Once any real provider is active the
    local mock providers are paused so ``auto`` never answers with mock output.
    """
    from sqlalchemy import select

    from oasis_route.connections import key_detect
    from oasis_route.connections import quick_connect as quick
    from oasis_route.db.models import Connection, User
    from oasis_route.domain.enums import ConnectionStatus

    text = "\n".join(f"{n}={os.environ[n]}" for n in key_detect.ENV_NAMES if os.environ.get(n))
    parsed = key_detect.parse_text(text)
    if not parsed.keys:
        print("oasis-route dev: --connect-env found no provider keys in the environment", file=sys.stderr)
        return
    async with ctx.db.transaction() as sess:
        admin = (await sess.execute(select(User).order_by(User.created_at))).scalars().first()
        if admin is None:
            return
        # Self-declared, like the console profile: providers with an age or country rule need it.
        if age is not None:
            admin.declared_age = age
        if country:
            admin.country = country.upper()
    summary = await quick.quick_connect(
        ctx,
        project_id=project_id,
        user=admin,
        keys=parsed.keys,
        accept_terms=True,
        free_tier_declared=free_tier,
    )
    for r in summary.results:
        extra = f" ({r.free_models} free models)" if r.free_models else ""
        note = f": {r.message}" if r.message and r.outcome not in ("connected", "already_connected") else ""
        print(
            f"  {r.env_name or r.key_hint}: {r.provider_id or '?'} {r.outcome}{extra}{note}", file=sys.stderr
        )
    async with ctx.db.transaction() as sess:
        conns_ = list(
            (await sess.execute(select(Connection).where(Connection.project_id == project_id))).scalars()
        )
        real_active = any(
            c.status == ConnectionStatus.ACTIVE.value and not c.provider_id.startswith("mock") for c in conns_
        )
        if real_active:
            for c in conns_:
                if c.provider_id.startswith("mock") and c.status == ConnectionStatus.ACTIVE.value:
                    c.status = ConnectionStatus.SUSPENDED.value
                    c.last_error = "paused: real providers are connected"


def _port_in_use(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return True
    return False


def _cmd_dev(args: argparse.Namespace) -> int:
    """One command: SQLite + migrations + two mock providers + API + worker + an admin Oasis Key."""
    busy = [p for p in (args.port, args.mock_port, args.mock_port + 1) if _port_in_use("127.0.0.1", p)]
    if busy:
        print(
            f"{'Port' if len(busy) == 1 else 'Ports'} {', '.join(map(str, busy))} "
            f"{'is' if len(busy) == 1 else 'are'} already in use: is another `oasis-route dev` running? "
            "Stop it, or pick free ports with --port and --mock-port.",
            file=sys.stderr,
        )
        return 2
    import uvicorn

    from oasis_route.app import create_app
    from oasis_route.db import migrate
    from oasis_route.devtools.mock_provider import create_mock_provider

    state = Path(args.state_dir).resolve()
    state.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("OASIS_ROUTE_DATABASE_URL", f"sqlite+aiosqlite:///{state / 'dev.db'}")
    # `dev` is development by definition, even inside the production image (which sets production).
    if os.environ.get("OASIS_ROUTE_ENVIRONMENT", "development") != "development":
        print("oasis-route dev: forcing OASIS_ROUTE_ENVIRONMENT=development", file=sys.stderr)
    os.environ["OASIS_ROUTE_ENVIRONMENT"] = "development"
    os.environ.setdefault("OASIS_ROUTE_ALLOW_PRIVATE_UPSTREAMS", "true")
    os.environ.setdefault("OASIS_ROUTE_ENABLE_MOCK_PROVIDERS", "true")
    get_settings.cache_clear()
    s = get_settings()
    migrate.upgrade(s.database_url)
    key_file = state / "admin.key"

    async def _run() -> None:
        from oasis_route.connections import service as conns
        from oasis_route.registry.catalog import sync_catalog

        ctx = _context(s)
        await sync_catalog(ctx.db, s)
        servers = [
            uvicorn.Server(
                uvicorn.Config(create_mock_provider(), host="127.0.0.1", port=p, log_level="warning")
            )
            for p in (args.mock_port, args.mock_port + 1)
        ]
        api = uvicorn.Server(uvicorn.Config(create_app(s, ctx), host=s.host, port=args.port, log_config=None))
        tasks = [asyncio.create_task(x.serve()) for x in servers]
        while not all(x.started for x in servers):  # noqa: ASYNC110 - uvicorn exposes only a flag
            await asyncio.sleep(0.05)
        created = await _bootstrap_admin(ctx, "admin@localhost", "default")
        if created is not None:
            pid, secret = created
            key_file.write_text(f"{pid}\n{secret}\n")
            key_file.chmod(0o600)
            from sqlalchemy import select

            from oasis_route.db.models import Connection, User

            async with ctx.db.transaction() as sess:
                admin = (await sess.execute(select(User))).scalar_one()
                for prov, port in (("mock", args.mock_port), ("mock_alt", args.mock_port + 1)):
                    await conns.create_connection(
                        ctx,
                        sess,
                        project_id=pid,
                        user=admin,
                        data=conns.ConnectionInput(
                            provider_id=prov,
                            secret={"api_key": "mock-dev-key"},
                            base_url=f"http://127.0.0.1:{port}/v1",
                            accept_terms=True,
                            display_name=f"{prov} (local)",
                        ),
                    )
            async with ctx.db.session() as sess:
                ids = list((await sess.execute(select(Connection.id))).scalars())
            for cid in ids:
                await conns.validate(ctx, cid)
        pid, secret = key_file.read_text().split()[:2] if key_file.exists() else ("?", "?")
        if args.connect_env and pid != "?":
            await _dev_connect_env(ctx, pid, free_tier=args.free_tier, age=args.age, country=args.country)
        base = f"http://{s.host}:{args.port}"
        local_note = ""
        if args.local and pid != "?":
            local_note = await _dev_connect_local(ctx, pid, base)
        console = f"{base}/console/#key={secret}"
        print(
            f"\nOasis Route dev is running.\n  console:  {console}\n"
            "            (one click: the key in the link signs you in and is removed from the address bar)\n"
            f"  API docs: {base}/docs\n{local_note}"
            f"  project:  {pid}\n  export OASIS_API_KEY={secret}\n\n"
            f'  curl {base}/v1/chat/completions -H "Authorization: Bearer $OASIS_API_KEY" \\\n'
            "    -H 'content-type: application/json' "
            '-d \'{"model":"auto","messages":[{"role":"user","content":"hello"}]}\'\n',
            flush=True,
        )
        if args.open:
            tasks.append(asyncio.create_task(_open_when_ready(api, console)))
        try:
            await api.serve()  # the app lifespan supervises the embedded worker
        finally:
            for x in servers:
                x.should_exit = True
            await asyncio.gather(*tasks, return_exceptions=True)
            await ctx.aclose()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())
    return 0


async def _dev_connect_local(ctx: Any, project_id: str, base: str) -> str:
    """Connect running local model servers (Ollama, LM Studio, ...) so `auto` has a real model at once."""
    from sqlalchemy import select

    from oasis_route import onboarding
    from oasis_route.db.models import User

    async with ctx.db.session() as sess:
        admin = (await sess.execute(select(User).where(User.role == "admin"))).scalars().first()
    if admin is None:
        return ""
    try:
        out = await onboarding.apply_setup(ctx, project_id, admin, connect_local=True, base_url=base)
    except Exception as exc:  # detection is a convenience; the server must still start
        return f"  local models: detection failed ({exc})\n"
    servers = [x for x in out["local_servers"]["servers"] if x["reachable"]]
    if not servers:
        return "  local models: none running (optional: https://ollama.com, then `ollama pull qwen2.5`)\n"
    names = ", ".join(
        f"{x['provider_id']} ({len(x['models'])} model{'' if len(x['models']) == 1 else 's'})"
        for x in servers
    )
    return f"  local models: {names} connected; `auto` uses them before the demo\n"


async def _open_when_ready(server: Any, url: str) -> None:
    import webbrowser

    while not server.started:  # noqa: ASYNC110 - uvicorn exposes only a flag
        await asyncio.sleep(0.05)
    await asyncio.to_thread(webbrowser.open, url)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="oasis-route", description="Oasis Route inference control plane")
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("migrate", help="apply or revert database migrations")
    m.add_argument("direction", choices=["up", "down"], nargs="?", default="up")
    m.add_argument("--revision")
    m.set_defaults(func=_cmd_migrate)

    sv = sub.add_parser("serve", help="run the API, gateway, and console")
    sv.add_argument("--host")
    sv.add_argument("--port", type=int)
    sv.add_argument("--migrate", action="store_true", help="apply migrations before starting")
    sv.add_argument(
        "--no-worker",
        action="store_true",
        help="do not supervise background jobs in this process (run `oasis-route worker` separately)",
    )
    sv.set_defaults(func=_cmd_serve)

    w = sub.add_parser("worker", help="run background jobs (safe to run on several hosts)")
    w.add_argument("--once", action="store_true", help="run every due job once and exit")
    w.set_defaults(func=_cmd_worker)

    b = sub.add_parser("bootstrap", help="create the first administrator and print its Oasis Key")
    b.add_argument("--email", required=True)
    b.add_argument("--project", default="default")
    b.set_defaults(func=_cmd_bootstrap)

    rk = sub.add_parser("rotate-kek", help="re-wrap stored credentials under the current master key")
    rk.set_defaults(func=_cmd_rotate_kek)

    mp = sub.add_parser("mock-provider", help="run the deterministic OpenAI-compatible mock provider")
    mp.add_argument("--host", default="127.0.0.1")
    mp.add_argument("--port", type=int, default=9100)
    mp.set_defaults(func=_cmd_mock_provider)

    c = sub.add_parser(
        "connect",
        help="connect every provider key you hold in one step (detects each key's provider locally)",
    )
    c.add_argument("--url", default=os.environ.get("OASIS_ROUTE_URL", "http://127.0.0.1:8080"))
    c.add_argument("--oasis-key", help="defaults to $OASIS_API_KEY")
    c.add_argument("--project", help="defaults to the Oasis Key's project")
    c.add_argument("--from-env", action="store_true", help="read GROQ_API_KEY, GEMINI_API_KEY, ... from env")
    c.add_argument("--env-file", action="append", help="read keys from a .env file (repeatable)")
    c.add_argument("--stdin", action="store_true", help="read pasted keys from standard input")
    c.add_argument(
        "--accept-terms",
        action="store_true",
        help="confirm you hold these accounts and accept each provider's terms (without it: dry run)",
    )
    c.add_argument(
        "--free-tier",
        action="store_true",
        default=None,
        help="declare the accounts have no billing set up (asked interactively when omitted)",
    )
    c.set_defaults(func=_cmd_connect)

    bl = sub.add_parser("billing", help="declare whether billing is set up on a connected provider account")
    bl.add_argument("provider", help="provider id, e.g. groq")
    bl.add_argument("mode", choices=["free_tier", "paid", "unknown"])
    bl.add_argument("--url", default=os.environ.get("OASIS_ROUTE_URL", "http://127.0.0.1:8080"))
    bl.add_argument("--oasis-key", help="defaults to $OASIS_API_KEY or the dev key")
    bl.add_argument("--project", help="defaults to the Oasis Key's project")
    bl.set_defaults(func=_cmd_billing)

    st = sub.add_parser(
        "setup",
        help="turnkey onboarding: detect local servers, list connectable free providers, classify routes",
    )
    _add_api_args(st)
    st.add_argument("--dry-run", action="store_true", help="only report; connect nothing")
    st.add_argument("--no-local", action="store_true", help="do not connect detected local servers")
    st.set_defaults(func=_cmd_setup)

    ig = sub.add_parser(
        "integrate",
        help="settings for Claude Code, Codex, Cursor, Cline, Roo Code, Aider, OpenCode, Continue, SDKs",
    )
    ig.add_argument("tool", nargs="?", help="tool id (omit to list)")
    _add_api_args(ig)
    ig.add_argument("--check", action="store_true", help="test the endpoint with your Oasis Key")
    ig.set_defaults(func=_cmd_integrate)

    from oasis_route import ops_cli

    ops_cli.register(sub, _add_api_args)

    d = sub.add_parser("dev", help="local all-in-one: SQLite, mock providers, API, worker, admin key")
    d.add_argument("--port", type=int, default=8080)
    d.add_argument("--mock-port", type=int, default=9100)
    d.add_argument("--state-dir", default=os.environ.get("OASIS_ROUTE_DEV_STATE_DIR", ".oasis-dev"))
    d.add_argument(
        "--connect-env",
        action="store_true",
        default=os.environ.get("OASIS_ROUTE_DEV_CONNECT_ENV", "").lower() in ("1", "true", "yes"),
        help="connect the provider keys in your environment (GROQ_API_KEY, HF_TOKEN, ...): "
        "confirms they are your own accounts and you accept each provider's terms",
    )
    d.add_argument(
        "--free-tier", action="store_true", help="with --connect-env: the accounts have no billing set up"
    )
    d.add_argument("--age", type=int, help="with --connect-env: your age (some providers require 18+)")
    d.add_argument("--country", help="with --connect-env: your ISO country code, e.g. BD")
    d.add_argument("--open", action="store_true", help="open the console in your browser, signed in")
    d.add_argument(
        "--no-local",
        dest="local",
        action="store_false",
        help="do not detect and connect local model servers (Ollama, LM Studio, vLLM, ...) on start",
    )
    d.set_defaults(func=_cmd_dev)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rc: int = args.func(args)
    return rc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
