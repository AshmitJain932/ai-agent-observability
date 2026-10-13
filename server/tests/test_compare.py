"""Cross-run regression detection (`aobs-server compare`)."""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from aobs_server.cli import main
from aobs_server.diagnosis import CompareConfig, build_comparisons, render_comparison_text
from aobs_server.diagnosis.compare import money
from support import REPO_ROOT

PAIRS = REPO_ROOT / "examples" / "diagnose" / "compare"
TRACES = REPO_ROOT / "examples" / "diagnose" / "traces"
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)

INVENTORY_RULES = Counter(
    {
        "regression.new_error": 1,
        "regression.slower_operation": 1,
        "regression.more_retries": 1,
        "regression.higher_cost": 1,
        "regression.slower_run": 1,
        "regression.more_tokens": 2,
        "regression.more_calls": 1,
        "change.model": 2,
        "change.tool_added": 1,
        "change.tool_removed": 1,
    }
)


def compare(baseline: Path, current: Path, **kw: Any) -> Any:
    (comparison,) = build_comparisons(baseline, [current], NOW, **kw).comparisons
    return comparison


def rules(comparison: Any) -> Counter[str]:
    return Counter(c.rule for c in comparison.changes)


def by_rule(comparison: Any, rule: str) -> Any:
    (change,) = [c for c in comparison.changes if c.rule == rule]
    return change


def rewrite(source: Path, target: Path, edit: Any) -> Path:
    """Copy a JSONL trace file, applying ``edit(event)`` to every event (return None to drop it)."""
    lines = []
    for line in source.read_text().splitlines():
        batch = json.loads(line)
        batch["events"] = [e for e in (edit(ev) for ev in batch["events"]) if e is not None]
        if batch["events"]:
            lines.append(json.dumps(batch))
    target.write_text("\n".join(lines) + "\n")
    return target


# --------------------------------------------------------------------------- healthy vs failed


def test_healthy_baseline_vs_failed_run() -> None:
    comparison = compare(PAIRS / "refund_ok.jsonl", PAIRS / "refund_outage.jsonl")
    assert rules(comparison) == Counter({"regression.run_failed": 1, "regression.new_error": 1})
    first = comparison.changes[0]
    assert first.rule == "regression.run_failed" and first.severity == "error"
    assert (first.baseline, first.current) == ("ok", "error")
    assert first.evidence == ("current root cause: tool 'issue_refund' failed with TimeoutError",)
    new_error = by_rule(comparison, "regression.new_error")
    assert new_error.difference == "+1 failure(s)"
    assert "baseline: 0 failure(s) in 1 call(s)" in new_error.evidence  # the tool ran fine before


def test_fixed_run_is_reported_as_an_improvement() -> None:
    comparison = compare(PAIRS / "refund_outage.jsonl", PAIRS / "refund_ok.jsonl")
    assert rules(comparison)["improvement.run_fixed"] == 1
    # The failed baseline stopped early; the extra model call and tokens of the full run are context, not
    # regressions.
    others = [c for c in comparison.changes if c.rule != "improvement.run_fixed"]
    assert others and all(c.severity == "info" for c in others)
    assert all(c.possible_causes[0].startswith("the baseline run failed and stopped early") for c in others)
    assert comparison.notes[0].startswith("the baseline failed")


# --------------------------------------------------------------------------- latency, retries, calls, models


def test_inventory_release_produces_exactly_the_expected_findings() -> None:
    assert rules(compare(PAIRS / "inventory_v1.jsonl", PAIRS / "inventory_v2.jsonl")) == INVENTORY_RULES


def test_latency_regression_is_measured_with_units_and_attributed() -> None:
    comparison = compare(PAIRS / "inventory_v1.jsonl", PAIRS / "inventory_v2.jsonl")
    run = by_rule(comparison, "regression.slower_run")
    assert run.baseline.endswith(" ms") and run.current.endswith(" ms")
    assert run.difference.startswith("+") and " ms (+" in run.difference
    assert any(e.startswith("tool 'check_supplier': total time new") for e in run.evidence)
    assert any(e.startswith("llm 'summarize': total time") for e in run.evidence)
    op = by_rule(comparison, "regression.slower_operation")
    assert op.title == "llm 'summarize' is slower per call" and "ms per call" in op.difference


