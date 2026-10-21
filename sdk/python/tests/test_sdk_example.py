"""The example agent runs in mock mode and produces the expected, connected trace."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

from aobs_server.model import validate_batch
from sdk_support import REPO_ROOT

EXAMPLE_DIR = REPO_ROOT / "examples" / "support_agent"


@pytest.fixture
def example() -> ModuleType:
    sys.path.insert(0, str(EXAMPLE_DIR))
    try:
        for name in ("run", "agent", "mock_llm"):
            sys.modules.pop(name, None)
        return importlib.import_module("run")
    finally:
        sys.path.remove(str(EXAMPLE_DIR))


def _trace(path: Path) -> tuple[dict[str, dict], list[dict]]:
    events = []
    for line in path.read_bytes().splitlines():
        result = validate_batch(line)
        assert result.ok and not result.rejected
        events.extend(a.canonical() for a in result.accepted)
    ends = {e["span_id"]: e for e in events if e["type"] == "span_end"}
    return ends, events


def test_success_run_produces_connected_trace(example: ModuleType, tmp_path: Path) -> None:
    out = tmp_path / "t.jsonl"
    assert example.main(["--out", str(out), "--quiet"]) == 0
    ends, _ = _trace(out)
    (root,) = [e for e in ends.values() if e["parent_span_id"] is None]
    assert root["kind"] == "agent" and root["name"] == "answer_ticket" and root["status"] == "ok"
    children = sorted((e["start_time"], e["kind"], e["name"]) for e in ends.values() if e is not root)
    assert [(k, n) for _, k, n in children] == [("llm", "chat"), ("tool", "get_order_status"), ("llm", "chat")]
    assert all(
        e["parent_span_id"] == root["span_id"] and e["trace_id"] == root["trace_id"]
        for e in ends.values()
        if e is not root
    )
    llm = next(e for e in ends.values() if e["kind"] == "llm")
    assert llm["usage"]["source"] == "provider_reported" and llm["llm"]["provider"] == "mock_llm"


def test_tool_outage_marks_tool_and_run_failed(example: ModuleType, tmp_path: Path) -> None:
    out = tmp_path / "t.jsonl"
    assert example.main(["--scenario", "refund-outage", "--out", str(out), "--quiet"]) == 1
    ends, _ = _trace(out)
    refund = next(e for e in ends.values() if e["name"] == "issue_refund")
    assert refund["status"] == "error" and refund["error"]["type"] == "TimeoutError"
    assert refund["error"]["origin"] == "timeout"
    root = next(e for e in ends.values() if e["parent_span_id"] is None)
    assert root["status"] == "error"


def test_model_outage_marks_the_model_call_and_run_failed(example: ModuleType, tmp_path: Path) -> None:
    out = tmp_path / "t.jsonl"
    assert example.main(["--scenario", "model-outage", "--out", str(out), "--quiet"]) == 1
    ends, _ = _trace(out)
    assert sorted(e["kind"] for e in ends.values()) == ["agent", "llm"]  # no tool ran
    llm = next(e for e in ends.values() if e["kind"] == "llm")
    assert llm["status"] == "error" and llm["llm"]["provider"] == "mock_llm"
    assert llm["error"]["origin"] == "provider" and llm["error"]["code"] == "503" and llm["error"]["retryable"]
    root = next(e for e in ends.values() if e["parent_span_id"] is None)
    assert root["status"] == "error" and root["error"]["type"] == llm["error"]["type"]


def test_each_run_answers_its_own_ticket(example: ModuleType, tmp_path: Path) -> None:
    sessions = []
    for name in ("a", "b"):
        out = tmp_path / f"{name}.jsonl"
        assert example.main(["--out", str(out), "--quiet"]) == 0
        ends, _ = _trace(out)
        sessions.append(next(e for e in ends.values() if e["parent_span_id"] is None)["session_id"])
    assert sessions[0] != sessions[1] and all(s.startswith("ticket-success-") for s in sessions)
    out = tmp_path / "c.jsonl"
    assert example.main(["--ticket", "T-1042", "--out", str(out), "--quiet"]) == 0
    ends, _ = _trace(out)
    assert next(e for e in ends.values() if e["parent_span_id"] is None)["session_id"] == "T-1042"


def test_model_outage_is_mock_only(example: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="needs --mode mock"):
        example.main(["--mode", "live", "--scenario", "model-outage", "--out", str(tmp_path / "t.jsonl"), "--quiet"])


def test_no_usage_mode_keeps_tokens_unknown(example: ModuleType, tmp_path: Path) -> None:
    out = tmp_path / "t.jsonl"
    assert example.main(["--no-usage", "--out", str(out), "--quiet"]) == 0
    ends, _ = _trace(out)
    assert all("usage" not in e for e in ends.values())


def test_instrumentation_is_at_most_ten_statements() -> None:
    # Every instrumentation statement (imports included) carries a trailing "# aobs" marker on its
    # first line; docstrings and comments that merely mention the marker are not counted.
    marked = [
        line
        for f in ("agent.py", "run.py")
        for line in (EXAMPLE_DIR / f).read_text().splitlines()
        if "# aobs" in line and not line.lstrip().startswith(("#", '"', "The ", "``"))
    ]
    code = [line for line in marked if "aobs" in line.split("#", 1)[0] or "wrap(" in line]
    assert code == marked, [m for m in marked if m not in code]
    assert 1 <= len(marked) <= 10, len(marked)


def test_live_mode_requires_explicit_credentials(
    example: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AOBS_EXAMPLE_MODEL", raising=False)
    with pytest.raises(SystemExit, match="OPENAI_API_KEY"):
        example.main(["--mode", "live", "--out", str(tmp_path / "t.jsonl"), "--quiet"])
