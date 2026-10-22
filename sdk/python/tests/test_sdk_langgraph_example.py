"""The LangGraph example runs offline and produces the expected, connected trace."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

from aobs_server.model import validate_batch
from sdk_support import REPO_ROOT

EXAMPLE_DIR = REPO_ROOT / "examples" / "langgraph_agent"


@pytest.fixture
def example() -> ModuleType:
    sys.path.insert(0, str(EXAMPLE_DIR))
    try:
        sys.modules.pop("run", None)
        return importlib.import_module("run")
    finally:
        sys.path.remove(str(EXAMPLE_DIR))
        sys.modules.pop("run", None)


def _ends(path: Path) -> list[dict]:
    events = []
    for line in path.read_bytes().splitlines():
        result = validate_batch(line)
        assert result.ok and not result.rejected
        events.extend(a.canonical() for a in result.accepted)
    return sorted((e for e in events if e["type"] == "span_end"), key=lambda e: e["start_time"])


def test_success_run_produces_connected_trace(example: ModuleType, tmp_path: Path) -> None:
    out = tmp_path / "t.jsonl"
    assert example.main(["--out", str(out), "--quiet"]) == 0
    ends = _ends(out)
    (root,) = [e for e in ends if e["parent_span_id"] is None]
    assert root["kind"] == "agent" and root["name"] == "support_agent" and root["status"] == "ok"
    assert root["session_id"] == "ticket-1042"
    assert {e["trace_id"] for e in ends} == {root["trace_id"]}
    assert [(e["kind"], e["name"]) for e in ends if e["kind"] in ("llm", "tool")] == [
        ("llm", "chat"),
        ("tool", "get_order_status"),
        ("llm", "chat"),
    ]
    first_llm = next(e for e in ends if e["kind"] == "llm")
    assert first_llm["usage"]["input_tokens"] == 52 and first_llm["usage"]["source"] == "provider_reported"


def test_tool_error_marks_tool_and_run_failed(example: ModuleType, tmp_path: Path) -> None:
    out = tmp_path / "t.jsonl"
    assert example.main(["--scenario", "tool-error", "--out", str(out), "--quiet"]) == 1
    ends = _ends(out)
    tool_end = next(e for e in ends if e["kind"] == "tool")
    assert tool_end["status"] == "error" and tool_end["error"]["type"] == "KeyError"
    assert next(e for e in ends if e["parent_span_id"] is None)["status"] == "error"
