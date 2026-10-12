"""Deterministic merging of span events (ADR-009).

Rules:

* Events are grouped by natural key: ``(trace_id, span_id, type)`` for span starts/ends and
  ``event_id`` for span events.
* Identical copies (same canonical content) are duplicates: counted, otherwise ignored.
* Differing copies are conflicts: the copy with the lowest SHA-256 of its canonical JSON wins and
  the others are counted. The winner therefore does not depend on arrival order.
* A span's start and end are deep-merged: objects merge key by key, the end wins on any leaf
  conflict, arrays are replaced. Identity fields that differ between start and end are reported
  in ``mismatches`` (the end's value is used).

The result for a multiset of events is the same for every ordering of that multiset — this is
verified by property tests.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .versioning import AcceptedEvent
from .wire import SpanEnd, SpanEvent, SpanStart

IDENTITY_FIELDS = ("parent_span_id", "kind", "name", "start_time")


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = dict(base)
    for key, value in override.items():
        current = out.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            out[key] = deep_merge(current, value)
        else:
            out[key] = value
    return out


@dataclass(frozen=True)
class SpanRecord:
    trace_id: str
    span_id: str
    start: AcceptedEvent | None
    end: AcceptedEvent | None
    conflicts: int
    mismatches: tuple[str, ...]
    merged: Mapping[str, Any]  # JSON-compatible merged fields, without "type"

    @property
    def start_event(self) -> SpanStart | None:
        return None if self.start is None else _as(self.start.event, SpanStart)

    @property
    def end_event(self) -> SpanEnd | None:
        return None if self.end is None else _as(self.end.event, SpanEnd)

    @property
    def identity(self) -> SpanStart | SpanEnd:
        """The event whose identity fields are authoritative: the end if present, else the start."""
        chosen = self.end_event or self.start_event
        assert chosen is not None  # a record always has at least one of the two
        return chosen

    @property
    def has_start(self) -> bool:
        return self.start is not None

    @property
    def has_end(self) -> bool:
        return self.end is not None


@dataclass(frozen=True)
class SpanEventRecord:
    event: SpanEvent
    conflicts: int


@dataclass(frozen=True)
class MergeResult:
    spans: Mapping[tuple[str, str], SpanRecord]
    span_events: Mapping[str, SpanEventRecord]
    duplicates: int
    conflicts: int


def _as(event: object, cls: type[Any]) -> Any:
    if not isinstance(event, cls):  # pragma: no cover - guarded by grouping
        raise TypeError(f"expected {cls.__name__}, got {type(event).__name__}")
    return event


def _pick(group: list[AcceptedEvent]) -> tuple[AcceptedEvent, int, int]:
    """Return (winner, duplicates, conflicts) for copies sharing one natural key."""
    by_digest: dict[str, AcceptedEvent] = {}
    for item in group:
        by_digest.setdefault(item.digest(), item)
    winner_digest = min(by_digest)
    distinct = len(by_digest)
    return by_digest[winner_digest], len(group) - distinct, distinct - 1


def merge_events(events: Iterable[AcceptedEvent]) -> MergeResult:
    starts: dict[tuple[str, str], list[AcceptedEvent]] = defaultdict(list)
    ends: dict[tuple[str, str], list[AcceptedEvent]] = defaultdict(list)
    point_events: dict[str, list[AcceptedEvent]] = defaultdict(list)

    for accepted in events:
        ev = accepted.event
        if isinstance(ev, SpanEvent):
            point_events[ev.event_id].append(accepted)
        elif isinstance(ev, SpanEnd):
            ends[(ev.trace_id, ev.span_id)].append(accepted)
        else:
            starts[(ev.trace_id, ev.span_id)].append(accepted)

    duplicates = 0
    conflicts = 0
    spans: dict[tuple[str, str], SpanRecord] = {}
    for key in sorted(set(starts) | set(ends)):
        start = end = None
        span_conflicts = 0
        if key in starts:
            start, dup, con = _pick(starts[key])
            duplicates += dup
            span_conflicts += con
        if key in ends:
            end, dup, con = _pick(ends[key])
            duplicates += dup
            span_conflicts += con
        conflicts += span_conflicts

        start_fields = _fields(start)
        end_fields = _fields(end)
        mismatches = tuple(
            name
            for name in IDENTITY_FIELDS
            if name in start_fields and name in end_fields and start_fields[name] != end_fields[name]
        )
        spans[key] = SpanRecord(
            trace_id=key[0],
            span_id=key[1],
            start=start,
            end=end,
            conflicts=span_conflicts,
            mismatches=mismatches,
            merged=deep_merge(start_fields, end_fields),
        )

    span_events: dict[str, SpanEventRecord] = {}
    for event_id in sorted(point_events):
        winner, dup, con = _pick(point_events[event_id])
        duplicates += dup
        conflicts += con
        span_events[event_id] = SpanEventRecord(event=_as(winner.event, SpanEvent), conflicts=con)

    return MergeResult(spans=spans, span_events=span_events, duplicates=duplicates, conflicts=conflicts)


def _fields(accepted: AcceptedEvent | None) -> dict[str, Any]:
    if accepted is None:
        return {}
    body = accepted.canonical()
    body.pop("type", None)
    return body
