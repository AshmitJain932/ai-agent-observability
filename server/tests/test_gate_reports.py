"""JUnit XML and Markdown reports of the reliability gate (``gate --junit/--markdown``): they agree
with the gate's verdict, carry expected/actual/evidence, stay well-formed for any trace data, and
never show content."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from test_compare import rewrite
from test_gate import NOW, PAIRS, POLICIES, SENSITIVE, TRACES, gate

from aobs_server.cli import main
from aobs_server.diagnosis import gate_reports
from aobs_server.diagnosis.gate import GateReport, build_gate_report, load_policy
from aobs_server.diagnosis.gate_reports import render_gate_junit, render_gate_markdown
from support import REPO_ROOT

OK = PAIRS / "refund_ok.jsonl"
OUTAGE = PAIRS / "refund_outage.jsonl"


def junit(report: GateReport) -> ET.Element:
    return ET.fromstring(render_gate_junit(report))


def cases(root: ET.Element) -> dict[tuple[str, str], ET.Element]:
    return {(c.get("classname", ""), c.get("name", "")): c for c in root.iter("testcase")}


def outcome(case: ET.Element) -> str:
    if case.find("failure") is not None:
        return "failed"
    return "skipped" if case.find("skipped") is not None else "passed"


def counts(root: ET.Element) -> dict[str, int]:
    return {k: int(root.get(k, "-1")) for k in ("tests", "failures", "errors", "skipped")}


def policy_report(*paths: Path, policy: Path = POLICIES / "support_agent.toml") -> GateReport:
    return build_gate_report(load_policy(policy), list(paths), NOW, policy_path=str(policy))


# --------------------------------------------------------------------------- JUnit


def test_a_failed_gate_is_a_failed_junit_report_with_expected_and_actual() -> None:
    report = policy_report(OK, OUTAGE)
    assert report.verdict == "fail"
    root = junit(report)
    by_id = cases(root)
    assert counts(root) == {"tests": len(by_id), "failures": 3, "errors": 0, "skipped": 0}
    failed = {name for (_, name), case in by_id.items() if outcome(case) == "failed"}
    assert failed == {"gate.pass_rate", "run.require_success", "run.tools.issue_refund.max_failures"}
    outage = report.runs[1].run.trace_id[:12]
    failure = by_id[(f"aobs.gate.run.{outage}", "run.require_success")].find("failure")
    assert failure is not None and failure.get("type") == "fail"
    assert failure.get("message") == "the run ended with status error"
    text = failure.text or ""
    assert "expected: ok or ok_with_errors" in text and "actual: error" in text
    assert "root cause: tool 'issue_refund' failed with TimeoutError" in text and str(OUTAGE) in text
    suites = root.findall("testsuite")
    assert suites[0].get("name") == "aobs gate"
    assert int(suites[0].get("failures", "0")) == 1  # gate.pass_rate
    props = {p.get("name"): p.get("value") for p in suites[2].iter("property")}
    assert props["aobs.trace_id"] == report.runs[1].run.trace_id and props["aobs.run_verdict"] == "fail"


def test_a_passing_gate_has_no_failures() -> None:
    root = junit(policy_report(OK))
    assert counts(root)["failures"] == 0 and counts(root)["skipped"] == 0 and counts(root)["tests"] == 14


def test_failures_tolerated_by_the_pass_rate_are_skipped_not_failed() -> None:
    report = gate({"require_success": True}, OK, OUTAGE, gate={"min_pass_rate": 0.5})
    assert report.verdict == "pass" and report.runs[1].verdict == "fail"
    root = junit(report)
    assert counts(root)["failures"] == 0 and counts(root)["skipped"] == 1
    (skipped,) = [c for c in root.iter("testcase") if outcome(c) == "skipped"]
    assert skipped.get("name") == "run.require_success"
    note = skipped.find("skipped")
    assert note is not None and "tolerated" in (note.get("message") or "")
    markdown = render_gate_markdown(report)
    assert "#### Failed checks" not in markdown and "#### Not counted against the gate" in markdown


def test_missing_data_follows_on_missing_data() -> None:
    requirements = {"max_cost": "0.01", "cost_currency": "USD"}  # the sample runs report no cost
    strict = gate(requirements, OK)
    assert strict.verdict == "fail"
    assert counts(junit(strict))["failures"] == 2  # the run's UNKNOWN check and gate.pass_rate
    lenient = gate(requirements, OK, gate={"on_missing_data": "warn"})
    assert lenient.verdict == "pass"
    root = junit(lenient)
    assert counts(root)["failures"] == 0 and counts(root)["skipped"] == 1
    note = next(c for c in root.iter("testcase") if outcome(c) == "skipped").find("skipped")
    assert note is not None and 'on_missing_data = "warn"' in (note.get("message") or "")


def test_input_problems_under_warn_pass_with_the_warning_attached(tmp_path: Path) -> None:
    broken = tmp_path / "broken.jsonl"
    broken.write_text("not json\n" + OK.read_text())
    report = gate({"require_success": True}, broken, gate={"on_input_problems": "warn"})
    assert report.verdict == "pass"
    root = junit(report)
    assert counts(root)["failures"] == 0
    (case,) = [c for c in root.iter("testcase") if c.get("name") == "gate.input_valid"]
    out = case.find("system-out")
    assert outcome(case) == "passed" and out is not None and "warning:" in (out.text or "")


@pytest.mark.parametrize(
    "requirements, sections",
    [
        ({"require_success": True}, {}),
        ({"require_success": True}, {"gate": {"min_pass_rate": 0.5}}),
        ({"max_cost": "1", "cost_currency": "USD"}, {}),
        ({"max_cost": "1", "cost_currency": "USD"}, {"gate": {"on_missing_data": "warn"}}),
        ({"max_tool_calls": 0}, {"gate": {"min_runs": 3}}),
        ({"required_tools": ["get_order_status"]}, {"gate": {"task": "restock_check"}}),
    ],
)
def test_the_junit_report_is_red_exactly_when_the_gate_fails(requirements: dict[str, Any], sections: Any) -> None:
    report = gate(requirements, OK, OUTAGE, PAIRS / "inventory_v1.jsonl", **sections)
    failures = counts(junit(report))["failures"]
    assert (failures > 0) == (report.verdict == "fail"), (report.verdict, failures)
    assert render_gate_markdown(report).startswith(f"### aobs reliability gate: **{report.verdict.upper()}**")


# --------------------------------------------------------------------------- hostile trace data


def hostile(tmp_path: Path) -> Path:
    name = 'refund <script>alert(1)</script> | ]]> & "q" \x01\x1b[31m\nnext # *x* [l](http://e)'

    def edit(event: dict[str, Any]) -> dict[str, Any]:
        if event.get("parent_span_id") is None and "name" in event:
            event["name"] = name
        return event

    return rewrite(OUTAGE, tmp_path / "hostile.jsonl", edit)


def test_any_trace_data_gives_well_formed_xml_and_inert_markdown(tmp_path: Path) -> None:
    report = gate({"require_success": True, "max_tool_calls": 0}, hostile(tmp_path))
    assert report.runs[0].run.run_name is not None and "<script>" in report.runs[0].run.run_name
    xml = render_gate_junit(report)
    root = ET.fromstring(xml)  # well-formed although the name holds ]]>, & and control characters
    assert "\x01" not in xml and "\x1b" not in xml and "�" in xml
    assert any("<script>" in (s.get("name") or "") for s in root.iter("testsuite"))  # kept as text
    markdown = render_gate_markdown(report)
    assert "<script>" not in markdown and "&lt;script&gt;" in markdown
    assert re.search(r"(?<!\\)\]\(", markdown) is None  # no link: every bracket is escaped
    assert "\x01" not in markdown and "\x1b" not in markdown
    rows = [line for line in markdown.splitlines() if line.startswith("| ")]
    assert rows
    for row in rows:  # an escaped \| is text; a row keeps its 5 (failed checks) or 6 (runs) columns
        assert row.replace("\\|", "").count("|") in (6, 7), row


def test_reports_never_show_content() -> None:
    files = sorted(TRACES.glob("*.jsonl"))
    raw = "".join(p.read_text() for p in files)
    assert all(secret in raw for secret in SENSITIVE)  # the inputs really contain them
    report = gate({"require_success": True, "max_redundant_calls": 0, "max_retries": 0}, *files)
    for text in (render_gate_junit(report), render_gate_markdown(report)):
        for secret in SENSITIVE:
            assert secret not in text, secret


def test_reports_are_deterministic_and_markdown_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    report = policy_report(OK, OUTAGE, *sorted(TRACES.glob("*.jsonl")))
    assert render_gate_junit(report) == render_gate_junit(policy_report(OK, OUTAGE, *sorted(TRACES.glob("*.jsonl"))))
    monkeypatch.setattr(gate_reports, "MAX_MARKDOWN_ROWS", 1)
    markdown = render_gate_markdown(report)
    assert "more failed checks (see the JUnit or JSON report)" in markdown and "more runs." in markdown


# --------------------------------------------------------------------------- CLI


def test_cli_writes_both_reports_and_keeps_the_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    policy = str(POLICIES / "support_agent.toml")
    xml, md = tmp_path / "reports" / "nested" / "gate.xml", tmp_path / "summary.md"
    assert main(["gate", "--policy", policy, str(OK), "--junit", str(xml), "--markdown", str(md)]) == 0
    assert "aobs reliability gate: PASS" in capsys.readouterr().out
    assert counts(ET.parse(xml).getroot())["failures"] == 0
    assert md.read_text().startswith("### aobs reliability gate: **PASS**")
    assert main(["gate", "--json", "--policy", policy, str(OUTAGE), "--junit", str(xml), "--markdown", str(md)]) == 3
    assert json.loads(capsys.readouterr().out)["verdict"] == "fail"  # stdout stays pure JSON
    assert counts(ET.parse(xml).getroot())["failures"] == 3
    assert "| `run.require_success` |" in md.read_text()


def test_cli_reports_an_unwritable_report_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    code = main(["gate", "--policy", str(POLICIES / "support_agent.toml"), str(OK), "--junit", str(blocker / "x.xml")])
    assert code == 1 and "cannot write" in capsys.readouterr().err


# --------------------------------------------------------------------------- the CI template

TEMPLATE = REPO_ROOT / "examples" / "ci" / "github-actions-agent-gate.yml"
RECORD = "uv run python examples/support_agent/run.py --scenario {s} --out traces/{s}.jsonl --quiet"
GATE = (
    "uv run aobs-server gate --policy examples/diagnose/gate/support_agent.toml traces/*.jsonl\n"
    '          --junit reports/agent-gate.xml --markdown "$GITHUB_STEP_SUMMARY"'
)


def test_the_ci_template_runs_and_passes(tmp_path: Path) -> None:
    """The template's commands, run here: record the example agent's runs with the real SDK (mock
    model, no API key), then gate them with both reports. Keeps the template from going stale."""
    template = TEMPLATE.read_text()
    scenarios = ("success", "refund")
    for scenario in scenarios:
        assert RECORD.format(s=scenario) in template
    assert GATE in template
    traces = tmp_path / "traces"
    traces.mkdir()
    for scenario in scenarios:
        subprocess.run(
            [sys.executable, "examples/support_agent/run.py", "--scenario", scenario,
             "--out", str(traces / f"{scenario}.jsonl"), "--quiet"],
            cwd=REPO_ROOT, check=True, capture_output=True, timeout=120,
        )  # fmt: skip
    xml, summary = tmp_path / "reports" / "agent-gate.xml", tmp_path / "summary.md"
    files = [str(p) for p in sorted(traces.glob("*.jsonl"))]
    argv = ["gate", "--policy", str(POLICIES / "support_agent.toml"), *files]
    assert main([*argv, "--junit", str(xml), "--markdown", str(summary)]) == 0
    root = ET.parse(xml).getroot()
    assert counts(root)["failures"] == 0 and len(root.findall("testsuite")) == 1 + len(scenarios)
    assert summary.read_text().startswith("### aobs reliability gate: **PASS**")


def test_xml_cleanup_replaces_exactly_what_xml_cannot_hold() -> None:
    assert gate_reports._x("a\x00b\x1bc\ud800d￾e") == "a�b�c�d�e"
    assert gate_reports._x("tab\tnl\ncr\r é ✓ 😀") == "tab\tnl\ncr\r é ✓ 😀"


def test_a_file_name_that_is_not_utf8_does_not_break_the_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    try:
        name = os.fsdecode(os.path.join(os.fsencode(tmp_path), b"outage-\xff.jsonl"))
        shutil.copy(OUTAGE, name)
    except (OSError, UnicodeError):
        pytest.skip("this file system does not accept file names that are not UTF-8")
    xml, md = tmp_path / "gate.xml", tmp_path / "gate.md"
    argv = ["gate", "--policy", str(POLICIES / "support_agent.toml"), name, "--junit", str(xml), "--markdown", str(md)]
    assert main(argv) == 3
    capsys.readouterr()
    assert counts(ET.parse(xml).getroot())["failures"] == 3
    assert "outage-�.jsonl" in md.read_text(encoding="utf-8")
