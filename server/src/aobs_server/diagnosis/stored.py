"""Traces stored in PostgreSQL as an input to ``analyze``, ``compare``, ``compare-groups``, ``gate``
and the dashboard, wherever a trace file is accepted.

A selection names one project's stored traces::

    db:trace=4bf92f3577b34da6a3ce929d0e0e4736            one trace (repeat trace=, or separate with commas)
    db:task=answer_ticket&since=2026-10-01&limit=20      filters; the most recently started first

Keys: ``trace``, ``task`` (root span name), ``agent``, ``session``, ``environment``, ``status``
(stored status: ok, ok_with_errors, error, cancelled, incomplete, open), ``since`` and ``until``
(start time, ISO 8601; a date means midnight UTC; ``until`` is exclusive) and ``limit`` (default
100, at most 1000). Values are URL-encoded (``%20`` or ``+`` for a space).

Reading follows the trace read API (``aobs_server.query.traces``): every query is filtered on the
project id, deleted traces are invisible, the project's **current** privacy policy is applied to
the stored events again, and ``span_content`` is never read. The loader then applies its own
metadata-only step, exactly as for files.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Any
from urllib.parse import parse_qsl
from uuid import UUID

from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from ..ingest.prepare import prepare_event
from ..model.versioning import AcceptedEvent
from ..model.wire import ENVIRONMENT_PATTERN, TRACE_ID_PATTERN, ecma_to_python
from ..storage import migrate, tenancy
from ..storage.traces import load_trace_events, select_traces, trace_resources
from .loader import SourceRead

PREFIX = "db:"
STATUSES = ("ok", "ok_with_errors", "error", "cancelled", "incomplete", "open")
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
MAX_VALUE = 256
_TRACE_ID = ecma_to_python(TRACE_ID_PATTERN)
_ENVIRONMENT = ecma_to_python(ENVIRONMENT_PATTERN)
_SINGLE_KEYS = ("task", "agent", "session", "environment", "status", "since", "until", "limit")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")


class SelectionSyntaxError(ValueError):
    """A ``db:`` selection that cannot be parsed (a usage error)."""


class StoreUnavailable(Exception):
    """The database cannot be used: unreachable, not migrated, or the project does not exist."""


@dataclass(frozen=True)
class Selection:
    trace_ids: tuple[str, ...] = ()
    task: str | None = None
    agent: str | None = None
    session: str | None = None
    environment: str | None = None
    status: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = DEFAULT_LIMIT


def is_selection(text: str) -> bool:
    return text.startswith(PREFIX)


def _instant(key: str, value: str) -> datetime:
    try:
        parsed = (
            datetime.combine(datetime.fromisoformat(value).date(), time.min)
            if _DATE.match(value)
            else datetime.fromisoformat(value.replace("Z", "+00:00"))
        )
    except ValueError:
        raise SelectionSyntaxError(f"{key}={value!r} is not an ISO 8601 date or time") from None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def parse_selection(text: str) -> Selection:
    """Parse ``db:key=value&...`` (the ``db:`` prefix is optional). Raises SelectionSyntaxError."""
    body = text[len(PREFIX) :] if text.startswith(PREFIX) else text
    if not body:
        raise SelectionSyntaxError("empty selection: use e.g. db:trace=<trace id> or db:task=<run name>")
    try:
        pairs = parse_qsl(body, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise SelectionSyntaxError(f"cannot parse selection {text!r}: expected key=value pairs joined by &") from None
    trace_ids: list[str] = []
    single: dict[str, str] = {}
    for key, value in pairs:
        if not value or (key != "trace" and len(value) > MAX_VALUE):  # trace ids are checked one by one
            raise SelectionSyntaxError(f"{key}= needs a value of 1 to {MAX_VALUE} characters")
        if key == "trace":
            for trace_id in value.split(","):
                if not _TRACE_ID.match(trace_id):
                    raise SelectionSyntaxError(f"trace={trace_id!r} is not a trace id (32 lowercase hex characters)")
                if trace_id not in trace_ids:
                    trace_ids.append(trace_id)
        elif key in _SINGLE_KEYS:
            if key in single:
                raise SelectionSyntaxError(f"{key}= is given more than once")
            single[key] = value
        else:
            raise SelectionSyntaxError(f"unknown selection key {key!r}; use trace, {', '.join(_SINGLE_KEYS)}")
    if len(trace_ids) > MAX_LIMIT:
        raise SelectionSyntaxError(f"at most {MAX_LIMIT} trace ids per selection")
    if "status" in single and single["status"] not in STATUSES:
        raise SelectionSyntaxError(f"status must be one of {', '.join(STATUSES)}")
    if "environment" in single and not _ENVIRONMENT.match(single["environment"]):
        raise SelectionSyntaxError("environment must match the wire schema's environment pattern")
    limit = DEFAULT_LIMIT if not trace_ids else max(len(trace_ids), DEFAULT_LIMIT)
    if "limit" in single:
        if not single["limit"].isdigit() or not 1 <= int(single["limit"]) <= MAX_LIMIT:
            raise SelectionSyntaxError(f"limit must be a whole number from 1 to {MAX_LIMIT}")
        limit = int(single["limit"])
    since = _instant("since", single["since"]) if "since" in single else None
    until = _instant("until", single["until"]) if "until" in single else None
    if since is not None and until is not None and since >= until:
        raise SelectionSyntaxError("since must be earlier than until")
    return Selection(
        tuple(trace_ids),
        single.get("task"),
        single.get("agent"),
        single.get("session"),
        single.get("environment"),
        single.get("status"),
        since,
        until,
        limit,
    )


def select_rows(conn: Connection, project_id: UUID, selection: Selection) -> list[Any]:
    """The project's trace summary rows matching ``selection``, most recently started first."""
    return select_traces(
        conn,
        project_id,
        limit=selection.limit,
        trace_ids=selection.trace_ids,
        root_name=selection.task,
        agent_name=selection.agent,
        session_id=selection.session,
        environment=selection.environment,
        status=selection.status,
        started_since=selection.since,
        started_before=selection.until,
    )


