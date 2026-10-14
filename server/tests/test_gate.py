"""Reliability gate (`aobs-server gate`): policy validation, checks, missing data, verdicts, CLI."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_compare import rewrite

from aobs_server.cli import main
from aobs_server.diagnosis.gate import (
    Check,
    GateReport,
    PolicyError,
    RunResult,
    build_gate_report,
    load_baseline,
    load_policy,
    parse_policy,
    render_gate_text,
)
from support import REPO_ROOT

DIAGNOSE = REPO_ROOT / "examples" / "diagnose"
PAIRS = DIAGNOSE / "compare"
GROUPS = DIAGNOSE / "groups"
TRACES = DIAGNOSE / "traces"
POLICIES = DIAGNOSE / "gate"
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
# Content present in the sample files (recorded with capture="full"); it must never reach the output.
SENSITIVE = ["jane.doe@example.com", "Berlin warehouse", "Summarize the account", "Jane has two", "c-7781"]


def gate(
    run: dict[str, Any] | None = None, *paths: Path, baseline_file: Path | None = None, **sections: Any
) -> GateReport:
    data: dict[str, Any] = {"version": 1, **sections}
    if run is not None:
        data["run"] = run
    base = None if baseline_file is None else load_baseline(baseline_file, NOW, None, error_messages=True)[0]
    return build_gate_report(parse_policy(data), list(paths), NOW, baseline=base)


def only_run(report: GateReport) -> RunResult:
    (result,) = report.runs
    return result


def check(report: GateReport, check_id: str) -> Check:
    matches = [c for r in report.runs for c in r.checks if c.id == check_id]
    matches += [c for c in report.gate_checks if c.id == check_id]
    assert len(matches) == 1, [c.id for r in report.runs for c in r.checks]
    return matches[0]


def results(report: GateReport) -> dict[str, str]:
    return {c.id: c.result for c in only_run(report).checks}


# --------------------------------------------------------------------------- normal runs


def test_healthy_run_passes_the_example_policy() -> None:
    policy = load_policy(POLICIES / "support_agent.toml")
    report = build_gate_report(policy, [PAIRS / "refund_ok.jsonl"], NOW)
    assert report.verdict == "pass"
    assert set(results(report).values()) == {"pass"}
    assert [c.result for c in report.gate_checks] == ["pass", "pass", "pass"]
    assert check(report, "run.max_input_tokens").actual == "207"
    assert check(report, "run.required_tools.get_order_status").span_ids  # evidence points to the call


def test_report_is_deterministic_and_json_serializable() -> None:
    policy = load_policy(POLICIES / "support_agent.toml")
    files = [PAIRS / "refund_ok.jsonl", PAIRS / "refund_outage.jsonl", PAIRS / "refund_ok_rerun.jsonl"]
    first = build_gate_report(policy, files, NOW)
    second = build_gate_report(policy, list(reversed(files)), NOW)
    assert json.dumps(first.to_dict()) == json.dumps(build_gate_report(policy, files, NOW).to_dict())
    # runs are ordered by source file, so the input order does not matter
    assert [r.run.trace_id for r in first.runs] == [r.run.trace_id for r in second.runs]
    assert render_gate_text(first) == render_gate_text(build_gate_report(policy, files, NOW))


def test_check_order_follows_the_policy() -> None:
    report = gate(
        {"max_tool_calls": 5, "require_success": True, "required_tools": ["get_order_status"]},
        PAIRS / "refund_ok.jsonl",
    )
    # fixed order (outcome first), independent of the order of keys in the file
    assert [c.id for c in only_run(report).checks] == [
        "run.require_success",
        "run.max_tool_calls",
        "run.required_tools.get_order_status",
    ]


# --------------------------------------------------------------------------- failures with evidence


def test_failed_run_names_the_root_cause_and_the_failing_span() -> None:
    policy = load_policy(POLICIES / "support_agent.toml")
    report = build_gate_report(policy, [PAIRS / "refund_outage.jsonl"], NOW)
    assert report.verdict == "fail"
    outcome = check(report, "run.require_success")
    assert outcome.result == "fail" and outcome.actual == "error"
    assert any("tool 'issue_refund' failed with TimeoutError" in e for e in outcome.evidence)
    failures = check(report, "run.tools.issue_refund.max_failures")
    assert failures.result == "fail" and failures.actual == "1"
    (span_id,) = failures.span_ids
    assert span_id in only_run(report).run.trace.spans
    assert "TimeoutError" in failures.evidence[0]
    assert check(report, "gate.pass_rate").result == "fail"


def test_handled_errors_count_as_success_only_if_allowed() -> None:
    file = PAIRS / "inventory_v2.jsonl"  # ends ok_with_errors: a search failed, the agent recovered
    assert check(gate({"require_success": True}, file), "run.require_success").result == "pass"
    strict = check(gate({"require_success": True, "allow_handled_errors": False}, file), "run.require_success")
    assert strict.result == "fail" and "recovered" in strict.message
    assert any("ConnectionError" in e for e in strict.evidence)


def test_retries_redundant_calls_and_failed_spans() -> None:
    storm = gate({"max_retries": 1, "max_failed_spans": 1}, TRACES / "retry_storm.jsonl")
    assert check(storm, "run.max_retries").result == "fail"
    assert check(storm, "run.max_retries").actual == "2"
    assert check(storm, "run.max_failed_spans").result == "fail"
    repeated = check(gate({"max_redundant_calls": 1}, TRACES / "slow_and_repeated.jsonl"), "run.max_redundant_calls")
    assert repeated.result == "fail" and repeated.actual == "2"  # 3 identical calls = 2 redundant
    assert len(repeated.span_ids) == 3 and "identical input" in repeated.evidence[0]


def test_required_and_forbidden_tools() -> None:
    report = gate(
        {"required_tools": ["check_supplier"], "forbidden_tools": ["issue_refund"]}, PAIRS / "refund_ok.jsonl"
    )
    required = check(report, "run.required_tools.check_supplier")
    assert required.result == "fail" and "get_order_status, issue_refund" in required.evidence[0]
    forbidden = check(report, "run.forbidden_tools.issue_refund")
    assert forbidden.result == "fail" and len(forbidden.span_ids) == 1


def test_baseline_requirements_against_a_regressed_version() -> None:
    policy = load_policy(POLICIES / "restock_baseline.toml")
    base, _ = load_baseline(PAIRS / "inventory_v1.jsonl", NOW, None, error_messages=True)
    report = build_gate_report(policy, [PAIRS / "inventory_v2.jsonl"], NOW, baseline=base)
    assert report.verdict == "fail"
    assert results(report) == {
        "run.require_success": "pass",
        "run.require_complete": "pass",
        "run.max_retries": "fail",
        "run.max_cost": "fail",
        "baseline.forbid_outcome_regression": "pass",
        "baseline.forbid_new_error_types": "fail",
        "baseline.forbid_removed_tools": "fail",
        "baseline.max_duration_increase_pct": "fail",
        "baseline.max_tool_call_increase": "fail",
        "baseline.max_input_token_increase_pct": "fail",
    }
    assert "ConnectionError" in check(report, "baseline.forbid_new_error_types").evidence[0]
    assert check(report, "baseline.forbid_removed_tools").actual == "read_cache"
    assert check(report, "baseline.max_tool_call_increase").actual == "+2"


def test_baseline_catches_an_outcome_regression_and_accepts_a_healthy_rerun() -> None:
    reqs = {
        "forbid_outcome_regression": True,
        "forbid_new_error_types": True,
        "forbid_new_tools": True,
        "forbid_removed_tools": True,
        "max_tool_call_increase": 0,
    }
    report = gate(None, PAIRS / "refund_outage.jsonl", baseline_file=PAIRS / "refund_ok.jsonl", baseline=reqs)
    assert check(report, "baseline.forbid_outcome_regression").result == "fail"
    assert check(report, "baseline.forbid_new_error_types").result == "fail"
    rerun = gate(None, PAIRS / "refund_ok_rerun.jsonl", baseline_file=PAIRS / "refund_ok.jsonl", baseline=reqs)
    assert rerun.verdict == "pass", render_gate_text(rerun)


# --------------------------------------------------------------------------- several runs: pass rate and min runs


@pytest.mark.parametrize(("rate", "verdict"), [(0.3, "pass"), (0.31, "fail"), (0.0, "pass"), (1.0, "fail")])
def test_pass_rate_boundary(rate: float, verdict: str) -> None:
    # flaky.jsonl: 10 runs, 3 recover (ok_with_errors), 7 fail
    report = gate({"require_success": True}, GROUPS / "flaky.jsonl", gate={"min_pass_rate": rate})
    assert sum(r.verdict == "pass" for r in report.runs) == 3
    assert report.verdict == verdict
    assert check(report, "gate.pass_rate").actual == "3 of 10 (30%)"


@pytest.mark.parametrize(("min_runs", "result"), [(10, "pass"), (11, "fail")])
def test_min_runs_boundary(min_runs: int, result: str) -> None:
    report = gate({"require_success": True}, GROUPS / "baseline.jsonl", gate={"min_runs": min_runs})
    assert check(report, "gate.min_runs").result == result


def test_task_filter_excludes_other_runs_and_reports_them() -> None:
    files = (PAIRS / "refund_ok.jsonl", PAIRS / "inventory_v1.jsonl")
    report = gate({"require_success": True}, *files, gate={"task": "restock_check"})
    assert [r.run.run_name for r in report.runs] == ["restock_check"]
    assert report.excluded_runs == 1
    assert "1 run(s) with another task" in check(report, "gate.min_runs").message


def test_no_matching_runs_fails_the_gate() -> None:
    report = gate({"require_success": True}, PAIRS / "refund_ok.jsonl", gate={"task": "no_such_task"})
    assert report.verdict == "fail"
    assert check(report, "gate.min_runs").result == "fail"
    assert check(report, "gate.pass_rate").result == "unknown"


# --------------------------------------------------------------------------- missing data and incomplete traces


def test_crashed_run_outcome_is_unknown_and_counts_are_lower_bounds() -> None:
    report = gate(
        {
            "require_success": True,
            "require_complete": True,
            "max_tool_calls": 1,
            "max_llm_calls": 5,
            "max_input_tokens": 100,
            "max_duration_ms": 1e9,
        },
        TRACES / "crashed_run.jsonl",
    )
    assert results(report) == {
        "run.require_success": "unknown",  # no final status was recorded
        "run.require_complete": "fail",
        "run.max_duration_ms": "unknown",
        "run.max_tool_calls": "fail",  # 2 calls so far: already above the limit
        "run.max_llm_calls": "unknown",  # 0 so far, more may have followed
        "run.max_input_tokens": "unknown",
    }
    assert check(report, "run.max_tool_calls").message.startswith("at least 2")
    assert len(check(report, "run.require_complete").span_ids) == 2
    assert report.verdict == "fail"


def test_missing_usage_is_unknown_and_on_missing_data_decides() -> None:
    file = GROUPS / "partial_usage.jsonl"  # 10 runs; 6 have a model call without usage
    strict = gate({"max_input_tokens": 10_000}, file)
    assert sum(r.verdict == "pass" for r in strict.runs) == 4
    unknown = [c for r in strict.runs for c in r.checks if c.result == "unknown"]
    assert len(unknown) == 6 and all("usage incomplete" in c.message for c in unknown)
    assert all(c.actual.startswith(">= ") and c.span_ids for c in unknown)
    assert strict.verdict == "fail"
    lenient = gate({"max_input_tokens": 10_000}, file, gate={"on_missing_data": "warn"})
    assert lenient.verdict == "pass"
    assert sum(c.result == "unknown" for r in lenient.runs for c in r.checks) == 6  # still reported


def test_a_partial_total_above_the_limit_is_a_failure() -> None:
    report = gate({"max_input_tokens": 400}, GROUPS / "partial_usage.jsonl")
    partial = [c for r in report.runs for c in r.checks if c.actual.startswith(">= ")]
    assert partial and all(c.result == "fail" for c in partial)  # reported part alone is 410+ > 400


def test_cost_unknown_when_not_reported_or_in_another_currency(tmp_path: Path) -> None:
    missing = check(gate({"max_cost": "1", "cost_currency": "USD"}, PAIRS / "refund_ok.jsonl"), "run.max_cost")
    assert missing.result == "unknown" and "provider-reported cost on 0 of 3" in missing.message
    other = check(gate({"max_cost": "1", "cost_currency": "EUR"}, PAIRS / "inventory_v1.jsonl"), "run.max_cost")
    assert other.result == "unknown" and "in USD, the limit in EUR" in other.message


def test_redundant_calls_unknown_without_fingerprints(tmp_path: Path) -> None:
    def strip(event: dict[str, Any]) -> dict[str, Any]:
        event.pop("input_fingerprint", None)
        return event

    file = rewrite(PAIRS / "refund_ok.jsonl", tmp_path / "no_fingerprints.jsonl", strip)
    result = check(gate({"max_redundant_calls": 0}, file), "run.max_redundant_calls")
    assert result.result == "unknown" and "no input fingerprint" in result.message


def test_orphan_spans_make_a_trace_incomplete(tmp_path: Path) -> None:
    def detach(event: dict[str, Any]) -> dict[str, Any]:
        if event.get("kind") == "tool":
            event["parent_span_id"] = "f" * 16
        return event

    file = rewrite(PAIRS / "refund_ok.jsonl", tmp_path / "orphans.jsonl", detach)
    result = check(gate({"require_complete": True}, file), "run.require_complete")
    assert result.result == "fail" and "missing or cyclic parent" in result.message
    assert any("not in the trace" in e for e in result.evidence)


def test_baseline_that_did_not_finish_is_not_a_reference() -> None:
    reqs = {"forbid_new_error_types": True, "max_duration_increase_pct": 10}
    report = gate(None, PAIRS / "refund_ok.jsonl", baseline_file=TRACES / "crashed_run.jsonl", baseline=reqs)
    assert set(results(report).values()) == {"unknown"}
    assert all("baseline run did not finish" in c.message for c in only_run(report).checks)


# --------------------------------------------------------------------------- threshold boundaries


def _duration(file: Path) -> float:
    duration = only_run(gate({"require_success": True}, file)).run.duration_ms
    assert duration is not None
    return duration


@pytest.mark.parametrize(
    ("run", "check_id", "fails_below"),
    [
        ({"max_tool_calls": 2}, "run.max_tool_calls", {"max_tool_calls": 1}),
        ({"max_llm_calls": 3}, "run.max_llm_calls", {"max_llm_calls": 2}),
        ({"max_input_tokens": 207}, "run.max_input_tokens", {"max_input_tokens": 206}),
        (
            {"tools": {"issue_refund": {"max_calls": 1}}},
            "run.tools.issue_refund.max_calls",
            {"tools": {"issue_refund": {"max_calls": 0}}},
        ),
    ],
)
def test_limits_are_inclusive(run: dict[str, Any], check_id: str, fails_below: dict[str, Any]) -> None:
    file = PAIRS / "refund_ok.jsonl"
    assert check(gate(run, file), check_id).result == "pass"
    assert check(gate(fails_below, file), check_id).result == "fail"


def test_duration_and_cost_boundaries() -> None:
    file = PAIRS / "inventory_v1.jsonl"
    duration = _duration(file)
    assert check(gate({"max_duration_ms": duration}, file), "run.max_duration_ms").result == "pass"
    assert check(gate({"max_duration_ms": duration - 0.01}, file), "run.max_duration_ms").result == "fail"
    assert check(gate({"max_cost": "0.00039", "cost_currency": "USD"}, file), "run.max_cost").result == "pass"
    assert check(gate({"max_cost": "0.000389", "cost_currency": "USD"}, file), "run.max_cost").result == "fail"


def test_baseline_increase_boundaries() -> None:
    base, cur = PAIRS / "inventory_v1.jsonl", PAIRS / "inventory_v2.jsonl"
    pct = (_duration(cur) - _duration(base)) / _duration(base) * 100
    ok = gate(None, cur, baseline_file=base, baseline={"max_duration_increase_pct": pct, "max_tool_call_increase": 2})
    assert set(results(ok).values()) == {"pass"}
    tight = gate(
        None, cur, baseline_file=base, baseline={"max_duration_increase_pct": pct - 0.01, "max_tool_call_increase": 1}
    )
    assert set(results(tight).values()) == {"fail"}


# --------------------------------------------------------------------------- malformed input


def test_malformed_lines_fail_the_gate_unless_allowed(tmp_path: Path) -> None:
    file = tmp_path / "mixed.jsonl"
    file.write_text("not json\n" + (PAIRS / "refund_ok.jsonl").read_text() + '{"schema_version": "9.0"}\n')
    report = gate({"require_success": True}, file)
    valid = check(report, "gate.input_valid")
    assert valid.result == "fail" and valid.actual == "2"
    assert any("not valid JSON" in e for e in valid.evidence)
    assert only_run(report).verdict == "pass"  # the readable run is still checked
    assert report.verdict == "fail"
    warned = gate({"require_success": True}, file, gate={"on_input_problems": "warn"})
    assert check(warned, "gate.input_valid").result == "warn"
    assert warned.verdict == "pass"


def test_file_without_runs_fails_on_min_runs(tmp_path: Path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    report = gate({"require_success": True}, empty)
    assert report.verdict == "fail" and not report.runs
    assert check(report, "gate.min_runs").result == "fail"


# --------------------------------------------------------------------------- policy validation


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"run": {"require_success": True}}, "version: required"),
        ({"version": 2, "run": {"require_success": True}}, "version"),
        ({"version": 1, "run": {"max_tool_cals": 3}}, "run.max_tool_cals: unknown setting"),
        ({"version": 1, "gates": {}}, "gates: unknown setting"),
        ({"version": 1, "run": {"max_tool_calls": "3"}}, "run.max_tool_calls"),
        ({"version": 1, "run": {"max_tool_calls": True}}, "run.max_tool_calls"),
        ({"version": 1, "run": {"max_tool_calls": -1}}, "run.max_tool_calls"),
        ({"version": 1, "run": {"max_duration_ms": -0.5}}, "run.max_duration_ms"),
        ({"version": 1, "gate": {"min_pass_rate": 1.5}, "run": {"require_success": True}}, "gate.min_pass_rate"),
        ({"version": 1, "gate": {"min_runs": 0}, "run": {"require_success": True}}, "gate.min_runs"),
        ({"version": 1, "gate": {"on_missing_data": "pass"}, "run": {"require_success": True}}, "on_missing_data"),
        ({"version": 1}, "sets no requirement"),
        ({"version": 1, "run": {"allow_handled_errors": False}}, "sets no requirement"),
        ({"version": 1, "baseline": {}}, "[baseline] section sets no requirement"),
        ({"version": 1, "run": {"max_cost": "0.1"}}, "max_cost and cost_currency must be set together"),
        ({"version": 1, "run": {"max_cost": "abc", "cost_currency": "USD"}}, "run.max_cost"),
        ({"version": 1, "run": {"max_cost": "-1", "cost_currency": "USD"}}, "run.max_cost"),
        ({"version": 1, "run": {"max_cost": "1", "cost_currency": "usd"}}, "run.cost_currency"),
        ({"version": 1, "run": {"required_tools": ["a"], "forbidden_tools": ["a"]}}, "both required and forbidden"),
        ({"version": 1, "run": {"tools": {"x": {}}}}, "run.tools.x"),
        ({"version": 1, "run": {"tools": {"x": {"max_call": 1}}}}, "run.tools.x.max_call: unknown setting"),
    ],
)
def test_invalid_policies_are_rejected(data: dict[str, Any], message: str) -> None:
    with pytest.raises(PolicyError, match="invalid policy") as info:
        parse_policy(data)
    assert message in str(info.value)


def test_policy_file_errors(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("version = 1\n[run\n")
    with pytest.raises(PolicyError, match="not valid TOML"):
        load_policy(bad)
    with pytest.raises(PolicyError, match="cannot read policy file"):
        load_policy(tmp_path / "missing.toml")


def test_example_policies_are_valid() -> None:
    paths = sorted(POLICIES.glob("*.toml"))
    assert len(paths) == 2
    for path in paths:
        load_policy(path)


def test_policy_example_in_the_docs_is_valid() -> None:
    import tomllib

    text = (REPO_ROOT / "docs" / "reliability-gate.md").read_text()
    (block,) = [b.split("```", 1)[0] for b in text.split("```toml\n")[1:]]
    policy = parse_policy(tomllib.loads(block))
    assert policy.baseline is not None and policy.run.tools["issue_refund"].max_calls == 1


def test_baseline_section_and_baseline_run_must_come_together() -> None:
    with_section = parse_policy({"version": 1, "baseline": {"forbid_new_tools": True}})
    with pytest.raises(PolicyError, match="no baseline run was given"):
        build_gate_report(with_section, [PAIRS / "refund_ok.jsonl"], NOW)
    base, _ = load_baseline(PAIRS / "refund_ok.jsonl", NOW, None, error_messages=True)
    without = parse_policy({"version": 1, "run": {"require_success": True}})
    with pytest.raises(PolicyError, match="no \\[baseline\\] section"):
        build_gate_report(without, [PAIRS / "refund_ok.jsonl"], NOW, baseline=base)


# --------------------------------------------------------------------------- privacy


@pytest.mark.parametrize("as_json", [False, True])
def test_output_never_shows_content_or_secrets(
    as_json: bool, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AOBS_DATABASE_URL", raising=False)
    policy = tmp_path / "p.toml"
    policy.write_text("version = 1\n[run]\nrequire_success = true\nmax_redundant_calls = 0\nmax_retries = 0\n")
    files = [str(p) for p in sorted(TRACES.glob("*.jsonl"))]
    assert main(["gate", "--policy", str(policy), *files, *(["--json"] if as_json else [])]) == 3
    out = capsys.readouterr().out
    for secret in SENSITIVE:
        assert secret not in out, secret
    raw = (TRACES / "retry_storm.jsonl").read_text() + (TRACES / "slow_and_repeated.jsonl").read_text()
    assert all(secret in raw for secret in SENSITIVE)  # the inputs really contain them


# --------------------------------------------------------------------------- command line


Cli = Callable[..., tuple[int, str, str]]


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> Cli:
    monkeypatch.delenv("AOBS_DATABASE_URL", raising=False)

    def run(*argv: str) -> tuple[int, str, str]:
        code = main(["gate", *argv])
        out = capsys.readouterr()
        return code, out.out, out.err

    return run


def test_cli_exit_codes(cli: Cli, tmp_path: Path) -> None:
    policy = str(POLICIES / "support_agent.toml")
    code, out, _ = cli("--policy", policy, str(PAIRS / "refund_ok.jsonl"))
    assert code == 0 and out.startswith("aobs reliability gate: PASS")
    code, out, _ = cli("--policy", policy, str(PAIRS / "refund_outage.jsonl"))
    assert code == 3 and "FAIL" in out and "run.require_success" in out
    code, _, err = cli("--policy", policy, str(tmp_path / "missing.jsonl"))
    assert code == 1 and "no such file" in err
    code, _, err = cli("--policy", str(tmp_path / "missing.toml"), str(PAIRS / "refund_ok.jsonl"))
    assert code == 2 and "cannot read policy file" in err
    typo = tmp_path / "typo.toml"
    typo.write_text("version = 1\n[run]\nmax_tool_cals = 3\n")
    code, _, err = cli("--policy", str(typo), str(PAIRS / "refund_ok.jsonl"))
    assert code == 2 and "run.max_tool_cals: unknown setting" in err


def test_cli_baseline_options(cli: Cli, tmp_path: Path) -> None:
    policy = str(POLICIES / "restock_baseline.toml")
    current = str(PAIRS / "inventory_v2.jsonl")
    code, _, err = cli("--policy", policy, current)
    assert code == 2 and "--baseline" in err
    code, out, _ = cli("--policy", policy, "--baseline", str(PAIRS / "inventory_v1.jsonl"), current)
    assert code == 3 and "baseline.forbid_new_error_types" in out
    code, _, err = cli("--policy", str(POLICIES / "support_agent.toml"), "--baseline", current, current)
    assert code == 2 and "no [baseline] section" in err
    code, _, err = cli("--policy", policy, "--baseline", str(GROUPS / "baseline.jsonl"), current)
    assert code == 2 and "choose one with a trace id prefix" in err
    code, out, _ = cli(
        "--policy", policy, "--baseline", str(GROUPS / "baseline.jsonl"), "--baseline-trace", "5bb5", current
    )
    assert code == 3 and "baseline: " in out
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    code, _, err = cli("--policy", policy, "--baseline", str(empty), current)
    assert code == 1 and "no readable traces" in err
    code, _, err = cli("--policy", str(POLICIES / "support_agent.toml"), "--baseline-trace", "ab", current)
    assert code == 2 and "--baseline-trace needs --baseline" in err


def test_cli_json_output(cli: Cli) -> None:
    code, out, _ = cli("--policy", str(POLICIES / "support_agent.toml"), "--json", str(PAIRS / "refund_outage.jsonl"))
    assert code == 3
    data = json.loads(out)
    assert data["verdict"] == "fail" and data["runs_checked"] == 1 and data["runs_passed"] == 0
    (run,) = data["runs"]
    failed = {c["id"] for c in run["checks"] if c["result"] == "fail"}
    assert failed == {"run.require_success", "run.tools.issue_refund.max_failures"}
    for item in run["checks"]:
        assert set(item) == {"id", "result", "expected", "actual", "message", "evidence", "span_ids"}


def test_existing_commands_are_unchanged(cli: Cli, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["analyze", str(TRACES / "healthy_run.jsonl")]) == 0
    assert main(["compare", str(PAIRS / "refund_ok.jsonl"), str(PAIRS / "refund_ok_rerun.jsonl")]) == 0
    capsys.readouterr()
