"""``aobs-server`` command line.

Exit codes: 0 success, 1 operation failed (e.g. unknown project), 2 usage or configuration error.
Commands that create things print one JSON object on stdout so they can be scripted.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from contextlib import ExitStack, contextmanager
from importlib.metadata import version
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Connection

from .config import ConfigError, Settings

if TYPE_CHECKING:
    from pathlib import Path

    from .diagnosis.loader import Source


class CommandError(Exception):
    """The command could not be carried out (exit code 1)."""


Handler = Callable[[argparse.Namespace, Settings], int]
STORED_HELP = "project whose stored traces the db: inputs select (needs AOBS_DATABASE_URL)"


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


def _inputs(
    args: argparse.Namespace, settings: Settings, stack: ExitStack, *groups: Sequence[str]
) -> list[list[Source]]:
    """Resolve FILE arguments: a path, or a ``db:`` selection of ``--project``'s stored traces
    (``aobs_server.diagnosis.stored``). Missing files and unknown trace ids are exit code 1, like a
    missing file; malformed selections are usage errors. The database layer is only imported when
    a ``db:`` input is given."""
    from pathlib import Path

    def is_selection(value: str) -> bool:
        return value.startswith("db:")  # diagnosis.stored.PREFIX, without importing the database layer

    values = [v for group in groups for v in group]
    project = getattr(args, "project", None)
    if not any(is_selection(v) for v in values):
        if project is not None:
            raise ConfigError("--project is only used with db: inputs (e.g. db:trace=<id> or db:task=<name>)")
        for value in values:
            if not Path(value).is_file():
                raise CommandError(f"no such file: {value}")
        return [[Path(v) for v in group] for group in groups]
    if project is None:
        raise ConfigError("db: inputs need --project PROJECT_ID (and AOBS_DATABASE_URL)")
    from sqlalchemy.exc import SQLAlchemyError

    from .diagnosis.stored import SelectionSyntaxError, StoreUnavailable, check_store, open_selection, select_rows
    from .storage.db import make_engine

    project_id = _uuid(project, "project")
    engine = make_engine(settings.require_database_url(), pool_size=1)
    stack.callback(engine.dispose)
    try:
        check_store(engine, project_id)
    except StoreUnavailable as exc:
        raise CommandError(str(exc)) from None
    resolved: list[list[Source]] = []
    for group in groups:
        sources: list[Source] = []
        for value in group:
            if not is_selection(value):
                if not Path(value).is_file():
                    raise CommandError(f"no such file: {value}")
                sources.append(Path(value))
                continue
            try:
                selection = open_selection(engine, project_id, value)
            except SelectionSyntaxError as exc:
                raise ConfigError(f"{value}: {exc}") from None
            if selection.selection.trace_ids:
                try:
                    with engine.connect() as conn:
                        found = {row.trace_id for row in select_rows(conn, project_id, selection.selection)}
                except SQLAlchemyError as exc:
                    raise CommandError(f"cannot read the database ({type(exc).__name__})") from None
                for trace_id in selection.selection.trace_ids:
                    if trace_id not in found:
                        raise CommandError(f"no stored trace {trace_id} in project {project_id}")
            sources.append(selection)
        resolved.append(sources)
    return resolved


@contextmanager
def _reading_inputs() -> Any:
    """Database failures while an analysis reads stored traces are exit code 1, not a traceback."""
    from sqlalchemy.exc import SQLAlchemyError

    try:
        yield
    except SQLAlchemyError as exc:
        raise CommandError(f"cannot read the database ({type(exc).__name__}); check AOBS_DATABASE_URL") from None


def _analyze(args: argparse.Namespace, settings: Settings) -> int:
    import io
    from datetime import UTC, datetime

    from .diagnosis import RuleConfig, build_report, render_text
    from .diagnosis.rules import SEVERITY_ORDER

    if not 0 < args.slow_share <= 1:
        raise ConfigError("--slow-share must be in (0, 1]")
    with ExitStack() as stack, _reading_inputs():
        (paths,) = _inputs(args, settings, stack, args.paths)
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

    from .diagnosis import CompareConfig, NoRunsError, SelectionError, build_comparisons, render_comparison_text
    from .diagnosis.rules import SEVERITY_ORDER

    if args.min_change_pct < 0 or args.min_run_ms < 0 or args.min_op_ms < 0:
        raise ConfigError("--min-change-pct, --min-run-ms and --min-op-ms must not be negative")
    try:
        with ExitStack() as stack, _reading_inputs():
            (paths,) = _inputs(args, settings, stack, [args.baseline, *args.current])
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


def _compare_groups(args: argparse.Namespace, settings: Settings) -> int:
    import io
    from datetime import UTC, datetime

    from .diagnosis import GroupConfig, NoRunsError, build_group_report, render_group_text
    from .diagnosis.rules import SEVERITY_ORDER

    if args.min_runs < 3:
        raise ConfigError("--min-runs must be at least 3 (fewer can never give a conclusive rank test)")
    if not 0 < args.alpha < 1:
        raise ConfigError("--alpha must be between 0 and 1")
    if args.min_change_pct < 0 or args.min_run_ms < 0 or args.min_op_ms < 0:
        raise ConfigError("--min-change-pct, --min-run-ms and --min-op-ms must not be negative")
    config = GroupConfig(
        min_runs=args.min_runs,
        alpha=args.alpha,
        min_change_pct=args.min_change_pct,
        min_run_ms=args.min_run_ms,
        min_op_ms=args.min_op_ms,
    )
    try:
        with ExitStack() as stack, _reading_inputs():
            baseline, current = _inputs(args, settings, stack, args.baseline, args.current)
            report = build_group_report(
                baseline, current, datetime.now(UTC), config, task=args.task, error_messages=not args.no_error_messages
            )
    except NoRunsError as exc:
        raise CommandError(str(exc)) from None
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="replace")
    if args.json:
        _print(report.to_dict())
    else:
        print(render_group_text(report))
    worst = report.severity()
    if args.fail_on and worst and SEVERITY_ORDER[worst] <= SEVERITY_ORDER[args.fail_on]:
        return 3
    return 0


def _gate(args: argparse.Namespace, settings: Settings) -> int:
    import io
    from datetime import UTC, datetime
    from pathlib import Path

    from .diagnosis import InputProblem, NoRunsError, SelectionError
    from .diagnosis.compare import RunProfile
    from .diagnosis.gate import PolicyError, build_gate_report, load_baseline, load_policy, render_gate_text

    try:
        policy = load_policy(Path(args.policy))
    except PolicyError as exc:
        raise ConfigError(str(exc)) from None
    if (policy.baseline is None) != (args.baseline is None):
        raise ConfigError(
            "the policy has a [baseline] section: pass the baseline run with --baseline FILE"
            if args.baseline is None
            else "--baseline was given but the policy has no [baseline] section"
        )
    if args.baseline_trace and args.baseline is None:
        raise ConfigError("--baseline-trace needs --baseline")
    now = datetime.now(UTC)
    baseline: RunProfile | None = None
    baseline_problems: list[InputProblem] = []
    with ExitStack() as stack, _reading_inputs():
        paths, baseline_input = _inputs(args, settings, stack, args.paths, [args.baseline] if args.baseline else [])
        if baseline_input:
            try:
                baseline, baseline_problems = load_baseline(
                    baseline_input[0], now, args.baseline_trace, error_messages=not args.no_error_messages
                )
            except NoRunsError as exc:
                raise CommandError(str(exc)) from None
            except SelectionError as exc:
                raise ConfigError(str(exc)) from None
        report = build_gate_report(
            policy,
            paths,
            now,
            policy_path=args.policy,
            baseline=baseline,
            baseline_problems=baseline_problems,
            error_messages=not args.no_error_messages,
        )
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="replace")
    if args.json:
        _print(report.to_dict())
    else:
        print(render_gate_text(report))
    if args.junit or args.markdown:
        from .diagnosis.gate_reports import render_gate_junit, render_gate_markdown

        for target, render in ((args.junit, render_gate_junit), (args.markdown, render_gate_markdown)):
            if target:
                _write_report(Path(target), render(report))
    return 0 if report.verdict == "pass" else 3


def _write_report(path: Path, text: str) -> None:
    """Write a report file for CI, creating its directory; failing to is exit code 1. A file name
    that is not valid UTF-8 (shown in the report as an input) is written with U+FFFD in its place."""
    import re

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(re.sub("[\ud800-\udfff]", "\ufffd", text), encoding="utf-8")
    except OSError as exc:
        raise CommandError(f"cannot write {path}: {exc.strerror or exc}") from None


def _traces_list(args: argparse.Namespace, settings: Settings) -> int:
    from datetime import UTC, datetime

    from sqlalchemy.exc import SQLAlchemyError

    from .diagnosis.stored import SelectionSyntaxError, StoreUnavailable, check_store, parse_selection, select_rows
    from .query.traces import summary
    from .storage import tenancy
    from .storage.db import make_engine

    project_id = _uuid(args.project, "project")
    try:
        selection = parse_selection(args.selection if args.selection else f"db:limit={args.limit}")
    except SelectionSyntaxError as exc:
        raise ConfigError(str(exc) if args.selection else "--limit must be a whole number from 1 to 1000") from None
    engine = make_engine(settings.require_database_url(), pool_size=1)
    try:
        check_store(engine, project_id)
        with engine.connect() as conn:
            project_settings = tenancy.project_settings(conn, project_id)
            if project_settings is None:  # pragma: no cover - deleted after check_store
                raise StoreUnavailable(f"project {project_id} does not exist")
            rows = select_rows(conn, project_id, selection)
    except StoreUnavailable as exc:
        raise CommandError(str(exc)) from None
    except SQLAlchemyError as exc:
        raise CommandError(f"cannot read the database ({type(exc).__name__}); check AOBS_DATABASE_URL") from None
    finally:
        engine.dispose()
    now = datetime.now(UTC)
    listed = [
        {"trace_id": row.trace_id, "input": f"db:trace={row.trace_id}", **summary(row, project_settings, now)}
        for row in rows
    ]
    if args.json:
        _print({"project_id": project_id, "traces": listed})
        return 0
    if not listed:
        print("no stored traces match")
        return 0
    print(f"{'TRACE ID':32}  {'STARTED (UTC)':20}  {'STATUS':14}  {'SPANS':>5}  {'ERRORS':>6}  RUN / AGENT")
    for item in listed:
        started = (item["started_at"] or "-")[:19].replace("T", " ")
        who = " / ".join(x for x in (item["root_name"], item["agent_name"]) if x) or "-"
        print(
            f"{item['trace_id']}  {started:20}  {item['status']:14}  {item['span_count']:>5}  "
            f"{item['error_count']:>6}  {who}"
        )
    print(f"{len(listed)} trace(s); analyze one with: aobs-server analyze --project {project_id} db:trace=<TRACE ID>")
    return 0


def _serve(args: argparse.Namespace, settings: Settings) -> int:
    import logging

    import uvicorn
    from sqlalchemy.exc import SQLAlchemyError

    from .api import create_app
    from .storage import migrate
    from .storage.db import make_engine

    if not 1 <= args.port <= 65535:
        raise ConfigError("--port must be between 1 and 65535")
    engine = make_engine(settings.require_database_url())
    try:
        current = migrate.current_revision(engine)
    except SQLAlchemyError as exc:
        engine.dispose()
        raise CommandError(f"cannot connect to the database ({type(exc).__name__}); check AOBS_DATABASE_URL") from None
    head = migrate.head_revision()
    if current != head:
        engine.dispose()
        raise CommandError(f"the database schema is at revision {current}, not {head}: run `aobs-server db upgrade`")
    # Only the server's own loggers at INFO: SQLAlchemy at INFO or DEBUG would log statements and rows.
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    aobs_logger = logging.getLogger("aobs")
    aobs_logger.addHandler(handler)
    aobs_logger.setLevel(logging.INFO)
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
    print(f"aobs collector on http://{args.host}:{args.port}/  (POST /v1/ingest)", flush=True)
    print(f"  limits: {settings.max_body_bytes} bytes and {settings.max_events_per_batch} events per batch", flush=True)
    print("  press Ctrl+C to stop", flush=True)
    try:
        uvicorn.run(create_app(settings, engine), host=args.host, port=args.port, log_level="info")
    finally:
        engine.dispose()
    return 0


def _dashboard(args: argparse.Namespace, settings: Settings) -> int:
    import ipaddress
    import os
    import secrets
    from pathlib import Path

    import uvicorn

    from .dashboard import DashboardConfig, create_app
    from .dashboard.datasets import REPO_EXAMPLES

    samples = None if args.no_samples else Path(args.samples_dir) if args.samples_dir else REPO_EXAMPLES
    data_dir = Path(args.data_dir).resolve() if args.data_dir else None
    if data_dir is not None and not data_dir.is_dir():
        raise CommandError(f"--data-dir {data_dir} is not a directory")
    project_id = _uuid(args.project, "project") if args.project else None
    if samples is None and data_dir is None and project_id is None:
        raise ConfigError("nothing to show: use --data-dir or --project, or drop --no-samples")
    if not 1 <= args.port <= 65535:
        raise ConfigError("--port must be between 1 and 65535")
    protected = data_dir is not None or project_id is not None
    token = (os.environ.get("AOBS_DASHBOARD_TOKEN") or secrets.token_urlsafe(24)) if protected else None
    engine = None
    if project_id is not None:
        from .diagnosis.stored import StoreUnavailable, check_store
        from .storage.db import make_engine

        engine = make_engine(settings.require_database_url(), pool_size=2)
        try:
            check_store(engine, project_id)
        except StoreUnavailable as exc:
            engine.dispose()
            raise CommandError(str(exc)) from None
    app = create_app(
        DashboardConfig(samples_dir=samples, data_dir=data_dir, token=token, project_id=project_id, engine=engine)
    )
    url = f"http://{'localhost' if args.host in ('127.0.0.1', '::1', 'localhost') else args.host}:{args.port}/"
    print(f"aobs dashboard on {url}", flush=True)
    if samples is not None and not samples.is_dir():
        print(f"  note: sample data not found at {samples}; only --data-dir will be shown", flush=True)
    if data_dir is not None:
        print(f"  local trace files: {data_dir}", flush=True)
    if project_id is not None:
        print(f"  stored traces of project {project_id} (PostgreSQL, AOBS_DATABASE_URL)", flush=True)
    if protected:
        print(f"  open {url}#token={token}", flush=True)
        print(
            "  (or paste the token into the dashboard when it asks; it is required for local files and stored traces)",
            flush=True,
        )
    try:
        loopback = ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        loopback = args.host == "localhost"
    if not loopback:
        print(f"  warning: listening on {args.host}, reachable from other machines on this network", flush=True)
    print("  press Ctrl+C to stop", flush=True)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        if engine is not None:
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
        help="diagnose saved traces (SDK JSONL files, or traces stored in PostgreSQL)",
        description="Diagnose saved traces with deterministic rules. An INPUT is a trace file, or db:KEY=VALUE&... "
        "selecting --project's stored traces (see docs/stored-analysis.md). Content is never shown; error messages "
        "are secret-scrubbed. Exit codes: 0 report printed, 1 no traces found, missing input or database "
        "unavailable, 2 invalid options, 3 --fail-on threshold reached.",
    )
    analyze.add_argument(
        "paths", nargs="+", metavar="INPUT", help="JSONL from JsonlFileExporter, a JSON batch, or a db: selection"
    )
    analyze.add_argument("--project", metavar="PROJECT_ID", help=STORED_HELP)
    analyze.add_argument("--json", action="store_true", help="machine-readable output")
    analyze.add_argument("--trace", metavar="ID", help="only the trace whose id starts with ID")
    analyze.add_argument("--no-error-messages", action="store_true", help="omit error messages entirely")
    analyze.add_argument("--slow-share", type=float, default=0.5, help="bottleneck share of the run (default 0.5)")
    analyze.add_argument("--slow-min-ms", type=float, default=100.0, help="bottleneck minimum duration (default 100)")
    analyze.add_argument("--fail-on", choices=["error", "warning", "info"], help="exit 3 if a finding is this severe")
    analyze.set_defaults(handler=_analyze)

    compare = commands.add_parser(
        "compare",
        help="compare a baseline run with later runs and report regressions (files or stored traces)",
        description="Compare the run in BASELINE with the run in each CURRENT input (a trace file, or a db: "
        "selection of --project's stored traces). Operations are matched by "
        "kind and tool/span name, not span id. Exit codes: 0 report printed, 1 unreadable input or no runs, "
        "2 invalid options or ambiguous run selection, 3 --fail-on threshold reached.",
    )
    compare.add_argument("baseline", metavar="BASELINE", help="trace file or db: selection of the known-good run")
    compare.add_argument("current", nargs="+", metavar="CURRENT", help="trace file(s) or db: selection(s) to check")
    compare.add_argument("--project", metavar="PROJECT_ID", help=STORED_HELP)
    compare.add_argument("--baseline-trace", metavar="ID", help="pick the baseline run by trace id prefix")
    compare.add_argument("--current-trace", metavar="ID", help="pick the current run(s) by trace id prefix")
    compare.add_argument("--json", action="store_true", help="machine-readable output")
    compare.add_argument("--no-error-messages", action="store_true", help="omit error messages entirely")
    compare.add_argument("--min-change-pct", type=float, default=20.0, help="smallest relative change (default 20)")
    compare.add_argument("--min-run-ms", type=float, default=100.0, help="smallest run slowdown (default 100)")
    compare.add_argument("--min-op-ms", type=float, default=50.0, help="smallest per-call slowdown (default 50)")
    compare.add_argument("--fail-on", choices=["error", "warning", "info"], help="exit 3 if a finding is this severe")
    compare.set_defaults(handler=_compare)

    groups = commands.add_parser(
        "compare-groups",
        help="compare repeated runs of a task (baseline group vs current group) with small-sample statistics",
        description="Compare every run in the --baseline files with every run in the --current files. Each metric "
        "gets medians, variation, a rank or exact test (Holm-adjusted) and a verdict that needs both statistical "
        "evidence and a practically relevant change. Exit codes: 0 report printed, 1 unreadable input or no runs, "
        "2 invalid options, 3 --fail-on reached (error: a likely regression; warning: also a weak signal of a "
        "regression or a comparability warning).",
    )
    groups.add_argument(
        "--baseline", nargs="+", required=True, metavar="INPUT", help="trace files or db: selections: baseline runs"
    )
    groups.add_argument(
        "--current", nargs="+", required=True, metavar="INPUT", help="trace files or db: selections: current runs"
    )
    groups.add_argument("--project", metavar="PROJECT_ID", help=STORED_HELP)
    groups.add_argument("--task", metavar="NAME", help="only runs whose run (root span) name is NAME")
    groups.add_argument("--json", action="store_true", help="machine-readable output")
    groups.add_argument("--no-error-messages", action="store_true", help="omit error messages entirely")
    groups.add_argument("--min-runs", type=int, default=5, help="usable runs per group needed for a test (default 5)")
    groups.add_argument("--alpha", type=float, default=0.05, help="significance level after Holm (default 0.05)")
    groups.add_argument("--min-change-pct", type=float, default=20.0, help="practical relative change (default 20)")
    groups.add_argument("--min-run-ms", type=float, default=50.0, help="practical run slowdown in ms (default 50)")
    groups.add_argument("--min-op-ms", type=float, default=20.0, help="practical per-call slowdown in ms (default 20)")
    groups.add_argument("--fail-on", choices=["error", "warning", "info"], help="exit 3 if a finding is this severe")
    groups.set_defaults(handler=_compare_groups)

    gate = commands.add_parser(
        "gate",
        help="check saved runs against the requirements in a policy file: PASS or FAIL (for CI)",
        description="Check every run in the trace files against the requirements in POLICY (TOML; see "
        "docs/reliability-gate.md). Each check is PASS, FAIL or UNKNOWN (the trace lacks the data) with evidence. "
        "--junit and --markdown also write CI report files. Exit codes: 0 PASS, 1 missing input, no runs in the "
        "baseline, database unavailable or a report file that cannot be written, 2 invalid policy or options, "
        "3 FAIL.",
    )
    gate.add_argument(
        "paths", nargs="+", metavar="INPUT", help="trace files (SDK JSONL or JSON batches) or db: selections"
    )
    gate.add_argument("--project", metavar="PROJECT_ID", help=STORED_HELP)
    gate.add_argument("--policy", required=True, metavar="POLICY", help="requirements file (TOML)")
    gate.add_argument(
        "--baseline", metavar="INPUT", help="known-good run (file or db: selection) for the [baseline] requirements"
    )
    gate.add_argument("--baseline-trace", metavar="ID", help="pick the baseline run by trace id prefix")
    gate.add_argument("--json", action="store_true", help="machine-readable output")
    gate.add_argument("--no-error-messages", action="store_true", help="omit error messages entirely")
    gate.add_argument(
        "--junit", metavar="FILE", help="also write a JUnit XML report (one test case per check) for CI test reports"
    )
    gate.add_argument(
        "--markdown", metavar="FILE", help='also write a Markdown summary, e.g. --markdown "$GITHUB_STEP_SUMMARY"'
    )
    gate.set_defaults(handler=_gate)

    traces = commands.add_parser("traces", help="stored traces").add_subparsers(dest="traces_command", metavar="ACTION")
    traces.required = True
    traces_list = traces.add_parser(
        "list",
        help="list a project's stored traces (most recently started first)",
        description="List stored traces of --project, optionally filtered by a db: selection (keys: trace, task, "
        "agent, session, environment, status, since, until, limit). Each line's trace id can be analyzed with "
        "db:trace=<id>. Content is never read.",
    )
    traces_list.add_argument("selection", nargs="?", metavar="SELECTION", help="e.g. db:task=answer_ticket&limit=20")
    traces_list.add_argument("--project", required=True, metavar="PROJECT_ID")
    traces_list.add_argument("--limit", type=int, default=50, help="without a SELECTION: how many (default 50)")
    traces_list.add_argument("--json", action="store_true", help="machine-readable output")
    traces_list.set_defaults(handler=_traces_list)

    serve = commands.add_parser(
        "serve",
        help="run the collector API (POST /v1/ingest) against PostgreSQL",
        description="Serve the authenticated ingestion API. Needs AOBS_DATABASE_URL and a database migrated with "
        "`aobs-server db upgrade`; refuses to start otherwise.",
    )
    serve.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8000, help="port (default 8000)")
    serve.set_defaults(handler=_serve)

    dash = commands.add_parser(
        "dashboard",
        help="start the web dashboard (sample data, a directory of trace files, and/or a project's stored traces)",
        description="Serve the read-only dashboard. Sample recordings from examples/diagnose are shown by default; "
        "--data-dir adds your own JSONL trace files and --project a project's traces stored in PostgreSQL "
        "(AOBS_DATABASE_URL), both protected by a token printed at start-up.",
    )
    dash.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    dash.add_argument("--port", type=int, default=8800, help="port (default 8800)")
    dash.add_argument("--data-dir", metavar="DIR", help="directory of JSONL trace files to show (needs the token)")
    dash.add_argument(
        "--samples-dir", metavar="DIR", help="where the sample recordings live (default examples/diagnose)"
    )
    dash.add_argument("--no-samples", action="store_true", help="do not show the sample datasets")
    dash.add_argument(
        "--project", metavar="PROJECT_ID", help="also show this project's stored traces (needs the token)"
    )
    dash.set_defaults(handler=_dashboard)
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
