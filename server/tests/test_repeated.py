"""Repeated-run regression analysis (`aobs-server compare-groups`)."""

from __future__ import annotations

import json
import random
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aobs_server.cli import main
from aobs_server.diagnosis import GroupConfig, build_group_report, render_group_text
from support import REPO_ROOT

GROUPS = REPO_ROOT / "examples" / "diagnose" / "groups"
TRACES = REPO_ROOT / "examples" / "diagnose" / "traces"
PAIRS = REPO_ROOT / "examples" / "diagnose" / "compare"
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
CONFIDENT = {"likely_regression", "possible_improvement"}


def report(baseline: list[Path] | Path, current: list[Path] | Path, **kw: Any) -> Any:
    b = baseline if isinstance(baseline, list) else [baseline]
    c = current if isinstance(current, list) else [current]
    return build_group_report(b, c, NOW, **kw)


def verdicts(rep: Any) -> dict[str, str]:
    return {r.metric.key: r.verdict for r in rep.results}


def result(rep: Any, key: str) -> Any:
    (found,) = [r for r in rep.results if r.metric.key == key]
    return found


# --------------------------------------------------------------------------- synthetic runs

RESOURCE = {
    "service_name": "synthetic",
    "environment": "test",
    "sdk": {"name": "test", "version": "0", "language": "python"},
    "capture": {"mode": "metadata_only", "error_messages": True, "stack_traces": False},
}
_counter = iter(range(1, 10**9))


