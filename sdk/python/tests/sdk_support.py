"""Helpers for SDK tests: schema validation of emitted batches and trace assembly.

Every batch the SDK emits in these tests can be checked against BOTH the normative JSON Schema
(ECMA pattern semantics) and the Phase 1 collector model (strict bytes path)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import ValidationError as SchemaError

import aobs
from aobs.export import InMemoryExporter
from aobs_server.model import AcceptedEvent, TraceView, assemble_events, validate_batch
from aobs_server.model.wire import ecma_to_python

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA = json.loads((REPO_ROOT / "schema" / "v1" / "ingest.schema.json").read_text())


def _ecma_pattern(validator: Any, pattern: str, instance: Any, schema: Any) -> Iterator[SchemaError]:
    if validator.is_type(instance, "string") and ecma_to_python(pattern).search(instance) is None:
        yield SchemaError(f"{instance!r} does not match {pattern!r}")


_Validator = validators.extend(Draft202012Validator, {"pattern": _ecma_pattern})
SCHEMA_VALIDATOR = _Validator(SCHEMA, format_checker=Draft202012Validator.FORMAT_CHECKER)


def check_bodies(bodies: list[bytes]) -> list[AcceptedEvent]:
    """Assert every body is schema-valid and fully accepted by the collector model."""
    accepted: list[AcceptedEvent] = []
    for body in bodies:
        payload = json.loads(body)
        errors = [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in SCHEMA_VALIDATOR.iter_errors(payload)]
        assert not errors, errors[:5]
        result = validate_batch(body)
        assert result.ok, result.batch_errors
        assert not result.rejected, result.rejected[:3]
        assert not result.lenient
        accepted.extend(result.accepted)
    return accepted


def traces(exporter: InMemoryExporter) -> dict[str, TraceView]:
    return assemble_events(check_bodies(exporter.bodies), datetime.now(UTC))


def only_trace(exporter: InMemoryExporter) -> TraceView:
    views = traces(exporter)
    assert len(views) == 1, list(views)
    return next(iter(views.values()))


def ends(exporter: InMemoryExporter) -> list[dict[str, Any]]:
    return [e for e in exporter.events() if e["type"] == "span_end"]


def end_by_name(exporter: InMemoryExporter, name: str) -> dict[str, Any]:
    matches = [e for e in ends(exporter) if e["name"] == name]
    assert len(matches) == 1, [e["name"] for e in ends(exporter)]
    return matches[0]


def flush() -> None:
    assert aobs.flush(5), aobs.diagnostics()
