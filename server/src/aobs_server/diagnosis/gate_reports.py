"""CI-native renderings of a :class:`~aobs_server.diagnosis.gate.GateReport`.

* :func:`render_gate_junit`: JUnit XML, the test-report format CI systems display natively
  (GitLab, Jenkins, Azure DevOps, CircleCI, Buildkite, and GitHub through test-report actions).
  One test suite for the gate-level checks and one per checked run; one test case per check.
* :func:`render_gate_markdown`: a Markdown summary for GitHub's job summary
  (``$GITHUB_STEP_SUMMARY``) or a pull-request comment.

Both say exactly what the gate decided. A check is reported as **failed** only when it counts
against the verdict, so a report is red if and only if the gate exit code is 3:

* a gate-level check that is FAIL or UNKNOWN fails (either one fails the gate);
* a run's FAIL check, and its UNKNOWN check under ``on_missing_data = "fail"``, fail when the gate
  failed. When the gate passed anyway (``min_pass_rate`` below 1 tolerates some failed runs) they
  are reported as skipped, with the reason;
* an UNKNOWN check under ``on_missing_data = "warn"`` is skipped, with the reason;
* a WARN gate check (``on_input_problems = "warn"``) passes, with the warning attached.

They hold what the text report holds and nothing more: content is never read, error messages
are secret-scrubbed. Output is deterministic (no timestamps), like the text and JSON reports.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Literal

from .gate import PRIVACY_NOTE, Check, GateReport, RunResult

Outcome = Literal["passed", "failed", "skipped"]
MAX_MARKDOWN_ROWS = 100  # GitHub keeps at most 1 MiB of job summary per step
_XML_INVALID = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]|#~])")
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")


@dataclass(frozen=True)
class _Case:
    check: Check
    outcome: Outcome
    note: str  # why a check is skipped, or the warning attached to a passing one


def _cases(report: GateReport, result: RunResult | None) -> list[_Case]:
    """Each check with how CI should count it (see the module docstring)."""
    cases = []
    if result is None:
        for check in report.gate_checks:
            if check.result in ("fail", "unknown"):
                cases.append(_Case(check, "failed", ""))
            elif check.result == "warn":
                cases.append(_Case(check, "passed", f'warning: {check.message} (on_input_problems = "warn")'))
            else:
                cases.append(_Case(check, "passed", ""))
        return cases
    missing_fails = report.policy.gate.on_missing_data == "fail"
    for check in result.checks:
        counts = check.result == "fail" or (check.result == "unknown" and missing_fails)
        if counts and report.verdict == "fail":
            cases.append(_Case(check, "failed", ""))
        elif counts:
            note = f"{check.result.upper()} but tolerated: the gate passed (min_pass_rate allows some failed runs)"
            cases.append(_Case(check, "skipped", note))
        elif check.result == "unknown":
            cases.append(
                _Case(check, "skipped", 'UNKNOWN (missing data) does not fail a run: on_missing_data = "warn"')
            )
        elif check.result == "warn":
            cases.append(_Case(check, "passed", f"warning: {check.message}"))
        else:
            cases.append(_Case(check, "passed", ""))
    return cases


def _run_label(result: RunResult) -> str:
    return f"run {result.run.trace_id[:12]} '{result.run.run_name or '(no root span)'}'"


def _details(check: Check, where: str) -> str:
    lines = [check.message, f"expected: {check.expected}", f"actual: {check.actual}"]
    if check.evidence:
        lines.append("evidence:")
        lines += [f"  - {item}" for item in check.evidence]
    if check.span_ids:
        lines.append(f"spans: {', '.join(check.span_ids)}")
    lines.append(where)
    return "\n".join(lines)


# --------------------------------------------------------------------------- JUnit XML


def _x(text: str) -> str:
    """Characters XML 1.0 cannot hold (most control characters) become U+FFFD."""
    return _XML_INVALID.sub("\ufffd", text)


def _suite(
    parent: ET.Element, name: str, classname: str, cases: list[_Case], where: str, props: dict[str, str]
) -> None:
    suite = ET.SubElement(
        parent,
        "testsuite",
        name=_x(name),
        tests=str(len(cases)),
        failures=str(sum(c.outcome == "failed" for c in cases)),
        errors="0",
        skipped=str(sum(c.outcome == "skipped" for c in cases)),
        time="0",
    )
    properties = ET.SubElement(suite, "properties")
    for key, value in props.items():
        ET.SubElement(properties, "property", name=key, value=_x(value))
    for case in cases:
        check = case.check
        element = ET.SubElement(suite, "testcase", classname=_x(classname), name=_x(check.id), time="0")
        if case.outcome == "failed":
            failure = ET.SubElement(element, "failure", message=_x(check.message), type=check.result)
            failure.text = _x(_details(check, where))
        elif case.outcome == "skipped":
            ET.SubElement(element, "skipped", message=_x(case.note))
            ET.SubElement(element, "system-out").text = _x(_details(check, where))
        elif case.note:
            ET.SubElement(element, "system-out").text = _x(f"{case.note}\n{_details(check, where)}")


def render_gate_junit(report: GateReport) -> str:
    gate_cases = _cases(report, None)
    run_cases = [(r, _cases(report, r)) for r in report.runs]
    every = gate_cases + [c for _, cases in run_cases for c in cases]
    root = ET.Element(
        "testsuites",
        name="aobs reliability gate",
        tests=str(len(every)),
        failures=str(sum(c.outcome == "failed" for c in every)),
        errors="0",
        skipped=str(sum(c.outcome == "skipped" for c in every)),
        time="0",
    )
    gate = report.policy.gate
    _suite(
        root,
        "aobs gate",
        "aobs.gate",
        gate_cases,
        f"gate-level check of policy {report.policy_path}",
        {
            "aobs.verdict": report.verdict,
            "aobs.policy": report.policy_path,
            "aobs.on_missing_data": gate.on_missing_data,
            "aobs.on_input_problems": gate.on_input_problems,
            "aobs.task": gate.task or "",
            "aobs.privacy": PRIVACY_NOTE,
        },
    )
    for result, cases in run_cases:
        run = result.run
        _suite(
            root,
            f"aobs gate: {_run_label(result)}",
            f"aobs.gate.run.{run.trace_id[:12]}",
            cases,
            f"{_run_label(result)} status={run.trace.status} in {run.source}",
            {
                "aobs.trace_id": run.trace_id,
                "aobs.run": run.run_name or "",
                "aobs.status": run.trace.status,
                "aobs.source": run.source,
                "aobs.run_verdict": result.verdict,
            },
        )
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


# --------------------------------------------------------------------------- Markdown


def _md(text: str) -> str:
    """Trace data as inert table text: no markup, links, HTML or line breaks."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = _CONTROL.sub(" ", text)  # line breaks and terminal escapes become spaces
    return _MARKDOWN_SPECIAL.sub(r"\\\1", " ".join(text.split()))


