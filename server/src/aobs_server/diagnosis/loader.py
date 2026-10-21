"""Load saved telemetry (SDK JSONL files or JSON batch files) into privacy-safe events.

Every batch goes through the same steps the collector uses: strict JSON parsing, schema and
version validation (``aobs_server.model``), then the ingest preparation step
(``aobs_server.ingest.prepare``) with a ``metadata_only`` ceiling, so prompts, responses, tool
arguments and results never reach the report and error messages are secret-scrubbed.

A :class:`TraceSource` (traces stored in PostgreSQL, ``diagnosis.stored``) can stand wherever a
file can; its events go through the same preparation step. This module never imports the
storage layer itself.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeAlias

from ..ingest.prepare import prepare_events
from ..model import loads_strict, validate_batch
from ..model.parsing import StrictJSONError
from ..model.versioning import AcceptedEvent
from ..project_settings import ProjectSettings


@dataclass(frozen=True)
class InputProblem:
    source: str
    line: int | None
    message: str

    def __str__(self) -> str:
        where = self.source if self.line is None else f"{self.source}:{self.line}"
        return f"{where}: {self.message}"


@dataclass(frozen=True)
class SourceRead:
    """What a :class:`TraceSource` read: events already validated (stored events were), the raw
    resource of each trace, and problems to report against the source."""

    events: list[AcceptedEvent]
    resources: dict[str, dict[str, Any]]
    problems: list[str]
    traces: int


class TraceSource(Protocol):
    """A non-file input, e.g. a selection of stored traces. ``str()`` names it in reports."""

    def read(self) -> SourceRead: ...


Source: TypeAlias = Path | TraceSource


@dataclass(frozen=True)
class Telemetry:
    events: tuple[AcceptedEvent, ...]
    resources: dict[str, dict[str, Any]]  # trace_id -> summary of the resource that sent it
    problems: tuple[InputProblem, ...]
    batches: int
    counters: Counter[str] = field(default_factory=Counter)


def privacy_settings(error_messages: bool = True) -> ProjectSettings:
    """What the report may show: metadata only; scrubbed error messages unless turned off."""
    return ProjectSettings(capture_ceiling="metadata_only", error_messages=error_messages, stack_traces=False)


def _documents(path: Path) -> Iterator[tuple[int | None, Any]]:
    """A file holding one batch, a JSON list of batches, or one batch per line (SDK JSONL)."""
    raw = path.read_bytes()
    try:
        whole = loads_strict(raw)
    except StrictJSONError:
        whole = None
    if isinstance(whole, list):
        yield from ((None, item) for item in whole)
        return
    if isinstance(whole, dict):
        yield None, whole
        return
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            yield number, loads_strict(line)
        except StrictJSONError as exc:
            yield number, exc


def _resource_summary(resource: dict[str, Any]) -> dict[str, Any]:
    sdk = resource.get("sdk", {})
    return {
        "service_name": resource.get("service_name"),
        "service_version": resource.get("service_version"),
        "environment": resource.get("environment"),
        "sdk": f"{sdk.get('name')} {sdk.get('version')}".strip(),
        "capture_mode": resource.get("capture", {}).get("mode"),
        "integrations": sorted(i.get("name", "") for i in resource.get("integrations", []) or []),
    }


def load(paths: Iterable[Source], error_messages: bool = True) -> Telemetry:
    settings = privacy_settings(error_messages)
    events: list[AcceptedEvent] = []
    resources: dict[str, dict[str, Any]] = {}
    problems: list[InputProblem] = []
    counters: Counter[str] = Counter()
    batches = 0
    for path in paths:
        source = str(path)
        if not isinstance(path, Path):
            read = path.read()
            batches += read.traces  # one stored trace is read as one unit
            problems.extend(InputProblem(source, None, message) for message in read.problems)
            prepared = prepare_events(tuple(read.events), settings)
            counters.update(prepared.counters)
            for item in prepared.accepted:
                trace_id = item.event.trace_id
                if trace_id not in resources:
                    resources[trace_id] = _resource_summary(read.resources.get(trace_id, {}))
            events.extend(prepared.accepted)
            continue
        try:
            documents = list(_documents(path))
        except OSError as exc:
            problems.append(InputProblem(source, None, f"cannot read file: {exc.strerror or exc}"))
            continue
        for line, document in documents:
            if isinstance(document, StrictJSONError):
                problems.append(InputProblem(source, line, f"not valid JSON: {document}"))
                continue
            result = validate_batch(document)
            if not result.ok or result.envelope is None:
                first = result.batch_errors[0]
                problems.append(
                    InputProblem(source, line, f"batch rejected: {first.path or '<root>'}: {first.message}")
                )
                continue
            batches += 1
            for rejected in result.rejected:
                error = rejected.errors[0]
                problems.append(InputProblem(source, line, f"event rejected: {error.path}: {error.message}"))
            prepared = prepare_events(result.accepted, settings)
            counters.update(prepared.counters)
            summary = _resource_summary(result.envelope.resource.model_dump(mode="json", exclude_unset=True))
            for item in prepared.accepted:
                resources.setdefault(item.event.trace_id, summary)
            events.extend(prepared.accepted)
    return Telemetry(tuple(events), resources, tuple(problems), batches, counters)
