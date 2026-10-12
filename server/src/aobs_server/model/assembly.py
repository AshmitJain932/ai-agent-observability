"""Trace assembly: merged span records -> trees with honest derived state.

Pure function of (merged events, ``now``, config). Never invents relationships:

* A span whose parent id is not present is an **orphan** (``missing_parent``) and is listed
  separately, not attached to any root.
* Spans whose parent pointers form a cycle (including a span that names itself as parent) are
  orphans with reason ``cycle``; their parent edges are ignored so traversal always terminates.
* Unended spans are ``in_progress`` while younger than ``stale_after``, then ``incomplete``.
* Token totals report how many spans contributed and never treat a missing count as zero.

Trace status (event-model §9), evaluated over all roots:

1. no roots: ``in_progress`` if the newest timestamp is within ``stale_after``, else ``incomplete``
2. some root unended: ``in_progress`` if every unended root is in progress, else ``incomplete``
3. any root ended ``error`` -> ``error``; else any root ``cancelled`` -> ``cancelled``
4. any span anywhere ended ``error`` -> ``ok_with_errors``; else ``ok``
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from .merge import MergeResult, SpanRecord, merge_events
from .versioning import AcceptedEvent
from .wire import TOKEN_FIELDS, SpanEvent

Lifecycle = Literal["complete", "in_progress", "incomplete"]
TraceStatus = Literal["ok", "ok_with_errors", "error", "cancelled", "in_progress", "incomplete"]
OrphanReason = Literal["missing_parent", "cycle"]

TOKEN_KINDS = ("llm", "embedding")  # usage on other kinds is shown per span but not summed


@dataclass(frozen=True)
class AssemblyConfig:
    stale_after: timedelta = timedelta(minutes=15)
    skew_tolerance: timedelta = timedelta(milliseconds=50)


@dataclass(frozen=True)
class SpanNode:
    span_id: str
    parent_span_id: str | None  # as reported; see orphan_reason for whether it was usable
    kind: str
    name: str
    start_time: datetime
    end_time: datetime | None
    duration_ms: float | None
    status: str | None
    lifecycle: Lifecycle
    children: tuple[str, ...]
    orphan_reason: OrphanReason | None
    flags: tuple[str, ...]
    record: SpanRecord = field(repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "kind": self.kind,
            "name": self.name,
            "start_time": self.start_time.isoformat(),
            "end_time": None if self.end_time is None else self.end_time.isoformat(),
            "duration_ms": self.duration_ms,
            "status": self.status,
            "lifecycle": self.lifecycle,
            "children": list(self.children),
            "orphan_reason": self.orphan_reason,
            "flags": list(self.flags),
            "fields": dict(self.record.merged),
        }


@dataclass(frozen=True)
class TokenTotal:
    value: int | None  # None when no span reported this count — never 0 by default
    reported_spans: int
    expected_spans: int
    estimated: bool  # at least one contributing span used an SDK estimate

    @property
    def complete(self) -> bool:
        return self.expected_spans > 0 and self.reported_spans == self.expected_spans

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "reported_spans": self.reported_spans,
            "expected_spans": self.expected_spans,
            "estimated": self.estimated,
            "complete": self.complete,
        }


@dataclass(frozen=True)
class TraceView:
    trace_id: str
    status: TraceStatus
    roots: tuple[str, ...]
    orphans: tuple[tuple[str, OrphanReason], ...]
    spans: Mapping[str, SpanNode]
    events_by_span: Mapping[str, tuple[SpanEvent, ...]]
    orphan_events: tuple[str, ...]  # event_ids whose span is not present
    error_count: int
    kind_counts: Mapping[str, int]
    tokens: Mapping[str, TokenTotal]
    conflicts: int
    started_at: datetime | None
    ended_at: datetime | None

    @property
    def span_count(self) -> int:
        return len(self.spans)

    @property
    def open_span_count(self) -> int:
        """Spans without an end. Non-zero on an 'ok' trace means instrumentation gaps or
        detached work — shown to users rather than folded into the status."""
        return sum(1 for n in self.spans.values() if n.lifecycle != "complete")

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, JSON-compatible representation (used for equality in tests)."""
        return {
            "trace_id": self.trace_id,
            "status": self.status,
            "roots": list(self.roots),
            "orphans": [{"span_id": s, "reason": r} for s, r in self.orphans],
            "spans": {sid: self.spans[sid].to_dict() for sid in sorted(self.spans)},
            "events_by_span": {
                sid: [e.model_dump(mode="json", exclude_unset=True) for e in evs]
                for sid, evs in sorted(self.events_by_span.items())
            },
            "orphan_events": list(self.orphan_events),
            "span_count": self.span_count,
            "open_span_count": self.open_span_count,
            "error_count": self.error_count,
            "kind_counts": dict(sorted(self.kind_counts.items())),
            "tokens": {k: v.to_dict() for k, v in self.tokens.items()},
            "conflicts": self.conflicts,
            "started_at": None if self.started_at is None else self.started_at.isoformat(),
            "ended_at": None if self.ended_at is None else self.ended_at.isoformat(),
        }


