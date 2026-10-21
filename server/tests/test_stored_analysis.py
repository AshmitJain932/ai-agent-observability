"""analyze / compare / compare-groups / gate / traces list on traces stored in real PostgreSQL.

The SDK recordings in examples/diagnose are ingested through ``POST /v1/ingest`` and then analyzed
from the database with ``db:`` selections. Each report must match the one the same command gives
for the files, so the database path reuses the analysis engine rather than re-implementing it.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, insert
from sqlalchemy.engine import URL

from aobs_server.cli import main
from aobs_server.dashboard import DashboardConfig, create_app
from aobs_server.diagnosis.stored import SelectionSyntaxError, StoredTraces, parse_selection
from aobs_server.storage.db import make_engine
from aobs_server.storage.tables import trace_tombstones
from ingest_support import Collector, batch, end, make_collector, sid, start, tid
from support import REPO_ROOT

DIAGNOSE = REPO_ROOT / "examples" / "diagnose"
PAIRS = DIAGNOSE / "compare"
GROUPS = DIAGNOSE / "groups"
POLICIES = DIAGNOSE / "gate"


def ingest_file(c: Collector, path: Path) -> list[str]:
    """Post every batch of an SDK JSONL file; the trace ids it holds, in file order."""
    trace_ids: list[str] = []
    for line in path.read_text().splitlines():
        body = json.loads(line)
        response = c.post(body)
        assert response.status_code == 200, response.text
        assert response.json()["counts"]["rejected"] == 0, response.text
        for event in body["events"]:
            if event["trace_id"] not in trace_ids:
                trace_ids.append(event["trace_id"])
    return trace_ids


def only(trace_ids: list[str]) -> str:
    (trace_id,) = trace_ids
    return trace_id


@pytest.fixture
def cli(db_url: URL, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Any:
    monkeypatch.setenv("AOBS_DATABASE_URL", db_url.render_as_string(hide_password=False))

    def run(*argv: str) -> tuple[int, str, str]:
        code = main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err

    return run


@pytest.fixture
def stored(engine: Engine) -> dict[str, Any]:
    """Project A holds the refund pair, the inventory pair and two groups of repeated runs."""
    c = make_collector(engine, name="a")
    ids = {
        name: ingest_file(c, PAIRS / f"{name}.jsonl")
        for name in ("refund_ok", "refund_outage", "inventory_v1", "inventory_v2")
    }
    ids["baseline"] = ingest_file(c, GROUPS / "baseline.jsonl")
    ids["slow"] = ingest_file(c, GROUPS / "slow.jsonl")
    return {"collector": c, "project": str(c.project_id), "ids": ids}


def named(value: Any, *names: str) -> Any:
    """``value`` with every input name (a file path or a db: selection) replaced by ``<input>``: reports
    name their inputs, e.g. in a suggested next command."""
    text = json.dumps(value)
    for name in names:
        text = text.replace(name, "<input>")
    return json.loads(text)


def runs_of(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [{k: v for k, v in run.items() if k != "source"} for run in report]


# --------------------------------------------------------------------------- analyze


def test_a_stored_run_is_analyzed_exactly_like_its_file(cli: Any, stored: dict[str, Any]) -> None:
    trace = only(stored["ids"]["refund_outage"])
    code, out, _ = cli("analyze", "--json", "--project", stored["project"], f"db:trace={trace}")
    assert code == 0
    from_db = json.loads(out)
    code, out, _ = cli("analyze", "--json", str(PAIRS / "refund_outage.jsonl"))
    assert code == 0
    from_file = json.loads(out)
    assert from_db["sources"] == [f"db:trace={trace}"] and from_db["input_problems"] == []
    assert from_db["traces"] == from_file["traces"]  # same spans, findings, evidence and resource
    assert from_db["traces"][0]["findings"]  # the outage is diagnosed


def test_analyze_selects_stored_runs_by_task_and_status(cli: Any, stored: dict[str, Any]) -> None:
    code, out, _ = cli("analyze", "--json", "--project", stored["project"], "db:task=answer_ticket&status=error")
    assert code == 0
    report = json.loads(out)
    assert [t["summary"]["trace_id"] for t in report["traces"]] == stored["ids"]["refund_outage"]
    code, out, _ = cli("analyze", "--json", "--project", stored["project"], "db:task=answer_ticket")
    assert {t["summary"]["trace_id"] for t in json.loads(out)["traces"]} == {
        *stored["ids"]["refund_ok"],
        *stored["ids"]["refund_outage"],
    }


def test_an_empty_selection_is_reported_and_exits_1(cli: Any, stored: dict[str, Any]) -> None:
    code, out, _ = cli("analyze", "--project", stored["project"], "db:task=no_such_task")
    assert code == 1
    assert "the selection matched no stored traces in this project" in out


# --------------------------------------------------------------------------- compare


def test_compare_a_stored_baseline_with_a_stored_run_like_the_files(cli: Any, stored: dict[str, Any]) -> None:
    ok, outage = only(stored["ids"]["refund_ok"]), only(stored["ids"]["refund_outage"])
    code, out, _ = cli("compare", "--json", "--project", stored["project"], f"db:trace={ok}", f"db:trace={outage}")
    assert code == 0
    from_db = json.loads(out)["comparisons"][0]
    code, out, _ = cli("compare", "--json", str(PAIRS / "refund_ok.jsonl"), str(PAIRS / "refund_outage.jsonl"))
    from_file = json.loads(out)["comparisons"][0]
    names = (f"db:trace={ok}", f"db:trace={outage}", str(PAIRS / "refund_ok.jsonl"), str(PAIRS / "refund_outage.jsonl"))
    assert named(from_db["findings"], *names) == named(from_file["findings"], *names) and from_db["findings"]
    assert from_db["notes"] == from_file["notes"]
    assert from_db["baseline"]["source"] == f"db:trace={ok}"


def test_compare_mixes_a_baseline_file_with_stored_runs(cli: Any, stored: dict[str, Any]) -> None:
    v2 = only(stored["ids"]["inventory_v2"])
    code, out, _ = cli(
        "compare", "--json", "--project", stored["project"], str(PAIRS / "inventory_v1.jsonl"), f"db:trace={v2}"
    )
    assert code == 0
    from_db = json.loads(out)["comparisons"][0]
    _, out, _ = cli("compare", "--json", str(PAIRS / "inventory_v1.jsonl"), str(PAIRS / "inventory_v2.jsonl"))
    names = (f"db:trace={v2}", str(PAIRS / "inventory_v2.jsonl"))
    from_file = json.loads(out)["comparisons"][0]
    assert named(from_db["findings"], *names) == named(from_file["findings"], *names) and from_db["findings"]


def test_compare_needs_one_run_per_side_and_says_how_to_pick(cli: Any, stored: dict[str, Any]) -> None:
    ok = only(stored["ids"]["refund_ok"])
    code, _, err = cli("compare", "--project", stored["project"], f"db:trace={ok}", "db:task=answer_ticket")
    assert code == 2 and "holds 2 runs; choose one with a trace id prefix" in err
    outage = only(stored["ids"]["refund_outage"])
    code, _, _ = cli(
        "compare", "--project", stored["project"], f"db:trace={ok}", "db:task=answer_ticket",
        "--current-trace", outage[:8],
    )  # fmt: skip
    assert code == 0


# --------------------------------------------------------------------------- compare-groups


def test_compare_groups_of_stored_runs_like_the_files(cli: Any, stored: dict[str, Any]) -> None:
    base, slow = ",".join(stored["ids"]["baseline"]), ",".join(stored["ids"]["slow"])
    assert len(stored["ids"]["baseline"]) == 10 and len(stored["ids"]["slow"]) == 10
    code, out, _ = cli(
        "compare-groups", "--json", "--project", stored["project"],
        "--baseline", f"db:trace={base}", "--current", f"db:trace={slow}",
    )  # fmt: skip
    assert code == 0
    from_db = json.loads(out)
    code, out, _ = cli(
        "compare-groups",
        "--json",
        "--baseline",
        str(GROUPS / "baseline.jsonl"),
        "--current",
        str(GROUPS / "slow.jsonl"),
    )
    from_file = json.loads(out)
    assert from_db["warnings"] == from_file["warnings"]
    assert from_db["metrics"] == from_file["metrics"]
    assert from_db["changes"] == from_file["changes"]
    assert runs_of(from_db["baseline"]["runs"]) == runs_of(from_file["baseline"]["runs"])


# --------------------------------------------------------------------------- gate


def test_gate_passes_and_fails_stored_runs_like_the_files(cli: Any, stored: dict[str, Any]) -> None:
    policy = str(POLICIES / "support_agent.toml")
    ok, outage = only(stored["ids"]["refund_ok"]), only(stored["ids"]["refund_outage"])
    code, out, _ = cli("gate", "--project", stored["project"], "--policy", policy, f"db:trace={ok}")
    assert code == 0 and "aobs reliability gate: PASS" in out
    code, out, _ = cli("gate", "--json", "--project", stored["project"], "--policy", policy, f"db:trace={outage}")
    assert code == 3
    from_db = json.loads(out)
    _, out, _ = cli("gate", "--json", "--policy", policy, str(PAIRS / "refund_outage.jsonl"))
    from_file = json.loads(out)
    assert from_db["verdict"] == from_file["verdict"] == "fail"
    assert [r["checks"] for r in from_db["runs"]] == [r["checks"] for r in from_file["runs"]]


def test_gate_checks_every_selected_run_and_a_stored_baseline(cli: Any, stored: dict[str, Any]) -> None:
    v1, v2 = only(stored["ids"]["inventory_v1"]), only(stored["ids"]["inventory_v2"])
    policy = str(POLICIES / "restock_baseline.toml")
    code, out, _ = cli(
        "gate", "--json", "--project", stored["project"], "--policy", policy, "--baseline", f"db:trace={v1}",
        f"db:trace={v2}",
    )  # fmt: skip
    from_db = json.loads(out)
    file_code, out, _ = cli(
        "gate", "--json", "--policy", policy, "--baseline", str(PAIRS / "inventory_v1.jsonl"),
        str(PAIRS / "inventory_v2.jsonl"),
    )  # fmt: skip
    from_file = json.loads(out)
    assert code == file_code
    assert [r["checks"] for r in from_db["runs"]] == [r["checks"] for r in from_file["runs"]]
    assert any(c["id"].startswith("baseline.") for c in from_db["runs"][0]["checks"])


def test_gate_on_an_empty_selection_fails(cli: Any, stored: dict[str, Any]) -> None:
    policy = str(POLICIES / "support_agent.toml")
    code, out, _ = cli("gate", "--project", stored["project"], "--policy", policy, "db:task=answer_ticket&status=open")
    assert code == 3
    assert "FAIL     gate.input_valid" in out and "matched no stored traces" in out
    assert "FAIL     gate.min_runs" in out


# --------------------------------------------------------------------------- traces list


def test_traces_list_shows_a_projects_traces_newest_first(cli: Any, stored: dict[str, Any]) -> None:
    code, out, _ = cli("traces", "list", "--json", "--project", stored["project"], "db:task=answer_ticket")
    assert code == 0
    listed = json.loads(out)["traces"]
    assert {t["trace_id"] for t in listed} == {*stored["ids"]["refund_ok"], *stored["ids"]["refund_outage"]}
    starts = [t["started_at"] for t in listed]
    assert starts == sorted(starts, reverse=True)
    assert all(t["input"] == f"db:trace={t['trace_id']}" for t in listed)
    code, out, _ = cli("traces", "list", "--project", stored["project"], "--limit", "3")
    assert code == 0 and "3 trace(s)" in out
    code, out, _ = cli("traces", "list", "--project", stored["project"], "db:environment=no-such-env")
    assert code == 0 and "no stored traces match" in out


def test_traces_list_filters_by_start_time(cli: Any, stored: dict[str, Any]) -> None:
    _, out, _ = cli("traces", "list", "--json", "--project", stored["project"], "db:limit=1000")
    every = json.loads(out)["traces"]
    middle = sorted(t["started_at"] for t in every)[len(every) // 2]
    _, out, _ = cli("traces", "list", "--json", "--project", stored["project"], f"db:since={middle}&limit=1000")
    later = json.loads(out)["traces"]
    _, out, _ = cli("traces", "list", "--json", "--project", stored["project"], f"db:until={middle}&limit=1000")
    earlier = json.loads(out)["traces"]
    assert len(later) + len(earlier) == len(every) and later and earlier
    assert all(t["started_at"] >= middle for t in later) and all(t["started_at"] < middle for t in earlier)


# --------------------------------------------------------------------------- isolation and privacy


def test_another_projects_traces_are_never_selected(cli: Any, engine: Engine, stored: dict[str, Any]) -> None:
    other = make_collector(engine, name="b")
    shared = only(ingest_file(other, PAIRS / "refund_ok.jsonl"))  # the same trace id in both projects
    assert shared == only(stored["ids"]["refund_ok"])
    outage = only(stored["ids"]["refund_outage"])
    code, _, err = cli("analyze", "--project", str(other.project_id), f"db:trace={outage}")
    assert code == 1 and f"no stored trace {outage} in project {other.project_id}" in err
    _, out, _ = cli("traces", "list", "--json", "--project", str(other.project_id), "db:limit=1000")
    assert [t["trace_id"] for t in json.loads(out)["traces"]] == [shared]
    _, out, _ = cli("analyze", "--json", "--project", str(other.project_id), "db:limit=1000")
    assert [t["summary"]["trace_id"] for t in json.loads(out)["traces"]] == [shared]
    # Deleting the trace in project A hides it there and nowhere else.
    with engine.begin() as conn:
        conn.execute(insert(trace_tombstones).values(project_id=UUID(stored["project"]), trace_id=shared))
    code, _, err = cli("analyze", "--project", stored["project"], f"db:trace={shared}")
    assert code == 1 and "no stored trace" in err
    code, _, _ = cli("analyze", "--project", str(other.project_id), f"db:trace={shared}")
    assert code == 0


def test_the_projects_current_privacy_policy_and_no_content_reach_the_report(cli: Any, engine: Engine) -> None:
    c = make_collector(engine, name="private")
    secret_content = "the-model-said-something-private"
    error = {"type": "ValueError", "message": "card 4111 1111 1111 1111 for jane@example.com", "stack": "frame"}
    content = {"input": {"status": "captured", "format": "text", "value": secret_content}}
    trace = tid(0xA1)
    events = [
        start(trace, sid(1), name="run"),
        end(trace, sid(1), name="run", status="error", error=error, content=content),
    ]
    assert c.post(batch(*events)).status_code == 200
    code, out, _ = cli("analyze", "--json", "--project", str(c.project_id), f"db:trace={trace}")
    assert code == 0
    assert secret_content not in out and "4111 1111" not in out and "jane@example.com" not in out
    assert "[REDACTED:card_number]" in out  # the error message is kept, scrubbed
    # Turning error messages off for the project hides them from stored-trace analysis too.
    with engine.begin() as conn:
        from aobs_server.storage.tables import projects

        conn.execute(
            projects.update()
            .where(projects.c.id == c.project_id)
            .values(settings={"capture_ceiling": "full", "error_messages": False, "stack_traces": False})
        )
    code, out, _ = cli("analyze", "--json", "--project", str(c.project_id), f"db:trace={trace}")
    assert code == 0 and "REDACTED:card_number" not in out and "ValueError" in out


# --------------------------------------------------------------------------- incomplete and truncated runs


def test_unfinished_and_truncated_runs_are_flagged(cli: Any, engine: Engine) -> None:
    c = make_collector(engine, name="partial", max_spans_per_trace=2)
    unfinished, big = tid(0xB1), tid(0xB2)
    assert c.post(batch(start(unfinished, sid(1), name="run"))).status_code == 200
    spans = [start(big, sid(1), name="run")] + [
        e for n in range(2, 5) for e in (start(big, sid(n), sid(1), name="step", kind="step"),
                                         end(big, sid(n), sid(1), name="step", kind="step"))
    ]  # fmt: skip
    assert c.post(batch(*spans, end(big, sid(1), name="run"))).status_code == 200
    code, out, _ = cli("analyze", "--json", "--project", str(c.project_id), f"db:trace={unfinished},{big}")
    assert code == 0
    report = json.loads(out)
    status = {t["summary"]["trace_id"]: t["summary"]["status"] for t in report["traces"]}
    assert status[unfinished] in ("in_progress", "incomplete")
    assert any(f"trace {big} was truncated at ingest" in p for p in report["input_problems"])
    policy = str(POLICIES / "support_agent.toml")
    code, out, _ = cli("gate", "--project", str(c.project_id), "--policy", policy, f"db:trace={unfinished}")
    assert code == 3


# --------------------------------------------------------------------------- usage and database errors


@pytest.mark.parametrize(
    ("selection", "message"),
    [
        ("db:trace=XYZ", "is not a trace id"),
        ("db:trace=" + "0" * 32, "is not a trace id"),
        ("db:color=red", "unknown selection key"),
        ("db:status=done", "status must be one of"),
        ("db:limit=0", "limit must be a whole number"),
        ("db:limit=5000", "limit must be a whole number"),
        ("db:since=yesterday", "is not an ISO 8601"),
        ("db:since=2026-10-02&until=2026-10-01", "since must be earlier"),
        ("db:task=a&task=b", "more than once"),
        ("db:", "empty selection"),
        ("db:task", "expected key=value"),
    ],
)
def test_malformed_selections_are_usage_errors(selection: str, message: str) -> None:
    with pytest.raises(SelectionSyntaxError, match=message):
        parse_selection(selection)


def test_selection_syntax(stored: dict[str, Any]) -> None:
    parsed = parse_selection("db:task=answer+ticket&environment=prod&since=2026-10-01&until=2026-10-02T12:00:00Z")
    assert parsed.task == "answer ticket" and parsed.environment == "prod"
    assert parsed.since is not None and parsed.since.isoformat() == "2026-10-01T00:00:00+00:00"
    assert parsed.until is not None and parsed.until - parsed.since == timedelta(days=1, hours=12)
    many = parse_selection(f"db:trace={tid(1)},{tid(2)}&trace={tid(1)}")
    assert many.trace_ids == (tid(1), tid(2))


def test_cli_usage_errors(cli: Any, stored: dict[str, Any]) -> None:
    project = stored["project"]
    assert cli("analyze", "db:task=x")[0] == 2  # db: input without --project
    assert cli("analyze", "--project", project, str(PAIRS / "refund_ok.jsonl"))[0] == 2  # --project without db:
    code, _, err = cli("analyze", "--project", project, "db:status=done")
    assert code == 2 and "status must be one of" in err
    code, _, err = cli("analyze", "--project", "not-a-uuid", "db:task=x")
    assert code == 1 and "is not a valid id" in err
    code, _, err = cli("analyze", "--project", str(uuid4()), "db:task=x")
    assert code == 1 and "does not exist" in err
    code, _, err = cli("traces", "list", "--project", project, "--limit", "0")
    assert code == 2


def test_an_unreachable_database_is_exit_1_without_a_traceback(
    stored: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("AOBS_DATABASE_URL", f"postgresql+psycopg://nobody@/aobs?host={tmp_path}")
    assert main(["analyze", "--project", stored["project"], "db:task=answer_ticket"]) == 1
    assert "cannot read the database" in capsys.readouterr().err
    assert main(["traces", "list", "--project", stored["project"]]) == 1
    assert "cannot read the database" in capsys.readouterr().err


def test_stored_traces_read_the_database_once_per_load(engine: Engine, stored: dict[str, Any]) -> None:
    """The source names itself by its selection and reports what it read."""
    source = StoredTraces(
        engine, UUID(stored["project"]), parse_selection("db:task=restock_check"), "db:task=restock_check"
    )
    read = source.read()
    assert str(source) == "db:task=restock_check"
    assert read.traces == 2 + 20 and not read.problems
    assert set(read.resources) == {e.event.trace_id for e in read.events}


# --------------------------------------------------------------------------- dashboard

TOKEN = "stored-dashboard-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def dashboard(engine: Engine, project: str) -> TestClient:
    config = DashboardConfig(samples_dir=None, token=TOKEN, project_id=UUID(project), engine=engine)
    return TestClient(create_app(config), raise_server_exceptions=False)


def test_dashboard_shows_a_projects_stored_traces_behind_the_token(
    cli: Any, engine: Engine, stored: dict[str, Any]
) -> None:
    client = dashboard(engine, stored["project"])
    (listed,) = client.get("/api/datasets").json()["datasets"]
    assert listed["id"] == "stored" and listed["kind"] == "stored" and listed["requires_token"]
    assert stored["project"] not in json.dumps(listed)  # shown without the token
    for route in ("overview", "runs", "files"):
        assert client.get(f"/api/datasets/stored/{route}").status_code == 401
    overview = client.get("/api/datasets/stored/overview", headers=AUTH).json()
    assert overview["source"]["kind"] == "stored"
    every = [t for ids in stored["ids"].values() for t in ids]
    assert overview["runs"] == len(every) and overview["failed"] >= 1
    runs = client.get("/api/datasets/stored/runs", headers=AUTH, params={"status": "failed"}).json()["runs"]
    assert {r["trace_id"] for r in runs} >= set(stored["ids"]["refund_outage"])
    # A run's detail is the same analysis as `analyze` gives for the stored trace.
    outage = only(stored["ids"]["refund_outage"])
    detail = client.get(f"/api/datasets/stored/runs/{outage}", headers=AUTH).json()
    _, out, _ = cli("analyze", "--json", "--project", stored["project"], f"db:trace={outage}")
    (analyzed,) = json.loads(out)["traces"]
    assert detail["findings"] == analyzed["findings"] and detail["summary"] == analyzed["summary"]


def test_dashboard_compares_stored_runs_and_groups(cli: Any, engine: Engine, stored: dict[str, Any]) -> None:
    client = dashboard(engine, stored["project"])
    ok, outage = only(stored["ids"]["refund_ok"]), only(stored["ids"]["refund_outage"])
    data = client.get("/api/datasets/stored/compare", headers=AUTH, params={"baseline": ok, "current": outage}).json()
    _, out, _ = cli("compare", "--json", "--project", stored["project"], f"db:trace={ok}", f"db:trace={outage}")
    expected = json.loads(out)["comparisons"][0]
    names = (f"db:trace={ok}", f"db:trace={outage}", ok, outage)  # longest first
    assert named(data["comparisons"][0]["findings"], *names) == named(expected["findings"], *names)
    params = [("baseline", t) for t in stored["ids"]["baseline"]] + [("current", t) for t in stored["ids"]["slow"]]
    groups = client.get("/api/datasets/stored/compare-groups", headers=AUTH, params=params).json()
    _, out, _ = cli(
        "compare-groups",
        "--json",
        "--baseline",
        str(GROUPS / "baseline.jsonl"),
        "--current",
        str(GROUPS / "slow.jsonl"),
    )
    assert groups["metrics"] == json.loads(out)["metrics"]


def test_dashboard_never_shows_another_projects_traces_or_content(engine: Engine, stored: dict[str, Any]) -> None:
    other = make_collector(engine, name="other")
    secret = "the-model-said-something-private"
    trace = tid(0xC1)
    content = {"input": {"status": "captured", "format": "text", "value": secret}}
    assert other.post(batch(start(trace, sid(1)), end(trace, sid(1), content=content))).status_code == 200
    client = dashboard(engine, stored["project"])
    assert client.get(f"/api/datasets/stored/runs/{trace}", headers=AUTH).status_code == 404
    ok = only(stored["ids"]["refund_ok"])
    response = client.get("/api/datasets/stored/compare", headers=AUTH, params={"baseline": ok, "current": trace})
    assert response.status_code == 400 and "unknown file" in response.json()["detail"]
    own = dashboard(engine, str(other.project_id))
    texts = [
        own.get(f"/api/datasets/stored/{route}", headers=AUTH).text for route in ("overview", "runs", f"runs/{trace}")
    ]
    assert "unknown" not in texts[2] and all(secret not in text for text in texts)


def test_dashboard_picks_up_newly_stored_traces(engine: Engine) -> None:
    c = make_collector(engine, name="live")
    client = dashboard(engine, str(c.project_id))
    assert client.get("/api/datasets/stored/overview", headers=AUTH).json()["runs"] == 0
    ingest_file(c, PAIRS / "refund_ok.jsonl")
    assert client.get("/api/datasets/stored/overview", headers=AUTH).json()["runs"] == 1


def test_dashboard_reports_an_unreadable_database_as_503(tmp_path: Path, stored: dict[str, Any]) -> None:
    broken = make_engine(f"postgresql+psycopg://nobody@/aobs?host={tmp_path}", pool_size=1)
    try:
        response = dashboard(broken, stored["project"]).get("/api/datasets/stored/overview", headers=AUTH)
    finally:
        broken.dispose()
    assert response.status_code == 503 and "could not be read" in response.json()["detail"]


def test_cli_dashboard_serves_a_projects_stored_traces(
    cli: Any, stored: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    started: dict[str, Any] = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: started.update(app=app, **kw))
    monkeypatch.setenv("AOBS_DASHBOARD_TOKEN", TOKEN)
    code, out, _ = cli("dashboard", "--no-samples", "--project", stored["project"])
    assert code == 0 and f"#token={TOKEN}" in out and f"stored traces of project {stored['project']}" in out
    client = TestClient(started["app"])
    assert client.get("/api/datasets/stored/overview").status_code == 401
    assert client.get("/api/datasets/stored/overview", headers=AUTH).json()["runs"] > 0
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: pytest.fail("should not start"))
    assert cli("dashboard", "--project", str(uuid4()))[0] == 1  # unknown project
    assert cli("dashboard", "--project", "nope")[0] == 1