def test_retries_and_repeated_calls() -> None:
    comparison = compare(PAIRS / "inventory_v1.jsonl", PAIRS / "inventory_v2.jsonl")
    retries = by_rule(comparison, "regression.more_retries")
    assert (retries.baseline, retries.current, retries.difference) == (
        "0 retry attempt(s)",
        "2 retry attempt(s)",
        "+2 attempt(s)",
    )
    calls = by_rule(comparison, "regression.more_calls")
    assert calls.title == "tool 'search_inventory' is called more often"
    assert calls.difference == "+2 call(s) (+200%)"


def test_model_tokens_and_provider_cost() -> None:
    comparison = compare(PAIRS / "inventory_v1.jsonl", PAIRS / "inventory_v2.jsonl")
    models = [c for c in comparison.changes if c.rule == "change.model"]
    assert {(m.baseline, m.current) for m in models} == {("planner-small", "planner-large")}
    tokens = {c.title: c for c in comparison.changes if c.rule == "regression.more_tokens"}
    inp = tokens["the run uses more input tokens"]
    assert (inp.baseline, inp.current, inp.difference) == ("720 tokens", "2,850 tokens", "+2,130 tokens (+296%)")
    cost = by_rule(comparison, "regression.higher_cost")
    assert (cost.baseline, cost.current) == ("0.00039 USD", "0.005 USD")  # exact decimal sums, no floats
    assert cost.difference == "+0.00461 USD (+1182%)"


def test_money_formatting() -> None:
    assert money(Decimal("0.00021") + Decimal("0.00018")) == "0.00039"
    assert money(Decimal("0.00500")) == "0.005"
    assert money(Decimal("12")) == "12"
    assert money(Decimal("0")) == "0"


# --------------------------------------------------------------------------- no false regressions


@pytest.mark.parametrize("name", ["refund_ok", "inventory_v1", "inventory_v2", "refund_outage"])
def test_a_run_compared_with_itself_has_no_findings(name: str) -> None:
    comparison = compare(PAIRS / f"{name}.jsonl", PAIRS / f"{name}.jsonl")
    assert comparison.changes == []
    assert comparison.notes[0].startswith("both sides are the same trace id")


def test_independent_recordings_of_the_same_task_have_no_findings() -> None:
    comparison = compare(PAIRS / "refund_ok.jsonl", PAIRS / "refund_ok_rerun.jsonl")
    assert comparison.baseline.trace_id != comparison.current.trace_id  # different span ids, matched by name
    assert comparison.changes == []


def test_thresholds_control_what_counts_as_slower() -> None:
    lenient = CompareConfig(min_change_pct=10_000, min_run_ms=10_000, min_op_ms=10_000)
    comparison = compare(PAIRS / "inventory_v1.jsonl", PAIRS / "inventory_v2.jsonl", config=lenient)
    assert not {"regression.slower_run", "regression.slower_operation"} & set(rules(comparison))


# --------------------------------------------------------------------------- missing and incompatible data


def test_missing_usage_is_reported_not_compared(tmp_path: Path) -> None:
    def drop_usage(event: dict[str, Any]) -> dict[str, Any]:
        if event.get("kind") == "llm" and event.get("name") == "summarize":
            event.pop("usage", None)
        return event

    current = rewrite(PAIRS / "inventory_v2.jsonl", tmp_path / "v2.jsonl", drop_usage)
    comparison = compare(PAIRS / "inventory_v1.jsonl", current)
    found = rules(comparison)
    assert "regression.more_tokens" not in found and found["data.incomplete_usage"] == 2
    gap = next(c for c in comparison.changes if c.rule == "data.incomplete_usage")
    assert "1 of 2 model call(s) reported it" in gap.current and gap.difference == "not computed"


