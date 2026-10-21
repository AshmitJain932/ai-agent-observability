"""Trace storage: the write path behind ``POST /v1/ingest``, and reading stored events back.

Everything runs inside the caller's transaction (one per batch), so a failure anywhere rolls the
whole batch back. The protocol (docs/architecture.md §5, docs/ingestion.md):

1. **Tombstones.** Events for a deleted trace are dropped, never stored.
2. **Per-trace lock.** One ``INSERT … ON CONFLICT DO UPDATE`` creates any missing trace rows and
   locks every affected row, in trace-id order (rows are processed in VALUES order), so batches
   that share several traces cannot deadlock. Every later write for a trace happens under its lock:
   two batches for the same trace are applied one after the other, never interleaved.
3. **Merge (ADR-009).** Span starts, span ends and span events are merged with what is stored. A
   copy whose digest is already stored (or appeared earlier in the batch) is a *duplicate* and
   changes nothing. Otherwise the copy with the lowest digest of all copies ever seen wins, so the
   stored winner does not depend on arrival order. ``*_digests`` keeps the lowest
   :data:`MAX_DIGESTS` distinct digests, which always includes the winner.
4. **Caps.** New spans and span events beyond the project's per-trace limits are dropped and the
   trace is flagged ``truncated``.
5. **Summary.** The trace row is recomputed from all stored spans and events with the same
   assembly code used everywhere else (``aobs_server.model``).

Digests are computed on the stored (post-privacy) form; a span end's ``content`` block is kept in
``span_content`` and re-attached on read, so stored digests can always be recomputed.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import Connection, delete, func, insert, select, tuple_, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..model.assembly import TraceView, assemble
from ..model.canonical import content_digest
from ..model.merge import SpanRecord, merge_events
from ..model.versioning import AcceptedEvent
from ..model.wire import Event, SpanEnd, SpanEvent, SpanStart
from ..project_settings import ProjectSettings
from .tables import (
    environments,
    ingest_diagnostics,
    resources,
    span_content,
    span_events,
    spans,
    trace_tombstones,
    traces,
)

MAX_DIGESTS = 16
# Stored status is computed as if nothing were stale: an unended trace is stored as "open" and
# becomes in_progress or incomplete at read time from stale_reference_time (tables.traces).
_NOTHING_STALE = datetime.min.replace(tzinfo=UTC)
_EVENT: TypeAdapter[SpanStart | SpanEnd | SpanEvent] = TypeAdapter(Event)

Outcome = Literal["accepted", "duplicate", "dropped", "rejected"]


class ConcurrentConflict(Exception):
    """Another transaction stored the same span event id for a different trace at the same moment.
    Retrying the batch resolves it (the API answers 503 with Retry-After)."""


@dataclass(frozen=True)
class EventOutcome:
    outcome: Outcome
    reason: str | None = None  # why it was dropped or rejected
    conflict: bool = False  # accepted, but differs from a stored or earlier copy of the same event


@dataclass
class StoreResult:
    outcomes: dict[int, EventOutcome] = field(default_factory=dict)  # by event index in the batch
    trace_ids: list[str] = field(default_factory=list)  # traces written (not tombstoned)
    counters: Counter[str] = field(default_factory=Counter)


@dataclass
class _TraceState:
    span_count: int
    span_event_count: int
    truncated: bool
    environments: list[str]


# --------------------------------------------------------------------------- helpers


def event_from_json(body: Mapping[str, Any], index: int = 0) -> AcceptedEvent:
    """An event from its stored canonical JSON (``__extra__`` holds newer-minor fields)."""
    data = dict(body)
    extra = data.pop("__extra__", {})
    return AcceptedEvent(index=index, event=_EVENT.validate_python(data), extra=extra)


def _split_content(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    content = body.pop("content", None)
    return body, content


def _choose(
    stored_digests: Sequence[str],
    copies: Sequence[AcceptedEvent],
    outcomes: dict[int, EventOutcome],
) -> tuple[AcceptedEvent | None, list[str], bool]:
    """Merge the batch's copies of one event with the stored copy.

    Returns (winning event if it comes from this batch else None, digests to store, changed).
    Records an outcome for every copy.
    """
    known = set(stored_digests)
    by_digest: dict[str, AcceptedEvent] = {}
    for copy in copies:
        digest = copy.digest()
        if digest in known or digest in by_digest:
            outcomes[copy.index] = EventOutcome("duplicate")
            continue
        outcomes[copy.index] = EventOutcome("accepted", conflict=bool(known or by_digest))
        by_digest[digest] = copy
    if not by_digest:
        return None, list(stored_digests), False
    every = known | set(by_digest)
    digests = sorted(every)[:MAX_DIGESTS]
    from_batch = by_digest.get(min(every))  # None: the stored copy still wins
    return from_batch, digests, from_batch is not None or digests != sorted(stored_digests)


def _hot_fields(record: SpanRecord) -> dict[str, Any]:
    ident = record.identity
    end = record.end_event
    merged = record.merged
    llm = merged.get("llm") or {}
    return {
        "parent_span_id": ident.parent_span_id,
        "kind": ident.kind,
        "name": ident.name,
        "status": None if end is None else end.status,
        "started_at": ident.start_time,
        "ended_at": None if end is None else end.end_time,
        "duration_ms": None if end is None else end.duration_ms,
        "session_id": merged.get("session_id"),
        "agent_name": (merged.get("agent") or {}).get("name"),
        "provider": llm.get("provider"),
        "request_model": llm.get("request_model"),
        "response_model": llm.get("response_model"),
        "tool_name": (merged.get("tool") or {}).get("name"),
    }


# --------------------------------------------------------------------------- write path


def store_events(
    conn: Connection,
    project_id: UUID,
    events: Sequence[AcceptedEvent],
    *,
    resource: Mapping[str, Any],
    environment: str,
    settings: ProjectSettings,
    now: datetime,
) -> StoreResult:
    """Merge prepared (post-privacy) events into storage. Caller owns the transaction."""
    result = StoreResult()
    if not events:
        return result
    trace_ids = sorted({e.event.trace_id for e in events})
    tombstoned = set(
        conn.execute(
            select(trace_tombstones.c.trace_id).where(
                trace_tombstones.c.project_id == project_id, trace_tombstones.c.trace_id.in_(trace_ids)
            )
        ).scalars()
    )
    live: list[AcceptedEvent] = []
    for item in events:
        if item.event.trace_id in tombstoned:
            result.outcomes[item.index] = EventOutcome("dropped", "trace_deleted")
            result.counters["dropped_trace_deleted"] += 1
        else:
            live.append(item)
    result.trace_ids = [t for t in trace_ids if t not in tombstoned]
    if not live:
        return result

    states = _lock_traces(conn, project_id, result.trace_ids, now)
    resource_hash = _store_resource(conn, project_id, resource, environment)
    _store_spans(conn, project_id, [e for e in live if not isinstance(e.event, SpanEvent)], states, resource_hash,
                 settings, now, result)  # fmt: skip
    _store_span_events(conn, project_id, [e for e in live if isinstance(e.event, SpanEvent)], states, settings,
                       now, result)  # fmt: skip
    for trace_id in result.trace_ids:
        states[trace_id].environments = sorted(set(states[trace_id].environments) | {environment})
    result.trace_ids = [t for t in result.trace_ids if refresh_trace(conn, project_id, t, states[t], now)]
    return result


def _lock_traces(conn: Connection, project_id: UUID, trace_ids: list[str], now: datetime) -> dict[str, _TraceState]:
    values = pg_insert(traces).values(
        [
            {"project_id": project_id, "trace_id": t, "status": "open", "first_received_at": now,
             "last_received_at": now}
            for t in trace_ids  # sorted: rows are inserted or locked in this order
        ]
    )  # fmt: skip
    statement = values.on_conflict_do_update(
        index_elements=[traces.c.project_id, traces.c.trace_id],
        set_={"last_received_at": values.excluded.last_received_at},
    ).returning(
        traces.c.trace_id, traces.c.span_count, traces.c.span_event_count, traces.c.truncated, traces.c.environments
    )
    return {
        row.trace_id: _TraceState(row.span_count, row.span_event_count, row.truncated, list(row.environments))
        for row in conn.execute(statement)
    }


def _store_resource(conn: Connection, project_id: UUID, resource: Mapping[str, Any], environment: str) -> str:
    resource_hash = content_digest(resource)
    conn.execute(pg_insert(environments).values(project_id=project_id, name=environment).on_conflict_do_nothing())
    conn.execute(
        pg_insert(resources)
        .values(project_id=project_id, resource_hash=resource_hash, resource=dict(resource))
        .on_conflict_do_nothing()
    )
    return resource_hash


def _group(events: Iterable[AcceptedEvent], key: Any) -> dict[Any, list[AcceptedEvent]]:
    groups: dict[Any, list[AcceptedEvent]] = defaultdict(list)
    for item in sorted(events, key=lambda e: e.index):
        groups[key(item)].append(item)
    return groups  # insertion order = order of first appearance in the batch


def _store_spans(
    conn: Connection,
    project_id: UUID,
    events: list[AcceptedEvent],
    states: dict[str, _TraceState],
    resource_hash: str,
    settings: ProjectSettings,
    now: datetime,
    result: StoreResult,
) -> None:
    if not events:
        return
    groups = _group(events, lambda e: (e.event.trace_id, e.event.span_id))
    stored = {
        (r.trace_id, r.span_id): r
        for r in conn.execute(
            select(
                spans.c.trace_id, spans.c.span_id, spans.c.start_event, spans.c.start_digests,
                spans.c.end_event, spans.c.end_digests,
            ).where(
                spans.c.project_id == project_id,
                tuple_(spans.c.trace_id, spans.c.span_id).in_(list(groups)),
            )
        )
    }  # fmt: skip
    rows: list[dict[str, Any]] = []
    content_rows: list[dict[str, Any]] = []
    content_deletes: list[tuple[str, str]] = []
    for key, copies in groups.items():
        state = states[key[0]]
        old = stored.get(key)
        if old is None:
            if state.span_count >= settings.max_spans_per_trace:
                for copy in copies:
                    result.outcomes[copy.index] = EventOutcome("dropped", "trace_span_limit")
                result.counters["dropped_span_limit"] += len(copies)
                state.truncated = True
                continue
            state.span_count += 1
        starts = [c for c in copies if isinstance(c.event, SpanStart)]
        ends = [c for c in copies if isinstance(c.event, SpanEnd)]
        new_start, start_digests, start_changed = _choose(old.start_digests if old else [], starts, result.outcomes)
        new_end, end_digests, end_changed = _choose(old.end_digests if old else [], ends, result.outcomes)
        if not (start_changed or end_changed):
            continue
        start = new_start or (event_from_json(old.start_event) if old is not None and old.start_event else None)
        end = new_end or (event_from_json(old.end_event) if old is not None and old.end_event else None)
        record = merge_events([e for e in (start, end) if e is not None]).spans[key]
        start_json = None if start is None else start.canonical()
        end_json = None
        if end is not None:
            end_json, content = _split_content(end.canonical())
            if new_end is not None:
                if content is None:
                    content_deletes.append(key)
                else:
                    content_rows.append({"project_id": project_id, "trace_id": key[0], "span_id": key[1],
                                         "content": content, "received_at": now})  # fmt: skip
        rows.append(
            {
                "project_id": project_id, "trace_id": key[0], "span_id": key[1], **_hot_fields(record),
                "start_event": start_json, "start_digests": start_digests,
                "end_event": end_json, "end_digests": end_digests,
                "resource_hash": resource_hash, "first_received_at": now, "last_received_at": now,
            }
        )  # fmt: skip
    if rows:
        statement = pg_insert(spans)
        keep = {"project_id", "trace_id", "span_id", "resource_hash", "first_received_at"}
        conn.execute(
            statement.on_conflict_do_update(
                index_elements=[spans.c.project_id, spans.c.trace_id, spans.c.span_id],
                set_={c.name: statement.excluded[c.name] for c in spans.columns if c.name not in keep},
            ),
            rows,
        )
    if content_rows:
        statement = pg_insert(span_content)
        conn.execute(
            statement.on_conflict_do_update(
                index_elements=[span_content.c.project_id, span_content.c.trace_id, span_content.c.span_id],
                set_={"content": statement.excluded.content, "received_at": statement.excluded.received_at},
            ),
            content_rows,
        )
    if content_deletes:
        conn.execute(
            delete(span_content).where(
                span_content.c.project_id == project_id,
                tuple_(span_content.c.trace_id, span_content.c.span_id).in_(content_deletes),
            )
        )


def _store_span_events(
    conn: Connection,
    project_id: UUID,
    events: list[AcceptedEvent],
    states: dict[str, _TraceState],
    settings: ProjectSettings,
    now: datetime,
    result: StoreResult,
) -> None:
    if not events:
        return
    groups = _group(events, lambda e: e.event.event_id)
    stored = {
        r.event_id: r
        for r in conn.execute(
            select(span_events.c.event_id, span_events.c.trace_id, span_events.c.span_id, span_events.c.event,
                   span_events.c.digests)
            .where(span_events.c.project_id == project_id, span_events.c.event_id.in_(list(groups)))
            .order_by(span_events.c.event_id)
            .with_for_update()
        )
    }  # fmt: skip
    new_rows: list[dict[str, Any]] = []
    changed_rows: list[dict[str, Any]] = []
    for event_id, copies in groups.items():
        old = stored.get(event_id)
        # An event id belongs to the trace and span of its first stored copy (else of its first
        # copy in this batch); a copy naming another trace or span is a client bug and is rejected.
        binding = (old.trace_id, old.span_id) if old else (copies[0].event.trace_id, copies[0].event.span_id)
        matching: list[AcceptedEvent] = []
        for copy in copies:
            if (copy.event.trace_id, copy.event.span_id) == binding:
                matching.append(copy)
            else:
                result.outcomes[copy.index] = EventOutcome("rejected", "event_id_conflict")
                result.counters["rejected_event_id_conflict"] += 1
        if not matching:
            continue
        state = states[binding[0]]
        if old is None:
            if state.span_event_count >= settings.max_span_events_per_trace:
                for copy in matching:
                    result.outcomes[copy.index] = EventOutcome("dropped", "trace_span_event_limit")
                result.counters["dropped_span_event_limit"] += len(matching)
                state.truncated = True
                continue
            state.span_event_count += 1
        winner, digests, changed = _choose(old.digests if old else [], matching, result.outcomes)
        if not changed:
            continue
        if winner is not None:
            chosen = winner
        else:
            assert old is not None  # a new event always has a winner from the batch
            chosen = event_from_json(old.event)
        ev = chosen.event
        assert isinstance(ev, SpanEvent)
        row = {
            "project_id": project_id, "event_id": event_id, "trace_id": ev.trace_id, "span_id": ev.span_id,
            "name": ev.name, "time": ev.time, "event": chosen.canonical(), "digests": digests,
            "first_received_at": now, "last_received_at": now,
        }  # fmt: skip
        (changed_rows if old else new_rows).append(row)
    if new_rows:
        inserted = set(
            conn.execute(
                pg_insert(span_events)
                .values(sorted(new_rows, key=lambda r: r["event_id"]))
                .on_conflict_do_nothing()
                .returning(span_events.c.event_id)
            ).scalars()
        )
        if len(inserted) != len(new_rows):
            raise ConcurrentConflict()
    for row in changed_rows:
        conn.execute(
            update(span_events)
            .where(span_events.c.project_id == project_id, span_events.c.event_id == row["event_id"])
            .values(event=row["event"], digests=row["digests"], name=row["name"], time=row["time"],
                    last_received_at=now)
        )  # fmt: skip


# --------------------------------------------------------------------------- trace summary


def _stored_trace(conn: Connection, project_id: UUID, trace_id: str) -> tuple[list[AcceptedEvent], int]:
    """(stored events without content, conflicts counted from the stored digests)."""
    events: list[AcceptedEvent] = []
    conflicts = 0
    for row in conn.execute(
        select(spans.c.start_event, spans.c.end_event, spans.c.start_digests, spans.c.end_digests).where(
            spans.c.project_id == project_id, spans.c.trace_id == trace_id
        )
    ):
        for body in (row.start_event, row.end_event):
            if body is not None:
                events.append(event_from_json(body))
        conflicts += max(len(row.start_digests) - 1, 0) + max(len(row.end_digests) - 1, 0)
    for event_row in conn.execute(
        select(span_events.c.event, span_events.c.digests).where(
            span_events.c.project_id == project_id, span_events.c.trace_id == trace_id
        )
    ):
        events.append(event_from_json(event_row.event))
        conflicts += max(len(event_row.digests) - 1, 0)
    return events, conflicts


def summary_of(view: TraceView | None, conflicts: int, span_event_count: int) -> dict[str, Any]:
    """The ``traces`` columns derived from an assembled trace (assembled with nothing stale)."""
    if view is None or not view.spans:  # only span events so far: incomplete, as in assembly
        return {
            "status": "incomplete",
            "stale_reference_time": None, "started_at": None, "ended_at": None,
            "root_name": None, "agent_name": None, "session_id": None,
            "span_count": 0, "open_span_count": 0, "error_count": 0, "span_event_count": span_event_count,
            "kind_counts": {}, "tokens": {} if view is None else {k: v.to_dict() for k, v in view.tokens.items()},
            "conflicts": conflicts, "orphan_count": 0,
        }  # fmt: skip
    stale_reference: datetime | None = None
    status: str = view.status
    if status == "in_progress":
        status = "open"
        unended_roots = [view.spans[r].start_time for r in view.roots if view.spans[r].lifecycle != "complete"]
        stale_reference = (
            min(unended_roots) if view.roots else max((n.end_time or n.start_time) for n in view.spans.values())
        )
    root = view.spans[view.roots[0]] if view.roots else None
    root_fields = {} if root is None else dict(root.record.merged)
    return {
        "status": status,
        "stale_reference_time": stale_reference,
        "started_at": view.started_at,
        "ended_at": view.ended_at,
        "root_name": None if root is None else root.name,
        "agent_name": (root_fields.get("agent") or {}).get("name"),
        "session_id": root_fields.get("session_id"),
        "span_count": view.span_count,
        "open_span_count": view.open_span_count,
        "error_count": view.error_count,
        "span_event_count": span_event_count,
        "kind_counts": dict(sorted(view.kind_counts.items())),
        "tokens": {k: v.to_dict() for k, v in view.tokens.items()},
        "conflicts": conflicts,
        "orphan_count": len(view.orphans),
    }


def refresh_trace(conn: Connection, project_id: UUID, trace_id: str, state: _TraceState, now: datetime) -> bool:
    """Recompute a (locked) trace row from its stored spans and events. An empty trace (every event
    of it was rejected) is deleted; returns whether the trace exists."""
    events, conflicts = _stored_trace(conn, project_id, trace_id)
    where = (traces.c.project_id == project_id, traces.c.trace_id == trace_id)
    if not events:
        conn.execute(delete(traces).where(*where))
        return False
    span_event_count = sum(1 for e in events if isinstance(e.event, SpanEvent))
    view = assemble(merge_events(events), _NOTHING_STALE).get(trace_id)
    conn.execute(
        update(traces)
        .where(*where)
        .values(
            **summary_of(view, conflicts, span_event_count),
            truncated=state.truncated,
            environments=state.environments,
            last_received_at=now,
        )
    )
    return True


# --------------------------------------------------------------------------- diagnostics and reads


def record_diagnostics(
    conn: Connection, project_id: UUID, api_key_id: UUID | None, batch_id: str | None, counters: Mapping[str, int],
    now: datetime,
) -> None:  # fmt: skip
    nonzero = {k: v for k, v in sorted(counters.items()) if v}
    if nonzero:
        conn.execute(
            insert(ingest_diagnostics).values(
                project_id=project_id, api_key_id=api_key_id, received_at=now, batch_id=batch_id, counters=nonzero
            )
        )


def load_trace_events(
    conn: Connection,
    project_id: UUID,
    trace_id: str,
    *,
    include_span_events: bool = True,
    include_content: bool = True,
) -> list[AcceptedEvent]:
    """The stored (post-privacy) events of one trace in this project, content re-attached unless
    ``include_content`` is false (then ``span_content`` is not read at all). Another project's
    trace with the same id is never returned."""
    out: list[AcceptedEvent] = []
    if include_content:
        query = select(spans.c.span_id, spans.c.start_event, spans.c.end_event, span_content.c.content).select_from(
            spans.outerjoin(
                span_content,
                (span_content.c.project_id == spans.c.project_id)
                & (span_content.c.trace_id == spans.c.trace_id)
                & (span_content.c.span_id == spans.c.span_id),
            )
        )
    else:
        query = select(spans.c.span_id, spans.c.start_event, spans.c.end_event)
    rows = conn.execute(
        query.where(spans.c.project_id == project_id, spans.c.trace_id == trace_id).order_by(spans.c.span_id)
    )
    for row in rows:
        if row.start_event is not None:
            out.append(event_from_json(row.start_event, len(out)))
        if row.end_event is not None:
            body = dict(row.end_event)
            if include_content and row.content is not None:
                body["content"] = row.content
            out.append(event_from_json(body, len(out)))
    if not include_span_events:
        return out
    for row in conn.execute(
        select(span_events.c.event)
        .where(span_events.c.project_id == project_id, span_events.c.trace_id == trace_id)
        .order_by(span_events.c.event_id)
    ):
        out.append(event_from_json(row.event, len(out)))
    return out


def trace_row(conn: Connection, project_id: UUID, trace_id: str) -> Any | None:
    """The summary row of a trace in this project, or None (also for a deleted trace)."""
    tombstoned = (
        select(trace_tombstones.c.trace_id)
        .where(trace_tombstones.c.project_id == project_id, trace_tombstones.c.trace_id == trace_id)
        .exists()
    )
    return conn.execute(
        select(traces).where(traces.c.project_id == project_id, traces.c.trace_id == trace_id, ~tombstoned)
    ).first()


def select_traces(
    conn: Connection,
    project_id: UUID,
    *,
    limit: int,
    trace_ids: Sequence[str] = (),
    root_name: str | None = None,
    agent_name: str | None = None,
    session_id: str | None = None,
    environment: str | None = None,
    status: str | None = None,
    started_since: datetime | None = None,
    started_before: datetime | None = None,
) -> list[Any]:
    """Summary rows of this project's traces that match every given filter, most recently started
    first (traces without a start time last). Deleted (tombstoned) traces are never returned."""
    tombstoned = (
        select(trace_tombstones.c.trace_id)
        .where(trace_tombstones.c.project_id == project_id, trace_tombstones.c.trace_id == traces.c.trace_id)
        .exists()
    )
    query = select(traces).where(traces.c.project_id == project_id, ~tombstoned)
    if trace_ids:
        query = query.where(traces.c.trace_id.in_(list(trace_ids)))
    if root_name is not None:
        query = query.where(traces.c.root_name == root_name)
    if agent_name is not None:
        query = query.where(traces.c.agent_name == agent_name)
    if session_id is not None:
        query = query.where(traces.c.session_id == session_id)
    if environment is not None:
        query = query.where(traces.c.environments.any_() == environment)
    if status is not None:
        query = query.where(traces.c.status == status)
    if started_since is not None:
        query = query.where(traces.c.started_at >= started_since)
    if started_before is not None:
        query = query.where(traces.c.started_at < started_before)
    query = query.order_by(traces.c.started_at.desc().nulls_last(), traces.c.trace_id).limit(limit)
    return list(conn.execute(query))


def trace_resources(conn: Connection, project_id: UUID, trace_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """The resource (service, SDK, environment) that sent each trace's root span, or else the
    trace's first span by id, for traces of this project."""
    if not trace_ids:
        return {}
    rows = conn.execute(
        select(spans.c.trace_id, resources.c.resource)
        .select_from(
            spans.join(
                resources,
                (resources.c.project_id == spans.c.project_id) & (resources.c.resource_hash == spans.c.resource_hash),
            )
        )
        .where(spans.c.project_id == project_id, spans.c.trace_id.in_(list(trace_ids)))
        .order_by(spans.c.trace_id, spans.c.parent_span_id.is_not(None), spans.c.span_id)
        .ext(distinct_on(spans.c.trace_id))
    )
    return {row.trace_id: dict(row.resource) for row in rows}


