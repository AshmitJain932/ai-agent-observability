"""Offline trace diagnosis (`aobs-server analyze`): rules, evidence, privacy, input handling, CLI."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_merge import T, accepted, end, start

from aobs_server.cli import main
from aobs_server.diagnosis import RuleConfig, build_report, diagnose, render_text
from aobs_server.model import assemble_events
from support import REPO_ROOT, fixture_files, fixture_id, fixture_path, load_fixture

SAMPLES = REPO_ROOT / "examples" / "diagnose" / "traces"
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)

EXPECTED_RULES = {
    "refund_outage": {"error.root_cause"},
    "retry_storm": {"error.root_cause", "error.repeated", "data.missing_usage"},
    "slow_and_repeated": {"waste.repeated_call", "latency.bottleneck"},
    "crashed_run": {"trace.unfinished"},
    "healthy_run": set(),
}
# Content present in the sample files (recorded with capture="full"), never in a report.
SENSITIVE = ["jane.doe@example.com", "Berlin warehouse", "Summarize the account", "Jane has two", "c-7781"]


def rules_of(path: Path, now: datetime = NOW) -> set[str]:
    (trace,) = build_report([path], now).traces
    return {f.rule for f in trace.findings}


# --------------------------------------------------------------------------- the sample traces


@pytest.mark.parametrize("name", sorted(EXPECTED_RULES))
def test_committed_samples_produce_exactly_the_expected_findings(name: str) -> None:
    assert rules_of(SAMPLES / f"{name}.jsonl") == EXPECTED_RULES[name]


def test_freshly_recorded_samples_produce_the_same_findings(tmp_path: Path) -> None:
    """Runs the real SDK and the example agent end to end, then diagnoses what they wrote."""
    script = REPO_ROOT / "examples" / "diagnose" / "make_samples.py"
    subprocess.run([sys.executable, str(script), "--out", str(tmp_path)], check=True, capture_output=True, timeout=120)
    for name, expected in EXPECTED_RULES.items():
        assert rules_of(tmp_path / f"{name}.jsonl", datetime.now(UTC)) == expected, name


def test_every_finding_cites_existing_spans_and_has_evidence_and_a_suggestion() -> None:
    report = build_report(sorted(SAMPLES.glob("*.jsonl")), NOW)
    assert len(report.traces) == len(EXPECTED_RULES)
    for trace_report in report.traces:
        for finding in trace_report.findings:
            assert finding.span_ids and set(finding.span_ids) <= set(trace_report.trace.spans)
            assert finding.evidence and all(finding.evidence)
            assert len(finding.suggestion) > 20


def test_refund_outage_explains_the_failure() -> None:
    (trace,) = build_report([SAMPLES / "refund_outage.jsonl"], NOW).traces
    (finding,) = trace.findings
    assert finding.title == "tool 'issue_refund' failed with TimeoutError"
    text = " ".join(finding.evidence)
    assert "origin=timeout" in text and "payments API did not respond" in text
    assert "parents that also failed: agent answer_ticket (TimeoutError)" in text
    assert "timed out" in finding.suggestion and "unhandled" in finding.suggestion


def test_retry_storm_reports_attempts_and_identical_input() -> None:
    (trace,) = build_report([SAMPLES / "retry_storm.jsonl"], NOW).traces
    repeated = next(f for f in trace.findings if f.rule == "error.repeated")
    assert len(repeated.span_ids) == 3
    assert "retry attempts 1, 2, 3 of 3" in repeated.evidence
    assert "every attempt had the same input fingerprint (identical input)" in repeated.evidence
    assert "backoff" in repeated.suggestion


def test_report_shows_tool_calls_and_timings() -> None:
    (trace,) = build_report([SAMPLES / "slow_and_repeated.jsonl"], NOW).traces
    assert [(t["tool"], t["calls"], t["failed"]) for t in trace.tools()] == [("fetch_customer_profile", 3, 0)]
    bottleneck = next(f for f in trace.findings if f.rule == "latency.bottleneck")
    assert bottleneck.title.startswith("retrieval vector_search took")
    assert trace.summary()["duration_ms"] > 400


# --------------------------------------------------------------------------- privacy


def test_sample_files_really_contain_the_sensitive_strings() -> None:
    raw = (SAMPLES / "retry_storm.jsonl").read_text() + (SAMPLES / "slow_and_repeated.jsonl").read_text()
    for secret in SENSITIVE:
        assert secret in raw, secret


@pytest.mark.parametrize("as_json", [False, True])
def test_reports_never_show_content_or_secrets(
    as_json: bool, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AOBS_DATABASE_URL", raising=False)
    files = [str(p) for p in sorted(SAMPLES.glob("*.jsonl"))]
    assert main(["analyze", *files, *(["--json"] if as_json else [])]) == 0
    out = capsys.readouterr().out
    for secret in SENSITIVE:
        assert secret not in out, secret
    assert "[REDACTED:url_credentials]" in out  # the SDK scrubbed this password before writing the file


def test_secrets_the_sdk_did_not_scrub_are_scrubbed_by_the_report(tmp_path: Path) -> None:
    """A file from another emitter (or an old SDK) with secrets in an error message and attribute."""
    batch = json.loads((SAMPLES / "refund_outage.jsonl").read_text().splitlines()[0])
    leaked = "Bearer abcdefghijklmnop12345 password=hunter2pw"
    for event in batch["events"]:
        if event.get("status") == "error":
            event["error"]["message"] = leaked
            event["attributes"] = {"app.debug": leaked}
    path = tmp_path / "leaky.jsonl"
    path.write_text(json.dumps(batch) + "\n")
    report = build_report([path], NOW)
    out = render_text(report) + json.dumps(report.to_dict())
    assert "hunter2pw" not in out and "abcdefghijklmnop12345" not in out
    assert "[REDACTED:" in out
    assert report.redactions["secrets_scrubbed"] >= 4


def test_error_messages_can_be_left_out_entirely() -> None:
    report = build_report([SAMPLES / "refund_outage.jsonl"], NOW, error_messages=False)
    text = render_text(report)
    assert "payments API did not respond" not in text
    assert "TimeoutError" in text  # the error type is metadata, still shown


# --------------------------------------------------------------------------- the Phase 1 fixtures


def _fixture_file(tmp_path: Path, name: str) -> Path:
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(load_fixture(fixture_path("valid", name))["payload"]))
    return path


@pytest.mark.parametrize("path", fixture_files("valid"), ids=fixture_id)
def test_every_valid_fixture_can_be_analyzed(path: Path, tmp_path: Path) -> None:
    report = build_report([_fixture_file(tmp_path, path.stem)], NOW)
    assert report.traces and not report.problems
    assert render_text(report)
    json.dumps(report.to_dict())


@pytest.mark.parametrize(
    ("name", "rule"),
    [
        ("failed_tool_run", "error.root_cause"),
        ("orphans_and_cycles", "trace.missing_parent"),
        ("orphans_and_cycles", "trace.parent_cycle"),
        ("incomplete_run", "trace.unfinished"),
        ("multiple_roots_and_skew", "data.clock_skew"),
    ],
)
def test_fixture_findings(name: str, rule: str, tmp_path: Path) -> None:
    report = build_report([_fixture_file(tmp_path, name)], NOW)
    assert rule in {f.rule for t in report.traces for f in t.findings}


# --------------------------------------------------------------------------- rules on small traces


def _trace(*events: dict[str, Any]) -> Any:
    (view,) = assemble_events(accepted(*events), NOW).values()
    return view


FP = "hmac-sha256:k1:" + "ab" * 32
ROOT = "1" * 16


def _tool(span_id: str, status: str = "ok", duration: float = 1.0, offset_ms: int = 0, **kw: Any) -> dict[str, Any]:
    t0 = datetime(2026, 10, 12, 12, 0, tzinfo=UTC) + timedelta(milliseconds=offset_ms)
    t1 = t0 + timedelta(milliseconds=duration)
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
    return end(
        span_id=span_id,
        parent_span_id=ROOT,
        kind="tool",
        name="lookup",
        tool={"name": "lookup"},
        start_time=t0.strftime(fmt),
        end_time=t1.strftime(fmt),
        duration_ms=duration,
        status=status,
        **kw,
    )


ROOT_END = end(span_id=ROOT, kind="agent", name="run", duration_ms=1000.0)


def test_one_identical_call_is_not_waste_two_are() -> None:
    once = _trace(ROOT_END, _tool("a" * 16, input_fingerprint=FP))
    twice = _trace(ROOT_END, _tool("a" * 16, input_fingerprint=FP), _tool("b" * 16, input_fingerprint=FP, offset_ms=5))
    assert "waste.repeated_call" not in {f.rule for f in diagnose(once)}
    assert "waste.repeated_call" in {f.rule for f in diagnose(twice)}


def test_identical_failed_retries_are_not_counted_as_waste() -> None:
    error = {"type": "ValueError"}
    trace = _trace(
        ROOT_END,
        _tool("a" * 16, status="error", error=error, input_fingerprint=FP),
        _tool("b" * 16, status="error", error=error, input_fingerprint=FP, offset_ms=5),
    )
    rules = {f.rule for f in diagnose(trace)}
    assert "error.repeated" in rules and "waste.repeated_call" not in rules


def test_non_retryable_repeats_get_a_different_suggestion() -> None:
    error = {"type": "ValueError", "retryable": False}
    trace = _trace(ROOT_END, _tool("a" * 16, status="error", error=error), _tool("b" * 16, status="error", error=error))
    (finding,) = [f for f in diagnose(trace) if f.rule == "error.repeated"]
    assert "non-retryable" in finding.suggestion


@pytest.mark.parametrize(("duration", "flagged"), [(499.0, False), (500.0, True)])
def test_bottleneck_threshold_is_a_share_of_the_run(duration: float, flagged: bool) -> None:
    trace = _trace(ROOT_END, _tool("a" * 16, duration=duration))
    assert ("latency.bottleneck" in {f.rule for f in diagnose(trace)}) is flagged


def test_bottleneck_needs_a_minimum_duration() -> None:
    short_root = end(span_id=ROOT, kind="agent", name="run", duration_ms=10.0)
    trace = _trace(short_root, _tool("a" * 16, duration=9.0))
    assert "latency.bottleneck" not in {f.rule for f in diagnose(trace)}
    assert "latency.bottleneck" in {f.rule for f in diagnose(trace, RuleConfig(slow_min_ms=5))}


def test_open_spans_are_unfinished_whatever_their_age() -> None:
    trace = _trace(start(span_id=ROOT, kind="agent", name="run"))
    (finding,) = diagnose(trace)
    assert finding.rule == "trace.unfinished" and "the run itself never finished" in finding.evidence[-1]
    assert trace.trace_id == T


def test_report_is_deterministic() -> None:
    files = sorted(SAMPLES.glob("*.jsonl"))
    forward, backward = build_report(files, NOW).to_dict(), build_report(list(reversed(files)), NOW).to_dict()
    del forward["sources"], backward["sources"]  # the only part that follows argument order
    assert forward == backward


# --------------------------------------------------------------------------- input handling and CLI


def test_bad_lines_are_reported_and_good_ones_still_analyzed(tmp_path: Path) -> None:
    good = (SAMPLES / "refund_outage.jsonl").read_text().strip()
    path = tmp_path / "mixed.jsonl"
    path.write_text(f"{{not json\n{good}\n{json.dumps({'schema_version': '9.0'})}\n")
    report = build_report([path], NOW)
    assert len(report.traces) == 1
    messages = [str(p) for p in report.problems]
    assert any(":1: not valid JSON" in m for m in messages)
    assert any(":3: batch rejected" in m for m in messages)


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(["analyze", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    outage, healthy = str(SAMPLES / "refund_outage.jsonl"), str(SAMPLES / "healthy_run.jsonl")
    assert _cli(capsys, outage)[0] == 0
    assert _cli(capsys, outage, "--fail-on", "error")[0] == 3
    assert _cli(capsys, healthy, "--fail-on", "info")[0] == 0
    code, _, err = _cli(capsys, str(tmp_path / "missing.jsonl"))
    assert code == 1 and "no such file" in err
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    code, out, _ = _cli(capsys, str(empty))
    assert code == 1 and "No traces found." in out
    assert _cli(capsys, outage, "--slow-share", "0")[0] == 2


def test_cli_json_and_trace_filter(capsys: pytest.CaptureFixture[str]) -> None:
    files = [str(p) for p in sorted(SAMPLES.glob("*.jsonl"))]
    code, out, _ = _cli(capsys, *files, "--json")
    data = json.loads(out)
    assert code == 0 and len(data["traces"]) == len(EXPECTED_RULES)
    first = data["traces"][0]["summary"]["trace_id"]
    code, out, _ = _cli(capsys, *files, "--json", "--trace", first[:10])
    assert [t["summary"]["trace_id"] for t in json.loads(out)["traces"]] == [first]
    assert set(data["traces"][0]) == {"summary", "spans", "tools", "findings"}
