"""Batch validation and the schema version policy (ADR-010).

* Unknown **major** version: the whole batch is rejected.
* Payload **minor ≤ the server's known minor**: strict. Unknown fields are errors (catches typos).
* Payload **minor > known minor**: lenient. Unknown fields are removed from the event, kept in
  ``extra`` keyed by their path, and reported; everything else is validated strictly.

The envelope is validated first. If it is invalid the batch is rejected; otherwise each event is
validated independently so one bad event does not drop the others.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import TypeAdapter, ValidationError

from .canonical import canonical_json, content_digest
from .parsing import StrictJSONError, loads_strict
from .wire import EVENT_TYPES, Envelope, Event, SpanEnd, SpanEvent, SpanStart

SUPPORTED_MAJOR = 1
KNOWN_MINOR = 0  # must equal the minor in schema/v1/SCHEMA_LOCK (enforced by a test)
MAX_EXTRA_BYTES = 4096  # per event; preserved unknown fields beyond this are dropped and reported

_EVENT_ADAPTER: TypeAdapter[SpanStart | SpanEnd | SpanEvent] = TypeAdapter(Event)


@dataclass(frozen=True)
class FieldError:
    path: str  # slash-separated, relative to the batch root, e.g. "events/3/usage/input_tokens"
    type: str
    message: str


@dataclass(frozen=True)
class AcceptedEvent:
    index: int
    event: SpanStart | SpanEnd | SpanEvent
    extra: Mapping[str, Any] = field(default_factory=dict)  # path (relative to event) -> value

    def canonical(self) -> dict[str, Any]:
        """JSON-compatible representation used for duplicate detection and conflict ordering.

        Unset optional fields are omitted, so "absent" and "present" never compare equal.
        """
        body: dict[str, Any] = self.event.model_dump(mode="json", exclude_unset=True)
        if self.extra:
            body["__extra__"] = dict(self.extra)
        return body

    def digest(self) -> str:
        return content_digest(self.canonical())


@dataclass(frozen=True)
class RejectedEvent:
    index: int
    errors: tuple[FieldError, ...]


@dataclass(frozen=True)
class BatchResult:
    """Outcome of validating one ingest batch. ``envelope`` is None iff ``batch_errors`` is set."""

    envelope: Envelope | None
    accepted: tuple[AcceptedEvent, ...] = ()
    rejected: tuple[RejectedEvent, ...] = ()
    batch_errors: tuple[FieldError, ...] = ()
    lenient: bool = False  # True when the payload's minor is newer than this server's
    envelope_extra: Mapping[str, Any] = field(default_factory=dict)  # unknown envelope fields (lenient)
    extra_dropped: tuple[int, ...] = ()  # event indexes whose unknown fields exceeded MAX_EXTRA_BYTES

    @property
    def ok(self) -> bool:
        return not self.batch_errors


class UnsupportedVersion(ValueError):
    pass


def parse_version(text: str) -> tuple[int, int]:
    major_s, _, minor_s = text.partition(".")
    if not (major_s.isdigit() and minor_s.isdigit()):
        raise UnsupportedVersion(f"malformed schema_version {text!r}")
    return int(major_s), int(minor_s)


def _join(prefix: Sequence[str | int], loc: Sequence[str | int]) -> str:
    return "/".join(str(part) for part in (*prefix, *loc))


def _event_loc(loc: Sequence[str | int]) -> tuple[str | int, ...]:
    # Discriminated unions prefix the location with the tag ("span_end", ...); drop it.
    if loc and loc[0] in EVENT_TYPES:
        return tuple(loc[1:])
    return tuple(loc)


def _errors(exc: ValidationError, prefix: Sequence[str | int], event: bool) -> tuple[FieldError, ...]:
    out = []
    for err in exc.errors(include_url=False):
        loc = _event_loc(err["loc"]) if event else tuple(err["loc"])
        out.append(FieldError(path=_join(prefix, loc), type=err["type"], message=err["msg"]))
    return tuple(out)


_MISSING = object()


def _child(container: Any, part: str | int) -> Any:
    if isinstance(container, dict) and isinstance(part, str):
        return container.get(part, _MISSING)
    if isinstance(container, list) and isinstance(part, int) and 0 <= part < len(container):
        return container[part]
    return _MISSING


def _pop_path(obj: Any, loc: Sequence[str | int]) -> tuple[bool, Any]:
    """Remove the item at ``loc`` from nested dicts/lists. Returns (found, value)."""
    target = obj
    for part in loc[:-1]:
        target = _child(target, part)
        if target is _MISSING:
            return False, None
    last = loc[-1]
    if isinstance(target, dict) and isinstance(last, str) and last in target:
        return True, target.pop(last)
    return False, None


def _strip_unknown(raw: Any, adapter: Any, event: bool) -> tuple[Any, dict[str, Any]]:
    """Repeatedly remove fields reported as ``extra_forbidden``; return (cleaned, extras)."""
    cleaned = copy.deepcopy(raw)
    extras: dict[str, Any] = {}
    for _ in range(16):  # bounded: each round removes at least one field
        try:
            adapter.validate_python(cleaned)
            break
        except ValidationError as exc:
            removed = False
            for err in exc.errors(include_url=False):
                if err["type"] != "extra_forbidden":
                    continue
                loc = _event_loc(err["loc"]) if event else tuple(err["loc"])
                found, value = _pop_path(cleaned, loc)
                if found:
                    extras["/".join(str(p) for p in loc)] = value
                    removed = True
            if not removed:
                break
    return cleaned, extras


_ENVELOPE_ADAPTER: TypeAdapter[Envelope] = TypeAdapter(Envelope)


def validate_batch(payload: bytes | str | Mapping[str, Any]) -> BatchResult:
    """Validate a raw batch (bytes/str are parsed strictly; a mapping is assumed already parsed)."""
    if isinstance(payload, (bytes, str)):
        try:
            data = loads_strict(payload)
        except StrictJSONError as exc:
            return BatchResult(envelope=None, batch_errors=(FieldError("", "invalid_json", str(exc)),))
    else:
        data = payload

    if not isinstance(data, dict):
        return BatchResult(envelope=None, batch_errors=(FieldError("", "dict_type", "batch must be a JSON object"),))

    version = data.get("schema_version")
    lenient = False
    if isinstance(version, str):
        try:
            major, minor = parse_version(version)
        except UnsupportedVersion:
            major, minor = -1, -1  # the pattern check below reports it
        if major >= 0 and major != SUPPORTED_MAJOR:
            return BatchResult(
                envelope=None,
                batch_errors=(
                    FieldError(
                        "schema_version",
                        "unsupported_schema_version",
                        f"major version {major} is not supported (supported: {SUPPORTED_MAJOR})",
                    ),
                ),
            )
        lenient = minor > KNOWN_MINOR

    envelope_data: Any = data
    envelope_extra: dict[str, Any] = {}
    if lenient:
        # Strip unknown envelope/resource fields without touching events (validated separately).
        events_raw = data.get("events")
        envelope_data, envelope_extra = _strip_unknown(
            {k: v for k, v in data.items() if k != "events"} | {"events": [None]},
            _ENVELOPE_ADAPTER,
            event=False,
        )
        if "events" in data:
            envelope_data["events"] = events_raw
        else:
            del envelope_data["events"]
        if len(canonical_json(envelope_extra).encode("utf-8")) > MAX_EXTRA_BYTES:
            envelope_extra = {}
    try:
        envelope = _ENVELOPE_ADAPTER.validate_python(envelope_data)
    except ValidationError as exc:
        return BatchResult(envelope=None, batch_errors=_errors(exc, (), event=False), lenient=lenient)

    accepted: list[AcceptedEvent] = []
    rejected: list[RejectedEvent] = []
    extra_dropped: list[int] = []
    for index, raw in enumerate(envelope.events):
        prefix = ("events", index)
        extras: dict[str, Any] = {}
        candidate = raw
        if lenient:
            candidate, extras = _strip_unknown(raw, _EVENT_ADAPTER, event=True)
            if extras and len(canonical_json(extras).encode("utf-8")) > MAX_EXTRA_BYTES:
                extras = {}
                extra_dropped.append(index)
        try:
            event = _EVENT_ADAPTER.validate_python(candidate)
        except ValidationError as exc:
            rejected.append(RejectedEvent(index, _errors(exc, prefix, event=True)))
            continue
        accepted.append(AcceptedEvent(index, event, extras))

    return BatchResult(
        envelope=envelope,
        accepted=tuple(accepted),
        rejected=tuple(rejected),
        lenient=lenient,
        envelope_extra=envelope_extra,
        extra_dropped=tuple(extra_dropped),
    )