def _code(text: str) -> str:
    return f"`{text.replace('`', '')}`"


def _why(check: Check) -> str:
    parts = [check.message, *check.evidence[:3]]
    if len(check.evidence) > 3:
        parts.append(f"… {len(check.evidence) - 3} more")
    return "; ".join(parts)


def render_gate_markdown(report: GateReport) -> str:
    gate = report.policy.gate
    passed = sum(r.verdict == "pass" for r in report.runs)
    lines = [f"### aobs reliability gate: **{report.verdict.upper()}**", ""]
    settings = f"missing data: {gate.on_missing_data} · input problems: {gate.on_input_problems}"
    if gate.task:
        settings += f" · task: {_md(gate.task)}"
    lines.append(f"{passed} of {len(report.runs)} run(s) passed · policy {_code(report.policy_path)} · {settings}")
    if report.baseline is not None:
        b = report.baseline
        lines.append("")
        lines.append(f"Baseline: run {_code(b.trace_id[:12])} {_md(b.run_name or '')} ({_md(b.source)})")

    failed: list[tuple[str, _Case]] = [("gate", c) for c in _cases(report, None) if c.outcome == "failed"]
    noted: list[tuple[str, _Case]] = [("gate", c) for c in _cases(report, None) if c.note]
    for result in report.runs:
        where = f"run {_code(result.run.trace_id[:12])} {_md(result.run.run_name or '')}"
        for case in _cases(report, result):
            if case.outcome == "failed":
                failed.append((where, case))
            elif case.note:
                noted.append((where, case))

    if failed:
        lines += ["", "#### Failed checks", "", "| Where | Check | Expected | Actual | Why |", "|---|---|---|---|---|"]
        for where, case in failed[:MAX_MARKDOWN_ROWS]:
            c = case.check
            lines.append(f"| {where} | {_code(c.id)} | {_md(c.expected)} | {_md(c.actual)} | {_md(_why(c))} |")
        if len(failed) > MAX_MARKDOWN_ROWS:
            lines += ["", f"… and {len(failed) - MAX_MARKDOWN_ROWS} more failed checks (see the JUnit or JSON report)."]
    if noted:
        lines += ["", "#### Not counted against the gate", ""]
        for where, case in noted[:MAX_MARKDOWN_ROWS]:
            lines.append(f"- {where} {_code(case.check.id)}: {_md(case.note)}. {_md(_why(case.check))}")
        if len(noted) > MAX_MARKDOWN_ROWS:
            lines.append(f"- … and {len(noted) - MAX_MARKDOWN_ROWS} more")

    if report.runs:
        header = "| Run | Task | Status | Verdict | Checks not passed | Input |"
        lines += ["", "#### Runs", "", header, "|---|---|---|---|---|---|"]
        for result in report.runs[:MAX_MARKDOWN_ROWS]:
            run = result.run
            not_passed = ", ".join(_code(c.id) for c in result.checks if c.result in ("fail", "unknown")) or "-"
            lines.append(
                f"| {_code(run.trace_id[:12])} | {_md(run.run_name or '(no root span)')} | {_md(run.trace.status)} "
                f"| {result.verdict.upper()} | {not_passed} | {_md(run.source)} |"
            )
        if len(report.runs) > MAX_MARKDOWN_ROWS:
            lines += ["", f"… and {len(report.runs) - MAX_MARKDOWN_ROWS} more runs."]
    if report.excluded_runs:
        lines += ["", f"{report.excluded_runs} run(s) of another task were not checked."]
    lines += ["", f"_{_md(PRIVACY_NOTE)}_", ""]
    return "\n".join(lines)
