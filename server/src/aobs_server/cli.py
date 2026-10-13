"""``aobs-server`` command line.

Exit codes: 0 success, 1 operation failed (e.g. unknown project), 2 usage or configuration error.
Commands that create things print one JSON object on stdout so they can be scripted.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from importlib.metadata import version
from typing import Any
from uuid import UUID

from sqlalchemy import Connection

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


@contextmanager
def _transaction(settings: Settings) -> Any:
    from .storage.db import make_engine

    engine = make_engine(settings.require_database_url(), pool_size=1)
    try:
        with engine.begin() as conn:
            yield conn
    finally:
        engine.dispose()


def _uuid(value: str, what: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise CommandError(f"{what} {value!r} is not a valid id") from None


def _org_create(args: argparse.Namespace, settings: Settings) -> int:
    from .storage import tenancy

    with _transaction(settings) as conn:
        org_id = tenancy.create_organization(conn, args.name)
    _print({"organization_id": org_id, "name": args.name})
    return 0


def _project_create(args: argparse.Namespace, settings: Settings) -> int:
    from pydantic import ValidationError

    from .project_settings import ProjectSettings
    from .storage import tenancy

    org_id = _uuid(args.org, "organization")
    try:
        project_settings = ProjectSettings(
            capture_ceiling=args.capture_ceiling,
            error_messages=args.error_messages,
            stack_traces=args.stack_traces,
            retention_days=args.retention_days,
            max_spans_per_trace=args.max_spans_per_trace,
        )
    except ValidationError as exc:
        raise ConfigError(f"invalid project settings: {exc.errors(include_url=False)[0]['msg']}") from None
    with _transaction(settings) as conn:
        if not tenancy.organization_exists(conn, org_id):
            raise CommandError(f"organization {org_id} does not exist")
        project_id = tenancy.create_project(conn, org_id, args.name, project_settings)
    _print({"project_id": project_id, "name": args.name, "settings": project_settings.to_db()})
    return 0


def _project_or_fail(conn: Connection, project_id: UUID) -> None:
    from .storage import tenancy

    if tenancy.project_settings(conn, project_id) is None:
        raise CommandError(f"project {project_id} does not exist")


def _key_create(args: argparse.Namespace, settings: Settings) -> int:
    from .auth.service import create_key
    from .model.wire import ENVIRONMENT_PATTERN, ecma_to_python

    project_id = _uuid(args.project, "project")
    scopes = tuple(s.strip() for s in args.scopes.split(",") if s.strip())
    if not scopes or set(scopes) - {"ingest", "read", "admin"}:
        raise ConfigError("--scopes must be a comma-separated subset of ingest,read,admin")
    if args.environment is not None and not ecma_to_python(ENVIRONMENT_PATTERN).match(args.environment):
        raise ConfigError("--environment must match the wire schema's environment pattern ([a-z0-9][a-z0-9_.-]*)")
    with _transaction(settings) as conn:
        _project_or_fail(conn, project_id)
        key_id, key = create_key(conn, project_id, args.name, scopes, args.environment)
    _print(
        {
            "key_id": key_id,
            "api_key": key.secret,
            "prefix": key.prefix,
            "project_id": project_id,
            "scopes": sorted(set(scopes)),
            "environment": args.environment,
            "note": "Store this key now. Only its hash is kept; it cannot be shown again.",
        }
    )
    return 0


def _key_list(args: argparse.Namespace, settings: Settings) -> int:
    from dataclasses import asdict

    from .storage import tenancy

    project_id = _uuid(args.project, "project")
    with _transaction(settings) as conn:
        _project_or_fail(conn, project_id)
        keys = tenancy.list_api_keys(conn, project_id)
    _print([asdict(k) for k in keys])
    return 0


def _key_revoke(args: argparse.Namespace, settings: Settings) -> int:
    from datetime import UTC, datetime

    from .storage import tenancy

    key_id = _uuid(args.key_id, "key")
    with _transaction(settings) as conn:
        if not tenancy.revoke_api_key(conn, key_id, datetime.now(UTC)):
            raise CommandError(f"no active key {key_id}")
    _print({"revoked": key_id})
    return 0


def _analyze(args: argparse.Namespace, settings: Settings) -> int:
    import io
    from datetime import UTC, datetime
    from pathlib import Path

    from .diagnosis import RuleConfig, build_report, render_text
    from .diagnosis.rules import SEVERITY_ORDER

    paths = [Path(p) for p in args.paths]
    for path in paths:
        if not path.is_file():
            raise CommandError(f"no such file: {path}")
    if not 0 < args.slow_share <= 1:
        raise ConfigError("--slow-share must be in (0, 1]")
    report = build_report(
        paths,
        datetime.now(UTC),
        RuleConfig(slow_share=args.slow_share, slow_min_ms=args.slow_min_ms),
        error_messages=not args.no_error_messages,
        trace_id=args.trace,
    )
    if isinstance(sys.stdout, io.TextIOWrapper):  # e.g. a Windows console that cannot print every character
        sys.stdout.reconfigure(errors="replace")
    if args.json:
        _print(report.to_dict())
    else:
        print(render_text(report))
    if not report.traces:
        return 1
    worst = report.worst_severity()
    if args.fail_on and worst and SEVERITY_ORDER[worst] <= SEVERITY_ORDER[args.fail_on]:
        return 3
    return 0


def _compare(args: argparse.Namespace, settings: Settings) -> int:
    import io
    from datetime import UTC, datetime
    from pathlib import Path

    from .diagnosis import CompareConfig, NoRunsError, SelectionError, build_comparisons, render_comparison_text
    from .diagnosis.rules import SEVERITY_ORDER

    paths = [Path(p) for p in [args.baseline, *args.current]]
    for path in paths:
        if not path.is_file():
            raise CommandError(f"no such file: {path}")
    if args.min_change_pct < 0 or args.min_run_ms < 0 or args.min_op_ms < 0:
        raise ConfigError("--min-change-pct, --min-run-ms and --min-op-ms must not be negative")
    try:
        report = build_comparisons(
            paths[0],
            paths[1:],
            datetime.now(UTC),
            CompareConfig(min_change_pct=args.min_change_pct, min_run_ms=args.min_run_ms, min_op_ms=args.min_op_ms),
            baseline_trace=args.baseline_trace,
            current_trace=args.current_trace,
            error_messages=not args.no_error_messages,
        )
    except NoRunsError as exc:
        raise CommandError(str(exc)) from None
    except SelectionError as exc:
        raise ConfigError(str(exc)) from None
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="replace")
    if args.json:
        _print(report.to_dict())
    else:
        print(render_comparison_text(report))
    worst = report.worst_severity()
    if args.fail_on and worst and SEVERITY_ORDER[worst] <= SEVERITY_ORDER[args.fail_on]:
        return 3
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

    org = commands.add_parser("org", help="organizations").add_subparsers(dest="org_command", metavar="ACTION")
    org.required = True
    org_create = org.add_parser("create", help="create an organization")
    org_create.add_argument("--name", required=True)
    org_create.set_defaults(handler=_org_create)

    project = commands.add_parser("project", help="projects").add_subparsers(dest="project_command", metavar="ACTION")
    project.required = True
    project_create = project.add_parser("create", help="create a project")
    project_create.add_argument("--org", required=True, help="organization id")
    project_create.add_argument("--name", required=True)
    project_create.add_argument(
        "--capture-ceiling",
        choices=["metadata_only", "redacted", "full"],
        default="metadata_only",
        help="most content this project stores, whatever SDKs send (default: metadata_only)",
    )
    project_create.add_argument("--error-messages", action=argparse.BooleanOptionalAction, default=True)
    project_create.add_argument("--stack-traces", action=argparse.BooleanOptionalAction, default=False)
    project_create.add_argument("--retention-days", type=int, default=30)
    project_create.add_argument("--max-spans-per-trace", type=int, default=10_000)
    project_create.set_defaults(handler=_project_create)

    key = commands.add_parser("key", help="API keys").add_subparsers(dest="key_command", metavar="ACTION")
    key.required = True
    key_create = key.add_parser("create", help="create an API key (printed once)")
    key_create.add_argument("--project", required=True, help="project id")
    key_create.add_argument("--name", required=True, help="label, e.g. the service using the key")
    key_create.add_argument("--scopes", default="ingest", help="comma-separated: ingest,read,admin (default ingest)")
    key_create.add_argument("--environment", help="only allow writing this environment")
    key_create.set_defaults(handler=_key_create)
    key_list = key.add_parser("list", help="list a project's keys (never shows secrets)")
    key_list.add_argument("--project", required=True)
    key_list.set_defaults(handler=_key_list)
    key_revoke = key.add_parser("revoke", help="revoke a key immediately")
    key_revoke.add_argument("--key-id", required=True)
    key_revoke.set_defaults(handler=_key_revoke)

    analyze = commands.add_parser(
        "analyze",
        help="diagnose saved traces (SDK JSONL files); no database or server needed",
        description="Diagnose saved traces with deterministic rules. Content is never shown; error messages "
        "are secret-scrubbed. Exit codes: 0 report printed, 1 no traces found, 3 --fail-on threshold reached.",
    )
    analyze.add_argument("paths", nargs="+", metavar="FILE", help="JSONL from JsonlFileExporter, or a JSON batch")
    analyze.add_argument("--json", action="store_true", help="machine-readable output")
    analyze.add_argument("--trace", metavar="ID", help="only the trace whose id starts with ID")
    analyze.add_argument("--no-error-messages", action="store_true", help="omit error messages entirely")
    analyze.add_argument("--slow-share", type=float, default=0.5, help="bottleneck share of the run (default 0.5)")
    analyze.add_argument("--slow-min-ms", type=float, default=100.0, help="bottleneck minimum duration (default 100)")
    analyze.add_argument("--fail-on", choices=["error", "warning", "info"], help="exit 3 if a finding is this severe")
    analyze.set_defaults(handler=_analyze)

    compare = commands.add_parser(
        "compare",
        help="compare a baseline run with later runs and report regressions; no database or server needed",
        description="Compare the run in BASELINE with the run in each CURRENT file. Operations are matched by "
        "kind and tool/span name, not span id. Exit codes: 0 report printed, 1 unreadable input or no runs, "
        "2 invalid options or ambiguous run selection, 3 --fail-on threshold reached.",
    )
    compare.add_argument("baseline", metavar="BASELINE", help="trace file of the known-good (earlier) run")
    compare.add_argument("current", nargs="+", metavar="CURRENT", help="trace file(s) of the run(s) to check")
    compare.add_argument("--baseline-trace", metavar="ID", help="pick the baseline run by trace id prefix")
    compare.add_argument("--current-trace", metavar="ID", help="pick the current run(s) by trace id prefix")
    compare.add_argument("--json", action="store_true", help="machine-readable output")
    compare.add_argument("--no-error-messages", action="store_true", help="omit error messages entirely")
    compare.add_argument("--min-change-pct", type=float, default=20.0, help="smallest relative change (default 20)")
    compare.add_argument("--min-run-ms", type=float, default=100.0, help="smallest run slowdown (default 100)")
    compare.add_argument("--min-op-ms", type=float, default=50.0, help="smallest per-call slowdown (default 50)")
    compare.add_argument("--fail-on", choices=["error", "warning", "info"], help="exit 3 if a finding is this severe")
    compare.set_defaults(handler=_compare)
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