def test_cost_is_only_compared_when_every_call_reports_it(tmp_path: Path) -> None:
    def drop_cost(event: dict[str, Any]) -> dict[str, Any]:
        if event.get("name") == "plan":
            event.pop("cost", None)
        return event

    current = rewrite(PAIRS / "inventory_v2.jsonl", tmp_path / "v2.jsonl", drop_cost)
    comparison = compare(PAIRS / "inventory_v1.jsonl", current)
    assert "regression.higher_cost" not in rules(comparison)
    assert any("cost not compared" in n and "1 of 2 model call(s)" in n for n in comparison.notes)
    assert all("never estimates prices" in n for n in comparison.notes if n.startswith("cost"))


def test_different_tasks_are_flagged(tmp_path: Path) -> None:
    comparison = compare(PAIRS / "inventory_v1.jsonl", PAIRS / "refund_ok.jsonl")
    first = comparison.changes[0]
    assert first.rule == "compare.different_task" and first.severity == "warning"
    assert "restock_check" in first.baseline and "answer_ticket" in first.current


def test_unfinished_run_is_not_compared_on_duration_or_outcome(tmp_path: Path) -> None:
    def drop_root_end(event: dict[str, Any]) -> dict[str, Any] | None:
        if event.get("type") == "span_end" and event.get("parent_span_id") is None:
            start = {
                k: v
                for k, v in event.items()
                if k not in ("end_time", "duration_ms", "status", "error", "usage", "cost", "content")
            }
            return {**start, "type": "span_start"}
        return event

    current = rewrite(PAIRS / "refund_outage.jsonl", tmp_path / "unfinished.jsonl", drop_root_end)
    comparison = compare(PAIRS / "refund_ok.jsonl", current)
    found = rules(comparison)
    assert found["data.unfinished_run"] == 1
    assert "regression.run_failed" not in found and "regression.slower_run" not in found
    assert "regression.new_error" in found  # the failing tool span itself was recorded


