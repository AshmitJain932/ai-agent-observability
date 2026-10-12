"""``aobs-server`` command line.

Exit codes: 0 success, 1 operation failed (e.g. unknown project), 2 usage or configuration error.
Commands that create things print one JSON object on stdout so they can be scripted.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from importlib.metadata import version
from typing import Any

from .config import ConfigError, Settings


class CommandError(Exception):
    """The command could not be carried out (exit code 1)."""


Handler = Callable[[argparse.Namespace, Settings], int]


def _print(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


# --------------------------------------------------------------------------- handlers


def _config(args: argparse.Namespace, settings: Settings) -> int:
    _print(settings.describe())
    return 0


def _db_upgrade(args: argparse.Namespace, settings: Settings) -> int:
    from .storage import migrate

    migrate.upgrade(settings.require_database_url(), args.revision)
    _print({"upgraded_to": args.revision})
    return 0


def _db_current(args: argparse.Namespace, settings: Settings) -> int:
    from .storage import migrate
    from .storage.db import make_engine

    engine = make_engine(settings.require_database_url(), pool_size=1)
    try:
        _print({"revision": migrate.current_revision(engine)})
    finally:
        engine.dispose()
    return 0


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aobs-server", description="aobs collector server")
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('aobs-server')}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.required = True

    config = commands.add_parser("config", help="validate AOBS_* settings and print them (passwords masked)")
    config.set_defaults(handler=_config)

    db = commands.add_parser("db", help="database schema").add_subparsers(dest="db_command", metavar="ACTION")
    db.required = True
    upgrade = db.add_parser("upgrade", help="apply migrations (default: to the latest revision)")
    upgrade.add_argument("--revision", default="head")
    upgrade.set_defaults(handler=_db_upgrade)
    db.add_parser("current", help="print the current schema revision").set_defaults(handler=_db_current)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env()
        handler: Handler = args.handler
        return handler(args, settings)
    except ConfigError as exc:
        print(f"aobs-server: configuration error: {exc}", file=sys.stderr)
        return 2
    except CommandError as exc:
        print(f"aobs-server: {exc}", file=sys.stderr)
        return 1
