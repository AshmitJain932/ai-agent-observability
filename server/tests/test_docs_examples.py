"""The JSON examples in docs/event-model.md must stay valid against the schema and the models."""

import json
import re
from typing import Any

from aobs_server.model import validate_batch
from support import REPO_ROOT, schema_error_paths

DOC = (REPO_ROOT / "docs" / "event-model.md").read_text()


def _json_blocks() -> list[str]:
    return re.findall(r"```json\n(.*?)```", DOC, re.DOTALL)


def _envelope() -> dict[str, Any]:
    block = next(b for b in _json_blocks() if '"schema_version"' in b)
    envelope: dict[str, Any] = json.loads(block)
    return envelope


def _example_events() -> list[dict[str, Any]]:
    block = next(b for b in reversed(_json_blocks()) if b.lstrip().startswith("["))
    events: list[dict[str, Any]] = json.loads(block)
    return events


def test_envelope_and_example_events_are_valid() -> None:
    envelope = _envelope()
    events = _example_events()
    assert len(events) == 3
    envelope["events"] = events
    assert schema_error_paths(envelope) == []
    result = validate_batch(envelope)
    assert result.ok and not result.rejected, (result.batch_errors, result.rejected)


def test_span_event_example_is_valid() -> None:
    block = next(b for b in _json_blocks() if '"type": "span_event"' in b)
    envelope = _envelope()
    envelope["events"] = [json.loads(block)]
    assert schema_error_paths(envelope) == []
    assert validate_batch(envelope).accepted
