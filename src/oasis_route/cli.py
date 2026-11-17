"""Command-line entry point: ``oasis-route``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
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


def _cmd_dev(args: argparse.Namespace) -> int:
    """One command: SQLite + migrations + two mock providers + API + worker + an admin Oasis Key."""
    import uvicorn

    from oasis_route.app import create_app
    from oasis_route.db import migrate
    from oasis_route.devtools.mock_provider import create_mock_provider

    state = Path(args.state_dir).resolve()
    state.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("OASIS_ROUTE_DATABASE_URL", f"sqlite+aiosqlite:///{state / 'dev.db'}")
    os.environ.setdefault("OASIS_ROUTE_ENVIRONMENT", "development")
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

    d = sub.add_parser("dev", help="local all-in-one: SQLite, mock providers, API, worker, admin key")
    d.add_argument("--port", type=int, default=8080)
    d.add_argument("--mock-port", type=int, default=9100)
    d.add_argument("--state-dir", default=".oasis-dev")
    d.set_defaults(func=_cmd_dev)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rc: int = args.func(args)
    return rc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