def check_store(engine: Engine, project_id: UUID) -> None:
    """Fail early and clearly: the database is reachable, migrated to the latest schema, and holds
    the project. Raises StoreUnavailable."""
    try:
        current = migrate.current_revision(engine)
        head = migrate.head_revision()
        if current != head:
            raise StoreUnavailable(
                f"the database schema is at revision {current}, not {head}: run `aobs-server db upgrade`"
            )
        with engine.connect() as conn:
            exists = tenancy.project_settings(conn, project_id) is not None
    except SQLAlchemyError as exc:
        raise StoreUnavailable(f"cannot read the database ({type(exc).__name__}); check AOBS_DATABASE_URL") from None
    if not exists:
        raise StoreUnavailable(f"project {project_id} does not exist")


@dataclass(frozen=True)
class StoredTraces:
    """A selection of one project's stored traces, readable by ``diagnosis.loader.load``."""

    engine: Engine
    project_id: UUID
    selection: Selection
    label: str  # how reports name this input, e.g. "db:task=answer_ticket"

    def __str__(self) -> str:
        return self.label

    def read(self) -> SourceRead:
        # One snapshot for the whole selection: traces written meanwhile are seen whole or not at all.
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
            settings = tenancy.project_settings(conn, self.project_id)
            if settings is None:
                raise StoreUnavailable(f"project {self.project_id} does not exist")
            rows = select_rows(conn, self.project_id, self.selection)
            found = [row.trace_id for row in rows]
            problems = [
                f"no stored trace {trace_id} in this project (unknown, deleted, or in another project)"
                for trace_id in self.selection.trace_ids
                if trace_id not in found
            ]
            if not rows and not self.selection.trace_ids:
                problems.append("the selection matched no stored traces in this project")
            counters: Counter[str] = Counter()
            events: list[AcceptedEvent] = []
            for row in rows:
                if row.truncated:
                    problems.append(
                        f"trace {row.trace_id} was truncated at ingest (the project's span limit): "
                        "some of its spans or span events were not stored"
                    )
                stored = load_trace_events(conn, self.project_id, row.trace_id, include_content=False)
                events.extend(prepare_event(e, settings, counters) for e in stored)  # current policy
            resources = trace_resources(conn, self.project_id, found)
        return SourceRead(events, resources, problems, len(rows))


def open_selection(engine: Engine, project_id: UUID, text: str) -> StoredTraces:
    """A ``db:`` argument as an input. Raises SelectionSyntaxError."""
    return StoredTraces(engine, project_id, parse_selection(text), text)
