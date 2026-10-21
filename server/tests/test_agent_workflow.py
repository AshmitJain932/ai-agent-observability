"""The whole workflow with the example support agent, as docs/agent-workflow.md describes it:

the agent runs (scripted mock model, no API key, no network beyond localhost) and sends its
telemetry with the SDK's HttpExporter to the collector on a real local port; the collector stores
it in real PostgreSQL; the CLI lists, analyzes, compares and gates the stored runs; the dashboard
shows them. Each scenario runs once, in its own process, exactly as ``run.py`` is run by hand.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

import pg
from aobs_server.api import create_app
from aobs_server.cli import main
from aobs_server.config import Settings
from aobs_server.dashboard import DashboardConfig
from aobs_server.dashboard import create_app as create_dashboard
from aobs_server.storage.db import make_engine
from ingest_support import LiveServer, database_text, make_collector
from support import REPO_ROOT
from tenants import make_key

POLICY = str(REPO_ROOT / "examples" / "diagnose" / "gate" / "support_agent.toml")
QUESTION = "Please refund order 1042, it arrived broken."  # content: must never be stored
TOKEN = "agent-workflow-dashboard-token"
# ticket (= session id) -> (run.py arguments, expected exit code)
SCENARIOS: dict[str, tuple[tuple[str, ...], int]] = {
    "ticket-ok": (("--scenario", "refund"), 0),
    "ticket-refund-down": (("--scenario", "refund-outage"), 1),
    "ticket-model-down": (("--scenario", "model-outage"), 1),
    "ticket-no-usage": (("--scenario", "refund", "--no-usage"), 0),
    "ticket-full-capture": (("--scenario", "refund", "--capture", "full"), 0),
}


@dataclass
class Workflow:
    engine: Engine
    url: str  # the collector
    database_url: str
    project: str
    key: str
    agent_output: dict[str, subprocess.CompletedProcess[str]]


@pytest.fixture(scope="module")
def workflow(pg_server: pg.PgServer, migrated_template: str) -> Iterator[Workflow]:
    name = pg_server.create_database(template=migrated_template)
    url = pg_server.sqlalchemy_url(name)
    engine = make_engine(url, pool_size=4)
    try:
        # The production default: the project stores metadata only, whatever the SDK captures.
        collector = make_collector(engine, name="support", capture_ceiling="metadata_only", stack_traces=False)
        env = {**os.environ, "AOBS_API_KEY": collector.key, "NO_PROXY": "127.0.0.1,localhost",
               "no_proxy": "127.0.0.1,localhost"}  # fmt: skip
        with LiveServer(create_app(Settings(), engine)) as server:  # the system clock, as `serve` uses
            outputs = {
                ticket: subprocess.run(
                    [
                        sys.executable,
                        "examples/support_agent/run.py",
                        *args,
                        "--ticket",
                        ticket,
                        "--endpoint",
                        server.url,
                    ],
                    cwd=REPO_ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                for ticket, (args, _) in SCENARIOS.items()
            }
            run = Workflow(
                engine, server.url, url.render_as_string(hide_password=False), str(collector.project_id),
                collector.key, outputs,
            )  # fmt: skip
            yield run
    finally:
        engine.dispose()
        pg_server.drop_database(name)


@pytest.fixture
def cli(workflow: Workflow, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Any:
    monkeypatch.setenv("AOBS_DATABASE_URL", workflow.database_url)

    def run(*argv: str) -> tuple[int, str, str]:
        code = main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err

    return run


def listed(cli: Any, workflow: Workflow, ticket: str) -> dict[str, Any]:
    code, out, _ = cli("traces", "list", "--json", "--project", workflow.project, f"db:session={ticket}")
    assert code == 0
    (trace,) = json.loads(out)["traces"]
    return dict(trace)


def read_trace(workflow: Workflow, trace_id: str) -> dict[str, Any]:
    """The stored trace through GET /v1/projects/{id}/traces/{trace_id} with a read-scoped key."""
    key = make_key(workflow.engine, UUID(workflow.project), scopes=("read",))[1]
    request = urllib.request.Request(
        f"{workflow.url}/v1/projects/{workflow.project}/traces/{trace_id}",
        headers={"Authorization": f"Bearer {key}"},
    )
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
        return dict(json.loads(response.read()))


# --------------------------------------------------------------------------- capture and storage


def test_every_run_is_sent_and_stored_once_with_its_outcome(cli: Any, workflow: Workflow) -> None:
    expected_status = {"ticket-ok": "ok", "ticket-refund-down": "error", "ticket-model-down": "error",
                       "ticket-no-usage": "ok", "ticket-full-capture": "ok"}  # fmt: skip
    for ticket, (_, exit_code) in SCENARIOS.items():
        output = workflow.agent_output[ticket]
        assert output.returncode == exit_code, output.stdout + output.stderr
        assert f"sent the run for ticket {ticket}" in output.stdout
        assert "warning: not all telemetry was exported" not in output.stdout
        trace = listed(cli, workflow, ticket)
        assert trace["status"] == expected_status[ticket] and trace["root_name"] == "answer_ticket"
        assert trace["agent_name"] == "support_agent" and trace["environments"] == ["dev"]
    _, out, _ = cli("traces", "list", "--json", "--project", workflow.project)
    assert len(json.loads(out)["traces"]) == len(SCENARIOS)


def test_spans_are_connected_and_simulated_model_calls_are_labelled(cli: Any, workflow: Workflow) -> None:
    trace = read_trace(workflow, listed(cli, workflow, "ticket-ok")["trace_id"])
    (root,) = trace["roots"]
    spans = {s["span_id"]: s for s in trace["spans"]}
    assert spans[root]["kind"] == "agent" and spans[root]["name"] == "answer_ticket"
    children = sorted((s for s in spans.values() if s["span_id"] != root), key=lambda s: s["start_time"])
    assert [(s["kind"], s["name"]) for s in children] == [
        ("llm", "chat"), ("tool", "get_order_status"), ("llm", "chat"), ("tool", "issue_refund"), ("llm", "chat"),
    ]  # fmt: skip
    assert all(s["parent_span_id"] == root for s in children) and trace["orphans"] == []
    for llm in (s for s in children if s["kind"] == "llm"):
        assert llm["fields"]["llm"]["provider"] == "mock_llm"  # never passed off as a real provider
        assert llm["fields"]["llm"]["request_model"] == "mock-model"
        assert llm["fields"]["usage"]["source"] == "provider_reported"
        assert llm["start_time"] >= spans[root]["start_time"] and llm["end_time"] <= spans[root]["end_time"]


def test_a_failed_tool_call_is_recorded_on_the_tool_and_the_run(cli: Any, workflow: Workflow) -> None:
    trace = read_trace(workflow, listed(cli, workflow, "ticket-refund-down")["trace_id"])
    spans = {s["name"]: s for s in trace["spans"]}
    refund = spans["issue_refund"]
    assert refund["status"] == "error"
    assert refund["fields"]["error"]["type"] == "TimeoutError" and refund["fields"]["error"]["origin"] == "timeout"
    assert spans["answer_ticket"]["status"] == "error" and spans["get_order_status"]["status"] == "ok"


def test_a_failed_model_call_is_recorded_as_a_provider_error(cli: Any, workflow: Workflow) -> None:
    trace = read_trace(workflow, listed(cli, workflow, "ticket-model-down")["trace_id"])
    kinds = sorted(s["kind"] for s in trace["spans"])
    assert kinds == ["agent", "llm"]  # the agent stopped at its first model call: no tool ran
    llm = next(s for s in trace["spans"] if s["kind"] == "llm")
    error = llm["fields"]["error"]
    assert llm["status"] == "error" and error["origin"] == "provider" and error["code"] == "503"
    assert error["retryable"] is True and error["type"].endswith("InternalServerError")


# --------------------------------------------------------------------------- analysis, comparison, gate


def test_analysis_names_the_failing_span_and_its_cause(cli: Any, workflow: Workflow) -> None:
    code, out, _ = cli("analyze", "--json", "--project", workflow.project, "db:session=ticket-refund-down")
    assert code == 0
    (report,) = json.loads(out)["traces"]
    (cause,) = [f for f in report["findings"] if f["rule"] == "error.root_cause"]
    assert cause["title"] == "tool 'issue_refund' failed with TimeoutError"
    refund = next(s for s in report["spans"] if s["label"] == "tool issue_refund")
    assert refund["status"] == "error" and refund["error_type"] == "TimeoutError"
    assert any(refund["span_id"][:8] in line for line in cause["evidence"])
    code, out, _ = cli("analyze", "--project", workflow.project, "db:session=ticket-ok")
    assert code == 0 and "status=OK" in out


def test_comparing_the_good_run_with_the_failed_one_finds_the_regression(cli: Any, workflow: Workflow) -> None:
    code, out, _ = cli(
        "compare", "--json", "--project", workflow.project, "db:session=ticket-ok", "db:session=ticket-refund-down"
    )
    assert code == 0
    findings = {f["rule"]: f for f in json.loads(out)["comparisons"][0]["findings"]}
    assert findings["regression.run_failed"]["current"] == "error"
    assert findings["regression.new_error"]["title"] == "new error: tool 'issue_refund' failed with TimeoutError"


def failed_checks(report: dict[str, Any]) -> set[str]:
    checks = [*report["gate_checks"], *(c for run in report["runs"] for c in run["checks"])]
    return {c["id"] for c in checks if c["result"] in ("fail", "unknown")}


def test_the_gate_passes_the_good_run_and_rejects_each_failure_for_its_reason(
    cli: Any, workflow: Workflow, tmp_path: Path
) -> None:
    def gate(ticket: str, *extra: str) -> tuple[int, dict[str, Any]]:
        code, out, _ = cli("gate", "--json", "--project", workflow.project, "--policy", POLICY,
                           f"db:session={ticket}", *extra)  # fmt: skip
        return code, json.loads(out)

    code, report = gate("ticket-ok", "--junit", str(tmp_path / "ok.xml"))
    assert code == 0 and report["verdict"] == "pass" and failed_checks(report) == set()
    assert ET.parse(tmp_path / "ok.xml").getroot().get("failures") == "0"

    code, report = gate("ticket-refund-down", "--junit", str(tmp_path / "down.xml"))
    assert code == 3
    assert failed_checks(report) == {"gate.pass_rate", "run.require_success", "run.tools.issue_refund.max_failures"}
    success = next(c for c in report["runs"][0]["checks"] if c["id"] == "run.require_success")
    assert "root cause: tool 'issue_refund' failed with TimeoutError" in success["evidence"][0]
    assert ET.parse(tmp_path / "down.xml").getroot().get("failures") == "3"

    code, report = gate("ticket-model-down")
    assert code == 3
    assert {"run.require_success", "run.required_tools.get_order_status"} <= failed_checks(report)
    success = next(c for c in report["runs"][0]["checks"] if c["id"] == "run.require_success")
    assert "InternalServerError" in success["evidence"][0]


def test_missing_telemetry_fails_the_gate_as_unknown_not_as_zero(cli: Any, workflow: Workflow) -> None:
    code, out, _ = cli(
        "gate", "--json", "--project", workflow.project, "--policy", POLICY, "db:session=ticket-no-usage"
    )
    assert code == 3
    report = json.loads(out)
    assert failed_checks(report) == {"gate.pass_rate", "run.max_input_tokens"}
    tokens = next(c for c in report["runs"][0]["checks"] if c["id"] == "run.max_input_tokens")
    assert tokens["result"] == "unknown" and tokens["actual"] == "unknown"


# --------------------------------------------------------------------------- privacy and secrets


def test_content_and_the_api_key_are_stored_and_shown_nowhere(cli: Any, workflow: Workflow, tmp_path: Path) -> None:
    """The full-capture run sent its prompts and tool arguments; the project's metadata-only
    ceiling dropped them before storage. The API key is never stored or printed."""
    sent = tmp_path / "sent.jsonl"  # what the same run sends: the prompt is really in it
    subprocess.run(
        [sys.executable, "examples/support_agent/run.py", "--scenario", "refund", "--capture", "full",
         "--out", str(sent), "--quiet"],
        cwd=REPO_ROOT, check=True, capture_output=True, timeout=120,
    )  # fmt: skip
    assert QUESTION in sent.read_text()
    stored = database_text(workflow.engine)
    assert "ticket-full-capture" in stored  # the run is there
    for secret in (QUESTION, "it arrived broken", "Your refund rf_1042", workflow.key):
        assert secret not in stored, secret
        for output in workflow.agent_output.values():
            assert workflow.key not in output.stdout + output.stderr
    code, out, _ = cli("analyze", "--json", "--project", workflow.project, "db:session=ticket-full-capture")
    assert code == 0 and QUESTION not in out and "arrived broken" not in out


# --------------------------------------------------------------------------- dashboard


def test_the_dashboard_shows_the_stored_runs(workflow: Workflow) -> None:
    config = DashboardConfig(samples_dir=None, token=TOKEN, project_id=UUID(workflow.project),
                             engine=workflow.engine)  # fmt: skip
    client = TestClient(create_dashboard(config))
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/api/datasets/stored/overview").status_code == 401
    overview = client.get("/api/datasets/stored/overview", headers=auth).json()
    assert overview["runs"] == len(SCENARIOS) and overview["failed"] == 2 and overview["succeeded"] == 3
    failed = client.get("/api/datasets/stored/runs", headers=auth, params={"status": "failed"}).json()["runs"]
    assert len(failed) == 2
    details = [client.get(f"/api/datasets/stored/runs/{r['trace_id']}", headers=auth).json() for r in failed]
    causes = {f["title"] for d in details for f in d["findings"] if f["rule"] == "error.root_cause"}
    assert "tool 'issue_refund' failed with TimeoutError" in causes
    assert any("InternalServerError" in title for title in causes)
    assert all(QUESTION not in json.dumps(d) for d in details)


# --------------------------------------------------------------------------- delivery problems


def run_agent(endpoint: str, key: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "AOBS_API_KEY": key, "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    return subprocess.run(
        [sys.executable, "examples/support_agent/run.py", *args, "--endpoint", endpoint],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120,
    )  # fmt: skip


@pytest.mark.parametrize("problem", ["wrong key", "no collector"])
def test_a_run_that_did_not_reach_the_collector_is_reported_never_claimed_as_sent(
    workflow: Workflow, problem: str
) -> None:
    key = "aobs_ik_" + "x" * 43  # well-formed, never issued
    endpoint = workflow.url if problem == "wrong key" else "http://127.0.0.1:9"  # nothing listens on port 9
    output = run_agent(endpoint, key, "--scenario", "refund", "--ticket", f"ticket-{problem.replace(' ', '-')}")
    text = output.stdout + output.stderr
    assert output.returncode == 0  # the agent itself succeeded
    assert "warning: not all telemetry was exported" in text and "sent the run" not in text
    assert key not in text and workflow.key not in text


def test_the_printed_selection_finds_the_run_whatever_the_ticket(cli: Any, workflow: Workflow) -> None:
    ticket = "order 1042 & 'refund'"
    own = make_collector(workflow.engine, name="hint", capture_ceiling="metadata_only")  # keeps run counts apart
    output = run_agent(workflow.url, own.key, "--scenario", "success", "--ticket", ticket)
    assert output.returncode == 0, output.stdout + output.stderr
    hint = next(line for line in output.stdout.splitlines() if "find it:" in line)
    selection = hint.split("'")[1]  # the shell-quoted db: selection
    assert selection == "db:session=order%201042%20%26%20%27refund%27"
    code, out, _ = cli("traces", "list", "--json", "--project", str(own.project_id), selection)
    assert code == 0 and [t["session_id"] for t in json.loads(out)["traces"]] == [ticket]