def assemble_events(
    events: Iterable[AcceptedEvent], now: datetime, config: AssemblyConfig | None = None
) -> dict[str, TraceView]:
    return assemble(merge_events(events), now, config)


def assemble(merged: MergeResult, now: datetime, config: AssemblyConfig | None = None) -> dict[str, TraceView]:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    cfg = config or AssemblyConfig()

    spans_by_trace: dict[str, dict[str, SpanRecord]] = defaultdict(dict)
    for (trace_id, span_id), record in merged.spans.items():
        spans_by_trace[trace_id][span_id] = record
    events_by_trace: dict[str, list[SpanEvent]] = defaultdict(list)
    for rec in merged.span_events.values():
        events_by_trace[rec.event.trace_id].append(rec.event)
    event_conflicts: dict[str, int] = defaultdict(int)
    for rec in merged.span_events.values():
        event_conflicts[rec.event.trace_id] += rec.conflicts

    return {
        trace_id: _assemble_trace(
            trace_id,
            spans_by_trace.get(trace_id, {}),
            events_by_trace.get(trace_id, []),
            event_conflicts.get(trace_id, 0),
            now,
            cfg,
        )
        for trace_id in sorted(set(spans_by_trace) | set(events_by_trace))
    }


def _find_cycle_members(parents: Mapping[str, str | None]) -> set[str]:
    in_cycle: set[str] = set()
    state: dict[str, int] = {}  # 1 = on the current walk, 2 = finished
    for start in sorted(parents):
        path: list[str] = []
        cur: str | None = start
        while cur is not None and cur in parents and state.get(cur, 0) == 0:
            state[cur] = 1
            path.append(cur)
            cur = parents[cur]
        if cur is not None and state.get(cur) == 1:
            in_cycle.update(path[path.index(cur) :])
        for node in path:
            state[node] = 2
    return in_cycle


