"""``aobs-server`` command line.

Subcommands are added by the milestones that need them (database migrations, tenancy and API
keys, serving, retention). Exit codes: 0 success, 2 usage or configuration error.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from importlib.metadata import version

from .config import ConfigError, Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aobs-server", description="aobs collector server")
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('aobs-server')}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.required = True
    commands.add_parser("config", help="validate AOBS_* settings and print them (passwords masked)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"aobs-server: configuration error: {exc}", file=sys.stderr)
        return 2
    if args.command == "config":
        print(json.dumps(settings.describe(), indent=2))
    return 0
