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


def _cmd_connect(args: argparse.Namespace) -> int:
    """Send keys to a running Oasis Route over its API (one-step onboarding)."""
    import httpx

    oasis_key = args.oasis_key or os.environ.get("OASIS_API_KEY", "")
    if not oasis_key:
        print("set OASIS_API_KEY or pass --oasis-key", file=sys.stderr)
        return 2
    text = _connect_text(args)
    if not text.strip():
        print("no keys found: use --from-env, --env-file PATH or --stdin", file=sys.stderr)
        return 2
    headers = {"authorization": f"Bearer {oasis_key}"}
    with httpx.Client(base_url=args.url.rstrip("/"), headers=headers, timeout=120) as c:
        me = c.get("/api/v1/me")
        if me.status_code != 200:
            print(f"Oasis Key rejected: HTTP {me.status_code}", file=sys.stderr)
            return 1
        project = args.project or me.json()["project"]["id"]
        r = c.post(
            f"/api/v1/projects/{project}/connections/quick",
            json={
                "text": text,
                "accept_terms": args.accept_terms,
                "free_tier": args.free_tier,
                "dry_run": not args.accept_terms,
            },
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
    if out.get("dry_run"):
        print(
            "\nNothing was stored. Re-run with --accept-terms to confirm these accounts are yours "
            "and connect them.",
            file=sys.stderr,
        )
        return 0
    print(
        f"\n{out['connected']} connected, {out['already_connected']} already connected, "
        f"{out['needs_attention']} need attention.",
        file=sys.stderr,
    )
    return 0 if out["needs_attention"] == 0 else 3


def _api(args: argparse.Namespace) -> tuple[Any, str] | None:
    """An HTTP client for a running Oasis Route plus the project id (from --project or the Oasis Key)."""
    import httpx

    oasis_key = getattr(args, "oasis_key", None) or os.environ.get("OASIS_API_KEY", "")
    if not oasis_key:
        print("set OASIS_API_KEY or pass --oasis-key", file=sys.stderr)
        return None
    c = httpx.Client(
        base_url=args.url.rstrip("/"), headers={"authorization": f"Bearer {oasis_key}"}, timeout=120
    )
    me = c.get("/api/v1/me")
    if me.status_code != 200:
        print(f"Oasis Key rejected: HTTP {me.status_code}", file=sys.stderr)
        c.close()
        return None
    return c, getattr(args, "project", None) or me.json()["project"]["id"]


def _add_api_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--url", default=os.environ.get("OASIS_ROUTE_URL", "http://127.0.0.1:8080"))
    p.add_argument("--oasis-key", help="defaults to $OASIS_API_KEY")
    p.add_argument("--project", help="defaults to the Oasis Key's project")
    p.add_argument("--json", action="store_true", help="print the raw JSON response")


def _print_setup(out: dict[str, Any]) -> None:
    local = out["local_servers"]
    print("Local servers")
    if not local["enabled"]:
        print(f"  detection off: {local['reason']}")
    for srv in local["servers"]:
        if srv["reachable"]:
            state = "connected" if srv.get("connected") else "found"
            print(f"  {srv['provider_id']:<20} {state:<10} {srv['base_url']}  {len(srv['models'])} models")
    if local["enabled"] and not any(x["reachable"] for x in local["servers"]):
        print("  none running on their default ports")
    for c in out.get("connected_local") or []:
        print(f"  connected {c['provider_id']}: {c['validation'].get('status')}")
    cp = out["connectable_providers"]
    print("\nVerified free providers you can connect")
    for e in cp["verified"]:
        print(f"  {e['provider_id']:<20} {'connected' if e['connected'] else 'available':<10} {e['name']}")
    if not cp["verified"]:
        print("  none reviewed yet")
    print(
        f"  plus {len(cp['advertised'])} advertised free tiers awaiting review "
        "(oasis-route providers --advertised)"
    )
    rs = out["routes"]["summary"]
    print("\nRoutes")
    print(f"  {rs['total']} total · cost {rs['by_cost_class']} · state {rs['by_state']}")
    print(f"  {rs['eligible_free']} eligible free routes · spend policy {rs['spend_policy']}")
    ep = out["endpoint"]
    print("\nOne OpenAI-compatible endpoint")
    print(f"  OPENAI_BASE_URL={ep['base_url']}  OPENAI_API_KEY=$OASIS_API_KEY  model=auto")


def _cmd_setup(args: argparse.Namespace) -> int:
    """Turnkey onboarding against a running server: detect local servers, connect them, classify routes."""
    api = _api(args)
    if api is None:
        return 2
    c, project = api
    with c:
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


def _cmd_dev(args: argparse.Namespace) -> int:
    """One command: SQLite + migrations + two mock providers + API + worker + an admin Oasis Key."""
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
        print(
            f"\nOasis Route dev is running.\n  console:  {base}/console/\n  API docs: {base}/docs\n"
            f"  project:  {pid}\n  export OASIS_API_KEY={secret}\n\n"
            f'  curl {base}/v1/chat/completions -H "Authorization: Bearer $OASIS_API_KEY" \\\n'
            "    -H 'content-type: application/json' "
            '-d \'{"model":"auto","messages":[{"role":"user","content":"hello"}]}\'\n',
            flush=True,
        )
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
    c.add_argument("--free-tier", action="store_true", help="declare the accounts have no billing set up")
    c.set_defaults(func=_cmd_connect)

    st = sub.add_parser(
        "setup",
        help="turnkey onboarding: detect local servers, list connectable free providers, classify routes",
    )
    _add_api_args(st)
    st.add_argument("--dry-run", action="store_true", help="only report; connect nothing")
    st.add_argument("--no-local", action="store_true", help="do not connect detected local servers")
    st.set_defaults(func=_cmd_setup)

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
    d.set_defaults(func=_cmd_dev)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rc: int = args.func(args)
    return rc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
