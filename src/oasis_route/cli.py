"""Command-line entry point: ``oasis-route``."""

from __future__ import annotations

import argparse
import asyncio
import sys

from oasis_route.config import get_settings


def _cmd_migrate(args: argparse.Namespace) -> int:
    from oasis_route.db import migrate

    url = get_settings().database_url
    if args.direction == "up":
        migrate.upgrade(url, args.revision or "head")
    else:
        migrate.downgrade(url, args.revision or "-1")
    print(f"migrations {args.direction} complete")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from oasis_route.app import create_app

    s = get_settings()
    if args.migrate:
        from oasis_route.db import migrate

        migrate.upgrade(s.database_url)
    uvicorn.run(
        create_app(s), host=args.host or s.host, port=args.port or s.port, log_config=None, proxy_headers=True
    )
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
    sv.set_defaults(func=_cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rc: int = args.func(args)
    return rc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
    _ = asyncio  # keep import for subcommands added later