def span_event_counts(conn: Connection, project_id: UUID, trace_id: str) -> tuple[dict[str, int], int]:
    """(span events per span id, span events whose span is not stored) for one trace."""
    counts = {
        row.span_id: row.n
        for row in conn.execute(
            select(span_events.c.span_id, func.count().label("n"))
            .where(span_events.c.project_id == project_id, span_events.c.trace_id == trace_id)
            .group_by(span_events.c.span_id)
        )
    }
    stored = set(
        conn.execute(
            select(spans.c.span_id).where(spans.c.project_id == project_id, spans.c.trace_id == trace_id)
        ).scalars()
    )
    return {k: v for k, v in counts.items() if k in stored}, sum(v for k, v in counts.items() if k not in stored)


def list_span_events(
    conn: Connection,
    project_id: UUID,
    trace_id: str,
    *,
    span_id: str | None,
    after: tuple[datetime, str] | None,
    limit: int,
) -> list[Any]:
    """Span events of one trace in (time, event_id) order, keyset-paginated after ``after``."""
    query = select(span_events.c.event_id, span_events.c.time, span_events.c.event).where(
        span_events.c.project_id == project_id, span_events.c.trace_id == trace_id
    )
    if span_id is not None:
        query = query.where(span_events.c.span_id == span_id)
    if after is not None:
        query = query.where(tuple_(span_events.c.time, span_events.c.event_id) > tuple_(*after))
    return list(conn.execute(query.order_by(span_events.c.time, span_events.c.event_id).limit(limit)))