def _ts(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def synthetic_run(
    duration: float,
    *,
    task: str = "task",
    status: str = "ok",
    tool_ms: float | None = None,
    attempts: int = 1,
    tokens: int | None = None,
    cost: str | None = "absent",
    currency: str = "USD",
    finished: bool = True,
) -> list[dict[str, Any]]:
    n = next(_counter)
    trace, root = f"{n:032x}", f"{n:016x}"
    start = datetime(2026, 10, 12, tzinfo=UTC) + timedelta(minutes=n)
    root_fields = {"trace_id": trace, "span_id": root, "parent_span_id": None, "kind": "agent", "name": task,
                   "start_time": _ts(start), "agent": {"name": f"{task}_agent"}}  # fmt: skip
    events: list[dict[str, Any]] = []
    if finished:
        end = start + timedelta(milliseconds=duration)
        events.append({**root_fields, "type": "span_end", "end_time": _ts(end), "duration_ms": duration,
                       "status": status})  # fmt: skip
    else:
        events.append({**root_fields, "type": "span_start"})
    for attempt in range(1, attempts + 1):
        ms = tool_ms if tool_ms is not None else duration / 2
        events.append({"type": "span_end", "trace_id": trace, "span_id": f"{n:012x}{attempt:04x}",
                       "parent_span_id": root, "kind": "tool", "name": "lookup", "tool": {"name": "lookup"},
                       "start_time": _ts(start),
                       "end_time": _ts(start + timedelta(milliseconds=ms)), "duration_ms": ms,
                       "status": "error" if attempt < attempts or status == "error" else "ok",
                       **({"error": {"type": "ConnectionError"}} if attempt < attempts or status == "error" else {}),
                       "retry": {"attempt": attempt, "max_attempts": 3}})  # fmt: skip
    if tokens is not None or cost != "absent":
        llm: dict[str, Any] = {"type": "span_end", "trace_id": trace, "span_id": f"{n:012x}ffff",
                               "parent_span_id": root, "kind": "llm", "name": "chat", "start_time": _ts(start),
                               "end_time": _ts(start + timedelta(milliseconds=1)), "duration_ms": 1.0, "status": "ok",
                               "llm": {"request_model": "m1"}}  # fmt: skip
        if tokens is not None:
            llm["usage"] = {"input_tokens": tokens, "source": "provider_reported"}
        if cost not in (None, "absent"):
            llm["cost"] = {"amount": cost, "currency": currency, "source": "provider_reported"}
        events.append(llm)
    return events


def write_runs(path: Path, runs: list[list[dict[str, Any]]]) -> Path:
    lines = [
        json.dumps({"schema_version": "1.0", "sent_at": "2026-10-12T12:00:00Z", "resource": RESOURCE, "events": run})
        for run in runs
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


def group(tmp: Path, name: str, durations: list[float], **kw: Any) -> Path:
    return write_runs(tmp / f"{name}.jsonl", [synthetic_run(d, **kw) for d in durations])


# --------------------------------------------------------------------------- identical and normal variation


def test_identical_recordings_show_no_change() -> None:
    rep = report(GROUPS / "baseline.jsonl", GROUPS / "baseline.jsonl")
    assert set(verdicts(rep).values()) == {"no_change"}
    assert all(r.p_value == pytest.approx(1.0) for r in rep.results)
    assert rep.warnings == [] and rep.changes == [] and rep.severity() is None


def test_normal_timing_variation_is_not_a_regression() -> None:
    rep = report(GROUPS / "baseline.jsonl", GROUPS / "healthy_rerun.jsonl")
    assert not set(verdicts(rep).values()) & (CONFIDENT | {"weak_signal", "minor_change"})
    assert rep.severity() is None


def test_false_positive_rate_on_same_distribution_groups(tmp_path: Path) -> None:
    """40 comparisons of groups drawn from the same distribution: Holm keeps confident claims rare."""
    rng = random.Random(7)
    confident = 0
    for i in range(40):
        base = group(tmp_path, f"b{i}", [rng.gauss(200, 40) for _ in range(8)])
        cur = group(tmp_path, f"c{i}", [rng.gauss(200, 40) for _ in range(8)])
        confident += any(v in CONFIDENT for v in verdicts(report(base, cur)).values())
    assert confident <= 2


# --------------------------------------------------------------------------- genuine regressions


def test_genuine_latency_regression() -> None:
    rep = report(GROUPS / "baseline.jsonl", GROUPS / "slow.jsonl")
    run = result(rep, "run.duration")
    assert run.verdict == "likely_regression"
    assert run.p_adjusted is not None and run.p_adjusted < 0.05 and run.exact
    estimate, low, high = run.shift
    assert low is not None and high is not None and 0 < low <= estimate <= high
    assert result(rep, "op.tool.search_inventory.duration").verdict == "likely_regression"
    for key in ("op.llm.plan.duration", "op.llm.summarize.duration", "run.failed", "run.input_tokens"):
        assert result(rep, key).verdict == "no_change", key
    assert rep.severity() == "error"


def test_increased_failures_and_retries() -> None:
    rep = report(GROUPS / "baseline.jsonl", GROUPS / "flaky.jsonl")
    failed = result(rep, "run.failed")
    assert failed.verdict == "likely_regression"
    assert failed.describe(failed.baseline).startswith("0/10") and failed.describe(failed.current).startswith("7/10")
    assert failed.abs_change == pytest.approx(0.7)
    assert result(rep, "run.retries").verdict == "likely_regression"
    assert result(rep, "op.tool.search_inventory.calls").verdict == "likely_regression"
    assert any(c.startswith("new error: tool 'search_inventory' ConnectionError in 10 of 10") for c in rep.changes)


def test_five_runs_per_group_is_the_minimum_for_a_conclusion(tmp_path: Path) -> None:
    base5 = group(tmp_path, "b5", [100, 101, 102, 103, 104])
    cur5 = group(tmp_path, "c5", [300, 301, 302, 303, 304])
    assert result(report(base5, cur5), "run.duration").verdict == "likely_regression"
    base4 = group(tmp_path, "b4", [100, 101, 102, 103])
    cur4 = group(tmp_path, "c4", [300, 301, 302, 303])
    four = result(report(base4, cur4), "run.duration")
    assert four.verdict == "insufficient_data" and four.p_value is None
    assert result(report(base4, cur4, config=GroupConfig(min_runs=4)), "run.duration").tested


# --------------------------------------------------------------------------- outliers, noise, weak and minor


def test_one_outlier_is_not_a_confident_regression() -> None:
    rep = report(GROUPS / "baseline.jsonl", GROUPS / "outlier.jsonl")
    assert result(rep, "run.duration").verdict == "no_change"
    assert any(o.startswith("run duration: current run") and "1,5" in o for o in rep.outliers)
    assert "Data quality: outliers" in render_group_text(rep)


def test_one_failed_run_is_not_a_confident_regression(tmp_path: Path) -> None:
    base = group(tmp_path, "b", [100] * 8)
    cur = write_runs(
        tmp_path / "c.jsonl", [synthetic_run(100) for _ in range(7)] + [synthetic_run(100, status="error")]
    )
    failed = result(report(base, cur), "run.failed")
    assert failed.verdict == "weak_signal"  # 0/8 -> 1/8 is 12.5 pp, large enough to matter, far from conclusive
    assert failed.p_adjusted == pytest.approx(1.0)


def test_noisy_groups_give_a_weak_signal_not_a_regression(tmp_path: Path) -> None:
    base = group(tmp_path, "b", [100, 400, 150, 380, 120, 360])
    cur = group(tmp_path, "c", [180, 480, 230, 460, 200, 440])  # +80 ms each: large, but buried in noise
    run = result(report(base, cur), "run.duration")
    assert run.verdict == "weak_signal" and run.p_adjusted is not None and run.p_adjusted >= 0.05


def test_detectable_but_small_change_is_minor(tmp_path: Path) -> None:
    base = group(tmp_path, "b", [100.0 + i * 0.1 for i in range(10)])
    cur = group(tmp_path, "c", [103.0 + i * 0.1 for i in range(10)])
    run = result(report(base, cur), "run.duration")
    assert run.verdict == "minor_change"  # significant, but +3 ms is below the 50 ms / 20% thresholds


def test_large_relative_but_tiny_absolute_change_is_minor(tmp_path: Path) -> None:
    base = group(tmp_path, "b", [10.0 + i * 0.01 for i in range(10)])
    cur = group(tmp_path, "c", [14.0 + i * 0.01 for i in range(10)])
    run = result(report(base, cur), "run.duration")
    assert run.rel_change is not None and run.rel_change > 20  # +40% ...
    assert run.verdict == "minor_change"  # ... but only +4 ms, below the 50 ms practical threshold


def test_multiple_testing_correction_can_overturn_a_small_sample_result(tmp_path: Path) -> None:
    """5 vs 5 with complete separation gives p = 2/252 = 0.0079. Alone that would pass alpha = 0.05;
    tested together with 6 other run-level metrics, Holm multiplies it by 7 (0.056): only a weak signal."""

    def runs(durations: list[float]) -> list[list[dict[str, Any]]]:
        return [synthetic_run(d, tokens=100, cost="0.01") for d in durations]

    base = write_runs(tmp_path / "b.jsonl", runs([100, 101, 102, 103, 104]))
    cur = write_runs(tmp_path / "c.jsonl", runs([300, 301, 302, 303, 304]))
    run = result(report(base, cur), "run.duration")
    assert run.p_value == pytest.approx(2 / 252)
    assert run.family_size == 7 and run.p_adjusted == pytest.approx(7 * 2 / 252)
    assert run.verdict == "weak_signal"


def test_holm_adjustment_is_reported() -> None:
    rep = report(GROUPS / "baseline.jsonl", GROUPS / "slow.jsonl")
    for r in rep.results:
        if r.tested:
            assert r.p_adjusted is not None and r.p_value is not None and r.p_adjusted >= r.p_value
            assert r.family_size == sum(x.tested for x in rep.results if x.metric.family == r.metric.family)


# --------------------------------------------------------------------------- small samples and missing data


def test_too_few_recordings_give_descriptive_numbers_only() -> None:
    rep = report(GROUPS / "baseline.jsonl", GROUPS / "few_runs.jsonl")
    assert set(verdicts(rep).values()) == {"insufficient_data"}
    run = result(rep, "run.duration")
    assert run.p_value is None and run.shift is not None and run.shift[1] is None  # estimate, no interval
    assert run.abs_change is not None and run.abs_change > 50  # the slowdown is still shown, descriptively
    assert any("current group has 3 finished run(s)" in w for w in rep.warnings)
    text = render_group_text(rep)
    assert "no confidence interval (too few runs)" in text and "LIKELY" not in text


def test_incomplete_usage_and_cost_are_excluded_not_zero() -> None:
    rep = report(GROUPS / "baseline.jsonl", GROUPS / "partial_usage.jsonl")
    tokens = result(rep, "run.input_tokens")
    assert tokens.current.n == 4 and tokens.current.excluded == {"usage not reported by every model call": 6}
    assert tokens.verdict == "insufficient_data"
    cost = result(rep, "run.cost")
    assert cost.current.n == 4 and sum(cost.current.excluded.values()) == 6 and cost.currency == "USD"
    assert result(rep, "run.duration").verdict == "no_change"  # unaffected metrics still compared
    text = render_group_text(rep)
    assert "current: 6 run(s) excluded (usage not reported by every model call)" in text
    assert "close the data gap listed under evidence" in text


def test_costs_in_different_currencies_are_not_compared(tmp_path: Path) -> None:
    base = write_runs(tmp_path / "b.jsonl", [synthetic_run(100, tokens=10, cost="0.01") for _ in range(5)])
    cur = write_runs(tmp_path / "c.jsonl", [synthetic_run(100, tokens=10, cost="0.01", currency="EUR")
                                            for _ in range(5)])  # fmt: skip
    rep = report(base, cur)
    assert "run.cost" not in verdicts(rep)
    assert any("several currencies" in n for n in rep.notes)


def test_no_cost_anywhere_is_noted_not_invented(tmp_path: Path) -> None:
    base = write_runs(tmp_path / "b.jsonl", [synthetic_run(100, tokens=10) for _ in range(5)])
    rep = report(base, base)
    assert "run.cost" not in verdicts(rep)
    assert any("never estimates prices" in n for n in rep.notes)


def test_unfinished_runs_are_excluded_and_reported(tmp_path: Path) -> None:
    base = group(tmp_path, "b", [100] * 6)
    runs = [synthetic_run(100) for _ in range(6)] + [synthetic_run(100, finished=False) for _ in range(2)]
    cur = write_runs(tmp_path / "c.jsonl", runs)
    rep = report(base, cur)
    run = result(rep, "run.duration")
    assert run.current.n == 6 and run.current.excluded == {}  # left out of every metric, reported once
    assert all(r.current.n <= 6 for r in rep.results)
    assert any("2 current run(s) never finished" in w for w in rep.warnings)


# --------------------------------------------------------------------------- different tasks, malformed input


def test_different_tasks_are_warned_about() -> None:
    rep = report([GROUPS / "baseline.jsonl"], [PAIRS / "refund_ok.jsonl", PAIRS / "refund_ok_rerun.jsonl"])
    assert any("different tasks" in w for w in rep.warnings)
    assert any("environment differs" in w for w in rep.warnings)
    assert rep.severity() in ("warning", "error")


def test_mixed_groups_are_warned_about_and_task_filter_selects(tmp_path: Path) -> None:
    runs = [synthetic_run(100) for _ in range(5)] + [synthetic_run(500, task="other") for _ in range(5)]
    mixed = write_runs(tmp_path / "mixed.jsonl", runs)
    base = group(tmp_path, "b", [100] * 5)
    rep = report(base, mixed)
    assert any("mixes different tasks" in w for w in rep.warnings)
    filtered = report(base, mixed, task="task")
    assert len(filtered.current.runs) == 5 and len(filtered.current.skipped) == 5
    assert not any("tasks" in w for w in filtered.warnings)


def test_malformed_lines_are_reported_and_valid_runs_used(tmp_path: Path) -> None:
    good = group(tmp_path, "g", [100] * 5).read_text()
    mixed = tmp_path / "mixed.jsonl"
    mixed.write_text("{broken\n" + good + '{"schema_version": "9.9"}\n')
    rep = report(GROUPS / "baseline.jsonl", mixed)
    problems = [str(p) for p in rep.current.problems]
    assert any(":1: not valid JSON" in p for p in problems) and any("batch rejected" in p for p in problems)
    assert len(rep.current.runs) == 5


# --------------------------------------------------------------------------- privacy and CLI


SENSITIVE = ["jane.doe@example.com", "Berlin warehouse", "Summarize the account", "Jane has two", "c-7781"]


@pytest.mark.parametrize("as_json", [False, True])
def test_reports_never_show_content(as_json: bool, capsys: pytest.CaptureFixture[str]) -> None:
    files = [str(TRACES / "retry_storm.jsonl"), str(TRACES / "slow_and_repeated.jsonl")]
    code = main(["compare-groups", "--baseline", *files, "--current", *files, *(["--json"] if as_json else [])])
    out = capsys.readouterr().out
    assert code == 0
    for secret in SENSITIVE:
        assert secret not in out, secret


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(["compare-groups", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_exit_codes_and_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base, slow, rerun = (str(GROUPS / f"{n}.jsonl") for n in ("baseline", "slow", "healthy_rerun"))
    assert _cli(capsys, "--baseline", base, "--current", slow)[0] == 0
    assert _cli(capsys, "--baseline", base, "--current", slow, "--fail-on", "error")[0] == 3
    assert _cli(capsys, "--baseline", base, "--current", rerun, "--fail-on", "info")[0] == 0
    code, out, _ = _cli(capsys, "--baseline", base, "--current", slow, "--json")
    data = json.loads(out)
    assert code == 0 and {"disclaimer", "method", "config", "baseline", "current", "metrics", "warnings"} <= set(data)
    duration = next(m for m in data["metrics"] if m["key"] == "run.duration")
    assert duration["verdict"] == "likely_regression" and duration["test"] == "mann_whitney"
    assert duration["baseline"]["n"] == 10 and duration["shift_estimate"]["ci95"][0] > 0
    assert _cli(capsys, "--baseline", base, "--current", slow, "--min-runs", "2")[0] == 2
    assert _cli(capsys, "--baseline", base, "--current", slow, "--alpha", "0")[0] == 2
    code, _, err = _cli(capsys, "--baseline", base, "--current", str(tmp_path / "missing.jsonl"))
    assert code == 1 and "no such file" in err
    garbage = tmp_path / "garbage.jsonl"
    garbage.write_text("nope\n")
    code, _, err = _cli(capsys, "--baseline", base, "--current", str(garbage))
    assert code == 1 and "no runs found" in err
    code, _, err = _cli(capsys, "--baseline", base, "--current", slow, "--task", "nope")
    assert code == 1 and "for task 'nope'" in err


def test_report_is_deterministic() -> None:
    first = report(GROUPS / "baseline.jsonl", GROUPS / "flaky.jsonl").to_dict()
    second = report(GROUPS / "baseline.jsonl", GROUPS / "flaky.jsonl").to_dict()
    assert first == second


# --------------------------------------------------------------------------- reproducible samples


def test_freshly_recorded_groups_give_the_same_conclusions(tmp_path: Path) -> None:
    script = REPO_ROOT / "examples" / "diagnose" / "make_group_samples.py"
    subprocess.run([sys.executable, str(script), "--out", str(tmp_path)], check=True, capture_output=True, timeout=180)
    base = tmp_path / "baseline.jsonl"
    assert result(report(base, tmp_path / "slow.jsonl"), "run.duration").verdict == "likely_regression"
    flaky = report(base, tmp_path / "flaky.jsonl")
    assert result(flaky, "run.failed").verdict == "likely_regression"
    assert result(flaky, "run.retries").verdict == "likely_regression"
    assert not set(verdicts(report(base, tmp_path / "healthy_rerun.jsonl")).values()) & CONFIDENT
    assert result(report(base, tmp_path / "outlier.jsonl"), "run.duration").verdict not in CONFIDENT
    assert set(verdicts(report(base, tmp_path / "few_runs.jsonl")).values()) == {"insufficient_data"}
    assert result(report(base, tmp_path / "partial_usage.jsonl"), "run.input_tokens").verdict == "insufficient_data"
