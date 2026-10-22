"""The GitHub Action that packages the reliability gate (``actions/gate``): its shell steps and
its pull-request comment script, run here with the environment the Actions runner would set
(``GITHUB_OUTPUT``, ``GITHUB_STEP_SUMMARY``, the event payload) simulated in temporary files.

``gh`` is replaced by a local fake that records its arguments and answers from a script, so no
request reaches GitHub. The action's install step (pip from the checkout) needs a package index
and is not run here."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from test_compare import rewrite

from support import REPO_ROOT

ACTION = REPO_ROOT / "actions" / "gate"
GATE_SH = ACTION / "gate.sh"
COMMENT_PY = ACTION / "comment.py"
GATE_BIN = Path(sys.executable).with_name("aobs-server")  # what the install step reports
POLICIES = REPO_ROOT / "examples" / "diagnose" / "gate"
PAIRS = REPO_ROOT / "examples" / "diagnose" / "compare"
SUPPORT_POLICY = "examples/diagnose/gate/support_agent.toml"
TOKEN = "ghs_not-a-real-token-0123456789"
REPO = "octo/agent"


def outputs(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if line)


def gate_step(tmp_path: Path, command: str, **env: str) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    """Run ``gate.sh COMMAND`` from the repository root, as the runner runs the composite step."""
    out = tmp_path / f"{command}.out"
    out.unlink(missing_ok=True)
    full = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(out),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "GITHUB_ACTION_PATH": str(ACTION),
        "AOBS_GATE_BIN": str(GATE_BIN),
        **env,
    }
    proc = subprocess.run(
        ["bash", str(GATE_SH), command], cwd=REPO_ROOT, env=full, capture_output=True, text=True, timeout=120
    )
    return proc, outputs(out)


def run_and_verdict(tmp_path: Path, **env: str) -> tuple[dict[str, str], subprocess.CompletedProcess[str]]:
    proc, out = gate_step(tmp_path, "run", **env)
    assert proc.returncode == 0, proc.stdout + proc.stderr  # the run step itself never fails on a verdict
    fail_on = {"AOBS_FAIL_ON_REGRESSION": env["AOBS_FAIL_ON_REGRESSION"]} if "AOBS_FAIL_ON_REGRESSION" in env else {}
    verdict, _ = gate_step(
        tmp_path, "verdict", AOBS_RESULT=out.get("result", ""), AOBS_EXIT_CODE=out.get("exit-code", ""), **fail_on
    )
    return out, verdict


# --------------------------------------------------------------------------- action.yml


def test_no_expression_is_interpolated_into_a_run_script() -> None:
    """Inputs reach the scripts through env; a `run:` line never holds `${{ }}` (script injection)."""
    text = (ACTION / "action.yml").read_text()
    runs = [line for line in text.splitlines() if re.match(r"\s+run:", line)]
    assert len(runs) == 4
    for line in runs:
        assert "${{" not in line and not line.rstrip().endswith(("|", ">", "|-", ">-")), line
    for name in ("policy", "traces", "baseline", "project", "comment-on-pr", "fail-on-regression"):
        assert f"${{{{ inputs.{name} }}}}" in text  # each input is wired to an env variable
    assert "update-environment: false" in text  # setting up Python does not change the job's PATH


def test_the_example_workflow_uses_only_inputs_the_action_has() -> None:
    action = (ACTION / "action.yml").read_text()
    inputs = set(re.findall(r"^  ([a-z-]+):\n    description:", action.split("\noutputs:")[0], re.M))
    assert {"policy", "traces", "comment-on-pr", "fail-on-regression", "python-version"} <= inputs
    example = (REPO_ROOT / "examples" / "ci" / "github-actions-agent-gate-action.yml").read_text()
    step = example.split("uses: AshmitJain932/ai-agent-observability/actions/gate@", 1)[1].split("\n      - ", 1)[0]
    used = set(re.findall(r"^          ([a-z-]+):", step, re.M))
    assert {"policy", "traces"} <= used <= inputs
    assert "steps.gate.outputs.junit-path" in example and "pull-requests: write" in example


# --------------------------------------------------------------------------- gate.sh run / verdict


def test_passing_traces_pass_and_the_summary_is_the_gate_markdown(tmp_path: Path) -> None:
    out, verdict = run_and_verdict(
        tmp_path, AOBS_POLICY=SUPPORT_POLICY, AOBS_TRACES="\n  examples/diagnose/compare/refund_ok*.jsonl  \n\n"
    )
    assert out["result"] == "pass" and out["exit-code"] == "0"
    markdown = Path(out["markdown-path"]).read_text()
    assert markdown.startswith("### aobs reliability gate: **PASS**")
    assert "2 of 2 run(s) passed" in markdown  # the glob found refund_ok.jsonl and refund_ok_rerun.jsonl
    assert (tmp_path / "summary.md").read_text() == markdown
    assert "<testsuites" in Path(out["junit-path"]).read_text()
    assert Path(out["junit-path"]).parent.parent == tmp_path  # a new directory under RUNNER_TEMP
    assert verdict.returncode == 0 and "PASS" in verdict.stdout


def test_a_regression_fails_the_step_unless_fail_on_regression_is_false(tmp_path: Path) -> None:
    traces = "examples/diagnose/compare/refund_outage.jsonl"
    out, verdict = run_and_verdict(tmp_path, AOBS_POLICY=SUPPORT_POLICY, AOBS_TRACES=traces)
    assert out["result"] == "fail" and out["exit-code"] == "3"
    assert verdict.returncode == 3 and "::error title=aobs gate::the reliability gate failed" in verdict.stdout
    summary = (tmp_path / "summary.md").read_text()
    assert summary.startswith("### aobs reliability gate: **FAIL**") and "`run.require_success`" in summary

    out, verdict = run_and_verdict(
        tmp_path, AOBS_POLICY=SUPPORT_POLICY, AOBS_TRACES=traces, AOBS_FAIL_ON_REGRESSION="false"
    )
    assert out["result"] == "fail"
    assert verdict.returncode == 0 and "::notice" in verdict.stdout


def test_baseline_inputs_reach_the_gate(tmp_path: Path) -> None:
    common = {
        "AOBS_POLICY": "examples/diagnose/gate/restock_baseline.toml",
        "AOBS_BASELINE": "examples/diagnose/compare/inventory_v1.jsonl",
        "AOBS_REPORT_DIR": str(tmp_path / "reports"),
    }
    out, _ = run_and_verdict(tmp_path, AOBS_TRACES="examples/diagnose/compare/inventory_v2.jsonl", **common)
    assert out["result"] == "fail" and out["junit-path"] == str(tmp_path / "reports" / "agent-gate.xml")
    assert "baseline." in Path(out["markdown-path"]).read_text()
    out, _ = run_and_verdict(tmp_path, AOBS_TRACES="examples/diagnose/compare/inventory_v1.jsonl", **common)
    assert out["result"] == "pass"
    common["AOBS_BASELINE"] = ""
    _, out = gate_step(tmp_path, "run", AOBS_TRACES="examples/diagnose/compare/inventory_v2.jsonl", **common)
    assert out["result"] == "error" and out["exit-code"] == "2"  # [baseline] needs --baseline


def test_no_verdict_is_an_error_whatever_fail_on_regression_says(tmp_path: Path) -> None:
    out, verdict = run_and_verdict(
        tmp_path, AOBS_POLICY=SUPPORT_POLICY, AOBS_TRACES="traces/missing.jsonl", AOBS_FAIL_ON_REGRESSION="false"
    )
    assert out["result"] == "error" and out["exit-code"] == "1"
    assert verdict.returncode == 1 and "missing input" in verdict.stdout
    summary = (tmp_path / "summary.md").read_text()
    assert summary.startswith("### aobs reliability gate: **ERROR**") and "exit code 1" in summary


def test_invalid_inputs_are_configuration_errors(tmp_path: Path) -> None:
    proc, _ = gate_step(tmp_path, "run", AOBS_POLICY=SUPPORT_POLICY, AOBS_TRACES="x", AOBS_COMMENT_ON_PR="yes")
    assert proc.returncode == 2 and "input comment-on-pr must be true or false" in proc.stdout
    proc, _ = gate_step(tmp_path, "run", AOBS_POLICY=SUPPORT_POLICY, AOBS_TRACES=" \n ")
    assert proc.returncode == 2 and "input traces is empty" in proc.stdout
    proc, _ = gate_step(tmp_path, "run", AOBS_TRACES="x")
    assert proc.returncode == 2 and "input policy is required" in proc.stdout


def test_file_names_are_never_split_or_read_as_options(tmp_path: Path) -> None:
    odd = tmp_path / "runs dir" / "--json $(touch pwned).jsonl"
    odd.parent.mkdir()
    shutil.copy(PAIRS / "refund_ok.jsonl", odd)
    out, _ = run_and_verdict(tmp_path, AOBS_POLICY=SUPPORT_POLICY, AOBS_TRACES=f"{tmp_path}/runs dir/*.jsonl")
    assert out["result"] == "pass"
    assert not (REPO_ROOT / "pwned").exists() and not (tmp_path / "pwned").exists()
    out, _ = run_and_verdict(tmp_path, AOBS_POLICY=SUPPORT_POLICY, AOBS_TRACES=str(odd))
    assert out["result"] == "pass"


def test_trace_data_cannot_issue_workflow_commands(tmp_path: Path) -> None:
    """The gate prints run names; a name holding a line break and ``::error::`` stays inert text."""

    def edit(event: dict[str, Any]) -> dict[str, Any]:
        if event.get("parent_span_id") is None and "name" in event:
            event["name"] = "x\n::error::injected\n::add-mask::y"
        return event

    hostile = rewrite(PAIRS / "refund_outage.jsonl", tmp_path / "hostile.jsonl", edit)
    policy = tmp_path / "policy.toml"
    policy.write_text("version = 1\n[run]\nrequire_success = true\n")
    proc, out = gate_step(tmp_path, "run", AOBS_POLICY=str(policy), AOBS_TRACES=str(hostile))
    assert out["result"] == "fail" and "::error::injected" in proc.stdout
    stop = re.search(r"^::stop-commands::(aobs-[0-9a-f]{32})$", proc.stdout, re.M)
    assert stop is not None
    resume = proc.stdout.index(f"\n::{stop.group(1)}::")
    assert stop.end() < proc.stdout.index("::error::injected") < resume
    markdown = Path(out["markdown-path"]).read_text()
    assert "x ::error::injected" in markdown  # line breaks became spaces: no line starts with ::
    assert not any(line.startswith("::") for line in markdown.splitlines())


# --------------------------------------------------------------------------- comment.py

FAKE_GH = """\
import json, os, sys
log = os.environ["FAKE_GH_LOG"]
with open(log, "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read(), "token": os.environ.get("GH_TOKEN")}) + "\\n")
fail = os.environ.get("FAKE_GH_FAIL")
if fail:
    sys.stderr.write(fail + "\\n")
    sys.exit(1)
if "--paginate" in sys.argv:
    for item in json.loads(os.environ.get("FAKE_GH_COMMENTS", "[]")):
        print(json.dumps(item))
else:
    print("https://github.com/octo/agent/pull/7#issuecomment-99")
"""


def comment(
    tmp_path: Path,
    *,
    event: str = "pull_request",
    head_repo: str | None = REPO,
    markdown: str = "### aobs reliability gate: **FAIL**\n\n| a | b |\n",
    result: str = "fail",
    with_gh: bool = True,
    **env: str,
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]], dict[str, str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if with_gh:
        gh = bin_dir / "gh"
        gh.write_text(f"#!{sys.executable}\n{FAKE_GH}")
        gh.chmod(0o755)
    payload = {"pull_request": {"number": 7, "head": {"repo": {"full_name": head_repo} if head_repo else None}}}
    (tmp_path / "event.json").write_text(json.dumps(payload))
    (tmp_path / "gate.md").write_text(markdown)
    log, out = tmp_path / "gh.log", tmp_path / "comment.out"
    log.unlink(missing_ok=True)
    out.unlink(missing_ok=True)
    full = {
        "PATH": str(bin_dir),
        "GITHUB_EVENT_NAME": event,
        "GITHUB_EVENT_PATH": str(tmp_path / "event.json"),
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ID": "123",
        "GITHUB_OUTPUT": str(out),
        "GH_TOKEN": TOKEN,
        "AOBS_RESULT": result,
        "AOBS_EXIT_CODE": "3",
        "AOBS_MARKDOWN_PATH": str(tmp_path / "gate.md"),
        "FAKE_GH_LOG": str(log),
        **env,
    }
    proc = subprocess.run(
        [sys.executable, "-I", str(COMMENT_PY)], env=full, capture_output=True, text=True, timeout=120
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    assert TOKEN not in proc.stdout + proc.stderr  # the token is never printed
    for call in calls:
        assert TOKEN not in json.dumps(call["argv"]) + call["stdin"]  # only gh's environment holds it
    return proc, calls, outputs(out)


def test_a_new_comment_is_posted_with_the_gate_markdown(tmp_path: Path) -> None:
    proc, calls, out = comment(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [c["argv"][:4] for c in calls] == [
        ["api", "--paginate", f"repos/{REPO}/issues/7/comments?per_page=100", "--jq"],
        ["api", "--method", "POST", f"repos/{REPO}/issues/7/comments"],
    ]
    assert calls[1]["token"] == TOKEN
    body = json.loads(calls[1]["stdin"])["body"]
    assert body.startswith("<!-- aobs-reliability-gate:default -->\n### aobs reliability gate: **FAIL**")
    assert "[Workflow run](https://github.com/octo/agent/actions/runs/123)" in body
    assert out["comment-url"] == "https://github.com/octo/agent/pull/7#issuecomment-99"


def test_the_sticky_comment_is_updated_not_duplicated(tmp_path: Path) -> None:
    mine = "<!-- aobs-reliability-gate:default -->\n### old"
    comments = [
        {"id": 5, "login": "mallory", "head": mine},  # someone else's comment with the marker
        {"id": 6, "login": "github-actions[bot]", "head": "<!-- aobs-reliability-gate:other -->\n"},
        {"id": 8, "login": "github-actions[bot]", "head": mine},
    ]
    proc, calls, _ = comment(tmp_path, FAKE_GH_COMMENTS=json.dumps(comments))
    assert proc.returncode == 0
    assert calls[1]["argv"][:4] == ["api", "--method", "PATCH", f"repos/{REPO}/issues/comments/8"]
    proc, calls, _ = comment(tmp_path, FAKE_GH_COMMENTS=json.dumps(comments), AOBS_COMMENT_TAG="other")
    assert calls[1]["argv"][:4] == ["api", "--method", "PATCH", f"repos/{REPO}/issues/comments/6"]


def test_no_comment_outside_pull_requests_or_from_forks(tmp_path: Path) -> None:
    proc, calls, _ = comment(tmp_path, event="push")
    assert proc.returncode == 0 and calls == [] and "::notice" in proc.stdout
    proc, calls, _ = comment(tmp_path, head_repo="someone/fork")
    assert proc.returncode == 0 and calls == [] and "fork" in proc.stdout
    proc, calls, _ = comment(tmp_path, head_repo=None)  # the fork was deleted
    assert proc.returncode == 0 and calls == []
    proc, calls, _ = comment(tmp_path, event="pull_request_target", head_repo="someone/fork")
    assert len(calls) == 2  # pull_request_target runs with the base repository's token


def test_a_read_only_token_or_missing_gh_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    proc, calls, _ = comment(tmp_path, FAKE_GH_FAIL="gh: Resource not accessible by integration (HTTP 403)")
    assert proc.returncode == 0 and len(calls) == 1
    assert "::warning" in proc.stdout and "pull-requests: write" in proc.stdout
    proc, _, _ = comment(tmp_path, FAKE_GH_FAIL="::error::boom\nmore")
    assert proc.returncode == 0 and "::error::" not in proc.stdout  # gh's message cannot issue commands
    proc, calls, _ = comment(tmp_path, with_gh=False, PATH=str(tmp_path / "empty"))
    assert proc.returncode == 0 and calls == [] and "gh command-line tool is not installed" in proc.stdout
    proc, calls, _ = comment(tmp_path, AOBS_COMMENT_TAG="bad tag -->")
    assert proc.returncode == 2 and calls == []


def test_the_comment_cannot_mention_users_and_stays_within_the_size_limit(tmp_path: Path) -> None:
    _, calls, _ = comment(tmp_path, markdown="### aobs reliability gate: **FAIL**\n\nrun @octo-org/team\n")
    body = json.loads(calls[1]["stdin"])["body"]
    assert "@octo-org" not in body and "@\u200bocto-org/team" in body
    rows = "".join(f"| row {i} | {'x' * 100} |\n" for i in range(2000))
    _, calls, _ = comment(tmp_path, markdown=f"### aobs reliability gate: **FAIL**\n\n{rows}")
    body = json.loads(calls[1]["stdin"])["body"]
    assert len(body) <= 60_000 and "the report was cut here" in body and body.rstrip().endswith("runs/123)")
    _, calls, _ = comment(tmp_path, result="error", markdown="ignored")
    body = json.loads(calls[1]["stdin"])["body"]
    assert "**ERROR**" in body and "exit code 3" in body and "ignored" not in body