# --------------------------------------------------------------------------- input handling


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(["compare", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_malformed_lines_are_reported_and_valid_runs_compared(tmp_path: Path) -> None:
    good = (PAIRS / "refund_outage.jsonl").read_text()
    mixed = tmp_path / "mixed.jsonl"
    mixed.write_text("{broken\n" + good)
    report = build_comparisons(PAIRS / "refund_ok.jsonl", [mixed], NOW)
    assert any(":1: not valid JSON" in str(p) for p in report.problems)
    assert "regression.run_failed" in rules(report.comparisons[0])


def test_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ok, rerun, outage = (str(PAIRS / f"{n}.jsonl") for n in ("refund_ok", "refund_ok_rerun", "refund_outage"))
    assert _cli(capsys, ok, outage)[0] == 0
    assert _cli(capsys, ok, outage, "--fail-on", "error")[0] == 3
    assert _cli(capsys, ok, rerun, "--fail-on", "info")[0] == 0
    code, _, err = _cli(capsys, ok, str(tmp_path / "missing.jsonl"))
    assert code == 1 and "no such file" in err
    garbage = tmp_path / "garbage.jsonl"
    garbage.write_text("not json\n")
    code, _, err = _cli(capsys, ok, str(garbage))
    assert code == 1 and "no readable traces" in err
    assert _cli(capsys, ok, outage, "--min-op-ms", "-1")[0] == 2


def test_files_with_several_runs_need_a_selector(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    both = tmp_path / "both.jsonl"
    both.write_text((PAIRS / "refund_ok.jsonl").read_text() + (PAIRS / "refund_outage.jsonl").read_text())
    ok_id = build_comparisons(PAIRS / "refund_ok.jsonl", [PAIRS / "refund_ok.jsonl"], NOW).comparisons[0]
    bad_id = build_comparisons(PAIRS / "refund_outage.jsonl", [PAIRS / "refund_outage.jsonl"], NOW).comparisons[0]
    code, _, err = _cli(capsys, str(both), str(both))
    assert code == 2 and "holds 2 runs" in err and ok_id.baseline.trace_id[:12] in err
    code, out, _ = _cli(
        capsys, str(both), str(both),
        "--baseline-trace", ok_id.baseline.trace_id[:8], "--current-trace", bad_id.baseline.trace_id[:8], "--json",
    )  # fmt: skip
    assert code == 0
    found = [f["rule"] for f in json.loads(out)["comparisons"][0]["findings"]]
    assert "regression.run_failed" in found
    code, _, err = _cli(capsys, str(both), str(both), "--baseline-trace", "ffffffff")
    assert code == 2 and "no trace id starts with" in err


def test_several_current_files_are_each_compared_with_the_baseline(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = _cli(
        capsys, *(str(PAIRS / f"{n}.jsonl") for n in ("refund_ok", "refund_ok_rerun", "refund_outage")), "--json"
    )
    data = json.loads(out)
    assert code == 0 and len(data["comparisons"]) == 2
    assert data["comparisons"][0]["findings"] == []
    assert {f["rule"] for f in data["comparisons"][1]["findings"]} == {"regression.run_failed", "regression.new_error"}
    for comparison in data["comparisons"]:
        assert set(comparison) == {"baseline", "current", "findings", "notes"}
        for finding in comparison["findings"]:
            assert set(finding) == {
                "rule", "severity", "title", "baseline", "current", "difference",
                "evidence", "why_it_matters", "possible_causes", "next_step",
            }  # fmt: skip


# --------------------------------------------------------------------------- privacy


SENSITIVE = ["jane.doe@example.com", "Berlin warehouse", "Summarize the account", "Jane has two", "c-7781"]


@pytest.mark.parametrize("as_json", [False, True])
def test_comparison_never_shows_content(as_json: bool, capsys: pytest.CaptureFixture[str]) -> None:
    raw = (TRACES / "retry_storm.jsonl").read_text() + (TRACES / "slow_and_repeated.jsonl").read_text()
    assert all(s in raw for s in SENSITIVE)  # the inputs really contain content (capture="full")
    args = [str(TRACES / "slow_and_repeated.jsonl"), str(TRACES / "retry_storm.jsonl")]
    _, out, _ = _cli(capsys, *args, *(["--json"] if as_json else []))
    for secret in SENSITIVE:
        assert secret not in out, secret


def test_secrets_in_error_messages_never_reach_the_report(tmp_path: Path) -> None:
    def leak(event: dict[str, Any]) -> dict[str, Any]:
        if event.get("status") == "error":
            event["error"]["message"] = "token=supersecretvalue Bearer abcdefghijklmnop12345"
            event["attributes"] = {"app.note": "password=hunter2pw"}
        return event

    current = rewrite(PAIRS / "refund_outage.jsonl", tmp_path / "leaky.jsonl", leak)
    report = build_comparisons(PAIRS / "refund_ok.jsonl", [current], NOW)
    out = render_comparison_text(report) + json.dumps(report.to_dict())
    for secret in ("supersecretvalue", "abcdefghijklmnop12345", "hunter2pw"):
        assert secret not in out


# --------------------------------------------------------------------------- reproducible samples


def test_freshly_recorded_pairs_give_the_same_findings(tmp_path: Path) -> None:
    script = REPO_ROOT / "examples" / "diagnose" / "make_compare_samples.py"
    subprocess.run([sys.executable, str(script), "--out", str(tmp_path)], check=True, capture_output=True, timeout=120)
    assert rules(compare(tmp_path / "inventory_v1.jsonl", tmp_path / "inventory_v2.jsonl")) == INVENTORY_RULES
    assert rules(compare(tmp_path / "refund_ok.jsonl", tmp_path / "refund_ok_rerun.jsonl")) == Counter()
    failed = rules(compare(tmp_path / "refund_ok.jsonl", tmp_path / "refund_outage.jsonl"))
    assert failed == Counter({"regression.run_failed": 1, "regression.new_error": 1})