def _assemble_trace(
    trace_id: str,
    records: Mapping[str, SpanRecord],
    span_events: list[SpanEvent],
    event_conflicts: int,
    now: datetime,
    cfg: AssemblyConfig,
) -> TraceView:
    parents = {sid: rec.identity.parent_span_id for sid, rec in records.items()}
    in_cycle = _find_cycle_members(parents)

    effective_parent: dict[str, str | None] = {}
    orphan_reason: dict[str, OrphanReason] = {}
    for sid, parent in parents.items():
        if parent is None:
            effective_parent[sid] = None
        elif sid in in_cycle:
            effective_parent[sid] = None
            orphan_reason[sid] = "cycle"
        elif parent not in records:
            effective_parent[sid] = None
            orphan_reason[sid] = "missing_parent"
        else:
            effective_parent[sid] = parent

    def order_key(sid: str) -> tuple[datetime, str]:
        return (records[sid].identity.start_time, sid)

    children: dict[str, list[str]] = defaultdict(list)
    for sid, parent in effective_parent.items():
        if parent is not None:
            children[parent].append(sid)

    def lifecycle_of(rec: SpanRecord) -> Lifecycle:
        if rec.has_end:
            return "complete"
        return "in_progress" if now - rec.identity.start_time < cfg.stale_after else "incomplete"

    nodes: dict[str, SpanNode] = {}
    for sid, rec in records.items():
        ident = rec.identity
        end = rec.end_event
        flags: list[str] = []
        if rec.mismatches:
            flags.append("start_end_mismatch")
        if rec.conflicts:
            flags.append("conflicting_duplicates")
        parent_id = effective_parent[sid]
        if parent_id is not None:
            parent_rec = records[parent_id]
            p_end = parent_rec.end_event
            if ident.start_time < parent_rec.identity.start_time - cfg.skew_tolerance or (
                end is not None and p_end is not None and end.end_time > p_end.end_time + cfg.skew_tolerance
            ):
                flags.append("clock_skew_suspected")
        nodes[sid] = SpanNode(
            span_id=sid,
            parent_span_id=ident.parent_span_id,
            kind=ident.kind,
            name=ident.name,
            start_time=ident.start_time,
            end_time=None if end is None else end.end_time,
            duration_ms=None if end is None else end.duration_ms,
            status=None if end is None else end.status,
            lifecycle=lifecycle_of(rec),
            children=tuple(sorted(children.get(sid, []), key=order_key)),
            orphan_reason=orphan_reason.get(sid),
            flags=tuple(sorted(flags)),
            record=rec,
        )

    roots = tuple(sorted((sid for sid, p in parents.items() if p is None), key=order_key))
    orphans = tuple((sid, orphan_reason[sid]) for sid in sorted(orphan_reason, key=order_key))

    status = _trace_status(nodes, roots, now, cfg)

    events_by_span: dict[str, list[SpanEvent]] = defaultdict(list)
    orphan_events: list[str] = []
    for ev in sorted(span_events, key=lambda e: (e.time, e.event_id)):
        if ev.span_id in records:
            events_by_span[ev.span_id].append(ev)
        else:
            orphan_events.append(ev.event_id)

    kind_counts: dict[str, int] = defaultdict(int)
    for node in nodes.values():
        kind_counts[node.kind] += 1

    starts = [n.start_time for n in nodes.values()]
    root_ends = [nodes[r].end_time for r in roots]
    ended_at = (
        max(e for e in root_ends if e is not None)
        if roots and status not in ("in_progress", "incomplete") and all(e is not None for e in root_ends)
        else None
    )

    return TraceView(
        trace_id=trace_id,
        status=status,
        roots=roots,
        orphans=orphans,
        spans=nodes,
        events_by_span={k: tuple(v) for k, v in events_by_span.items()},
        orphan_events=tuple(orphan_events),
        error_count=sum(1 for n in nodes.values() if n.status == "error"),
        kind_counts=dict(kind_counts),
        tokens=_token_totals(records.values()),
        conflicts=sum(r.conflicts for r in records.values()) + event_conflicts,
        started_at=min(starts) if starts else None,
        ended_at=ended_at,
    )


def _trace_status(
    nodes: Mapping[str, SpanNode], roots: tuple[str, ...], now: datetime, cfg: AssemblyConfig
) -> TraceStatus:
    if not roots:
        if not nodes:
            return "incomplete"
        newest = max((n.end_time or n.start_time) for n in nodes.values())
        return "in_progress" if now - newest < cfg.stale_after else "incomplete"
    unended = [nodes[r] for r in roots if nodes[r].lifecycle != "complete"]
    if unended:
        return "in_progress" if all(n.lifecycle == "in_progress" for n in unended) else "incomplete"
    root_statuses = {nodes[r].status for r in roots}
    if "error" in root_statuses:
        return "error"
    if "cancelled" in root_statuses:
        return "cancelled"
    if any(n.status == "error" for n in nodes.values()):
        return "ok_with_errors"
    return "ok"


def _token_totals(records: Iterable[SpanRecord]) -> dict[str, TokenTotal]:
    relevant = [r for r in records if r.identity.kind in TOKEN_KINDS]
    totals: dict[str, TokenTotal] = {}
    for name in TOKEN_FIELDS:
        values: list[int] = []
        estimated = False
        for rec in relevant:
            end = rec.end_event
            usage = None if end is None else end.usage
            count = None if usage is None else getattr(usage, name)
            if count is not None:
                values.append(count)
                estimated = estimated or (usage is not None and usage.source == "sdk_estimated")
        totals[name] = TokenTotal(
            value=sum(values) if values else None,
            reported_spans=len(values),
            expected_spans=len(relevant),
            estimated=estimated,
        )
    return totals
