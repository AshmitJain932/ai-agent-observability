"""Dashboard JSON API: correctness against the CLI libraries, validation, privacy, token protection."""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aobs_server.cli import main
from aobs_server.dashboard import DashboardConfig, create_app
from aobs_server.dashboard.app import without_paths
from aobs_server.diagnosis import build_comparisons, build_group_report, build_report
from support import REPO_ROOT

SAMPLES = REPO_ROOT / "examples" / "diagnose"
TOKEN = "test-token-0123456789abcdef"
SENSITIVE = ["jane.doe@example.com", "Berlin warehouse", "Summarize the account", "Jane has two", "c-7781"]


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(DashboardConfig(samples_dir=SAMPLES)))


@pytest.fixture
def local_dir(tmp_path: Path) -> Path:
    data = tmp_path / "traces"
    data.mkdir()
    shutil.copy(SAMPLES / "traces" / "refund_outage.jsonl", data / "outage.jsonl")
    return data


@pytest.fixture
def local_client(local_dir: Path) -> TestClient:
    return TestClient(create_app(DashboardConfig(samples_dir=SAMPLES, data_dir=local_dir, token=TOKEN)))


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def strip(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k != "source"}


# --------------------------------------------------------------------------- datasets and overview


def test_sample_datasets_are_listed_and_labelled(client: TestClient) -> None:
    data = client.get("/api/datasets").json()["datasets"]
    assert [d["id"] for d in data] == ["sample-repeated", "sample-pairs", "sample-failures"]
    assert all(d["kind"] == "sample" and not d["requires_token"] for d in data)
    for dataset in data:
        listed = {f["name"] for f in client.get(f"/api/datasets/{dataset['id']}/files").json()["files"]}
        for preset in dataset["presets"]:
            assert set(preset["baseline"]) | set(preset["current"]) <= listed, preset["id"]
    overview = client.get("/api/datasets/sample-repeated/overview").json()
    assert overview["source"]["kind"] == "sample"
    assert "not live production traffic" in overview["source"]["note"]


def test_overview_numbers_match_an_independent_count(client: TestClient) -> None:
    files = sorted((SAMPLES / "groups").glob("*.jsonl"))
    report = build_report(files, datetime.now(UTC))
    statuses = Counter(t.trace.status for t in report.traces)
    durations = [
        t.summary()["duration_ms"] for t in report.traces if t.trace.status in ("ok", "ok_with_errors", "error")
    ]
    data = client.get("/api/datasets/sample-repeated/overview").json()
    assert data["runs"] == len(report.traces) == 63
    assert data["succeeded"] == statuses["ok"] + statuses["ok_with_errors"]
    assert data["failed"] == statuses["error"] == 7
    assert data["failure_rate"] == pytest.approx(7 / 63)
    assert data["median_duration_ms"] == pytest.approx(statistics.median(durations))
    # 6 runs of partial_usage.jsonl lack usage and cost: they are left out of the sums, not counted as zero
    assert data["tokens"]["input_tokens"]["runs_counted"] == 57
    assert data["cost"]["runs_counted"] == 57 and data["cost"]["currency"] == "USD"
    assert len(data["recent"]) == 8 and data["tasks"] == ["restock_check"]


def test_cost_is_not_reported_when_no_run_reports_it(client: TestClient) -> None:
    data = client.get("/api/datasets/sample-failures/overview").json()
    assert data["cost"]["total"] is None and "no run reports cost" in data["cost"]["note"]
    assert data["unfinished"] == 1 and data["finished"] == 4  # the crashed run is not a failure or a success


# --------------------------------------------------------------------------- runs and details


def test_runs_filters(client: TestClient) -> None:
    every = client.get("/api/datasets/sample-repeated/runs").json()
    failed = client.get("/api/datasets/sample-repeated/runs", params={"status": "failed"}).json()["runs"]
    assert len(every["runs"]) == every["total"] == 63
    assert len(failed) == 7 and all(r["status"] == "error" for r in failed)
    by_file = client.get("/api/datasets/sample-repeated/runs", params={"file": "few_runs.jsonl"}).json()["runs"]
    assert len(by_file) == 3 and {r["file"] for r in by_file} == {"few_runs.jsonl"}
    none = client.get("/api/datasets/sample-repeated/runs", params={"task": "nope"}).json()
    assert none["runs"] == [] and none["total"] == 63


@pytest.mark.parametrize(
    "params",
    [{"status": "exploded"}, {"file": "../../etc/passwd"}, {"file": "missing.jsonl"}, {"status": "x" * 50}],
)
def test_runs_rejects_invalid_parameters(client: TestClient, params: dict[str, str]) -> None:
    response = client.get("/api/datasets/sample-repeated/runs", params=params)
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "urn:aobs:problem:bad-request"


def test_run_detail_matches_analyze(client: TestClient) -> None:
    expected = build_report([SAMPLES / "traces" / "retry_storm.jsonl"], datetime.now(UTC)).traces[0].to_dict()
    trace_id = expected["summary"]["trace_id"]
    data = client.get(f"/api/datasets/sample-failures/runs/{trace_id}").json()
    assert data["findings"] == expected["findings"]
    assert data["tools"] == expected["tools"]
    assert [s["span_id"] for s in data["spans"]] == [s["span_id"] for s in expected["spans"]]
    assert all(s["offset_ms"] is not None and s["offset_ms"] >= 0 for s in data["spans"])
    assert data["file"] == "retry_storm.jsonl" and data["outcome"] == "failed" and data["cost"] is None


@pytest.mark.parametrize("trace_id", ["0" * 32, "ABCDEF" + "0" * 26, "short", "..%2F..%2Fetc"])
def test_unknown_or_malformed_run_is_404(client: TestClient, trace_id: str) -> None:
    response = client.get(f"/api/datasets/sample-failures/runs/{trace_id}")
    assert response.status_code == 404 and response.json()["detail"] == "The requested resource was not found"


def test_unknown_dataset_is_404(client: TestClient) -> None:
    assert client.get("/api/datasets/nope/overview").status_code == 404
    assert client.get("/api/datasets/local/overview").status_code == 404  # no --data-dir configured


# --------------------------------------------------------------------------- regression analysis


def test_compare_matches_the_cli_library(client: TestClient) -> None:
    params = {"baseline": "refund_ok.jsonl", "current": "refund_outage.jsonl"}
    data = client.get("/api/datasets/sample-pairs/compare", params=params).json()
    expected = build_comparisons(SAMPLES / "compare" / "refund_ok.jsonl", [SAMPLES / "compare" / "refund_outage.jsonl"],
                                 datetime.now(UTC)).to_dict()  # fmt: skip
    # identical to the CLI result, except that the server's directory is replaced by plain file names
    expected = without_paths(expected, SAMPLES / "compare")
    assert data["comparisons"][0]["findings"] == expected["comparisons"][0]["findings"]
    assert "`aobs-server analyze refund_outage.jsonl`" in data["comparisons"][0]["findings"][0]["next_step"]
    assert data["source"]["kind"] == "sample"


def test_compare_groups_matches_the_cli_library(client: TestClient) -> None:
    params = [("baseline", "baseline.jsonl"), ("current", "slow.jsonl")]
    data = client.get("/api/datasets/sample-repeated/compare-groups", params=params).json()
    expected = build_group_report([SAMPLES / "groups" / "baseline.jsonl"], [SAMPLES / "groups" / "slow.jsonl"],
                                  datetime.now(UTC)).to_dict()  # fmt: skip
    assert [(m["key"], m["verdict"]) for m in data["metrics"]] == [
        (m["key"], m["verdict"]) for m in expected["metrics"]
    ]
    assert next(m for m in data["metrics"] if m["key"] == "run.duration")["verdict"] == "likely_regression"


def test_compare_errors_are_reported_not_hidden(client: TestClient) -> None:
    missing = client.get("/api/datasets/sample-pairs/compare", params={"baseline": "refund_ok.jsonl"})
    assert missing.status_code == 400 and "current" in missing.json()["detail"]
    groups = client.get("/api/datasets/sample-repeated/compare", params={"baseline": "baseline.jsonl",
                                                                         "current": "slow.jsonl"})  # fmt: skip
    assert groups.status_code == 400 and "holds 10 runs" in groups.json()["detail"]  # needs a run selection
    bad_prefix = client.get(
        "/api/datasets/sample-pairs/compare",
        params={"baseline": "refund_ok.jsonl", "current": "refund_ok.jsonl", "baseline_trace": "XYZ!"},
    )
    assert bad_prefix.status_code == 400
    too_many = [("baseline", "baseline.jsonl")] * 21 + [("current", "slow.jsonl")]
    assert client.get("/api/datasets/sample-repeated/compare-groups", params=too_many).status_code == 400
    traversal = [("baseline", "../traces/retry_storm.jsonl"), ("current", "slow.jsonl")]
    assert client.get("/api/datasets/sample-repeated/compare-groups", params=traversal).status_code == 400


# --------------------------------------------------------------------------- privacy


def test_no_endpoint_returns_trace_content(client: TestClient) -> None:
    raw = (SAMPLES / "traces" / "retry_storm.jsonl").read_text() + (
        SAMPLES / "traces" / "slow_and_repeated.jsonl"
    ).read_text()
    assert all(s in raw for s in SENSITIVE)  # the inputs really hold content (recorded with capture="full")
    bodies = [
        client.get("/api/datasets/sample-failures/overview").text,
        client.get("/api/datasets/sample-failures/runs").text,
    ]
    for run in client.get("/api/datasets/sample-failures/runs").json()["runs"]:
        bodies.append(client.get(f"/api/datasets/sample-failures/runs/{run['trace_id']}").text)
    for secret in SENSITIVE:
        assert not any(secret in body for body in bodies), secret


def test_secrets_in_local_files_are_scrubbed(local_dir: Path, local_client: TestClient) -> None:
    batch = json.loads((local_dir / "outage.jsonl").read_text().splitlines()[0])
    for event in batch["events"]:
        if event.get("status") == "error":
            event["error"]["message"] = "failed with token=supersecretvalue Bearer abcdefghijklmnop12345"
            event["attributes"] = {"app.note": "password=hunter2pw"}
    (local_dir / "outage.jsonl").write_text(json.dumps(batch) + "\n")
    runs = local_client.get("/api/datasets/local/runs", headers=auth()).json()["runs"]
    body = local_client.get(f"/api/datasets/local/runs/{runs[0]['trace_id']}", headers=auth()).text
    assert "[REDACTED" in body
    for secret in ("supersecretvalue", "abcdefghijklmnop12345", "hunter2pw"):
        assert secret not in body


def test_responses_never_reveal_server_paths(local_dir: Path, local_client: TestClient) -> None:
    (local_dir / "broken.jsonl").write_text("{not json\n")
    shutil.copy(SAMPLES / "compare" / "refund_ok.jsonl", local_dir / "ok.jsonl")
    sample_dirs = [str(SAMPLES), str(SAMPLES.resolve())]
    requests = [
        ("/api/datasets/sample-pairs/compare", {"baseline": "refund_ok.jsonl", "current": "refund_outage.jsonl"}, {}),
        ("/api/datasets/sample-repeated/compare", {"baseline": "baseline.jsonl", "current": "slow.jsonl"}, {}),  # 400
        (
            "/api/datasets/sample-repeated/compare-groups",
            [("baseline", "baseline.jsonl"), ("current", "slow.jsonl")],
            {},
        ),
        ("/api/datasets/sample-repeated/overview", {}, {}),
        ("/api/datasets/local/files", {}, auth()),
        ("/api/datasets/local/overview", {}, auth()),
        ("/api/datasets/local/compare", {"baseline": "ok.jsonl", "current": "outage.jsonl"}, auth()),
        ("/api/datasets/local/compare-groups", [("baseline", "ok.jsonl"), ("current", "outage.jsonl")], auth()),
    ]
    for path, params, headers in requests:
        body = local_client.get(path, params=params, headers=headers).text
        for directory in [*sample_dirs, str(local_dir), str(local_dir.resolve())]:
            assert directory not in body, (path, directory)
    files = local_client.get("/api/datasets/local/files", headers=auth()).json()
    assert any(p.startswith("broken.jsonl:1:") for p in files["input_problems"])  # file names are kept
    compared = local_client.get(
        "/api/datasets/sample-pairs/compare", params={"baseline": "refund_ok.jsonl", "current": "refund_outage.jsonl"}
    ).json()
    assert compared["comparisons"][0]["baseline"]["source"] == "refund_ok.jsonl"


# --------------------------------------------------------------------------- local data: token, files


def test_local_dataset_requires_the_token(local_client: TestClient) -> None:
    listing = local_client.get("/api/datasets").json()["datasets"]
    local = next(d for d in listing if d["id"] == "local")
    assert local["requires_token"] and "traces" not in local["description"]  # no directory path without a token
    for path in ("overview", "runs", "files"):
        response = local_client.get(f"/api/datasets/local/{path}")
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Bearer")
        assert TOKEN not in response.text
        assert (
            local_client.get(f"/api/datasets/local/{path}", headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )
        assert local_client.get(f"/api/datasets/local/{path}", headers=auth()).status_code == 200
    assert local_client.get("/api/datasets/sample-pairs/overview").status_code == 200  # samples stay public


def test_dashboard_config_refuses_local_data_without_a_token(local_dir: Path) -> None:
    with pytest.raises(ValueError, match="token"):
        DashboardConfig(data_dir=local_dir)


def test_empty_local_directory_gives_an_empty_dataset(tmp_path: Path) -> None:
    app = create_app(DashboardConfig(samples_dir=None, data_dir=tmp_path, token=TOKEN))
    client = TestClient(app)
    data = client.get("/api/datasets/local/overview", headers=auth()).json()
    assert data["runs"] == 0 and data["failure_rate"] is None and data["median_duration_ms"] is None
    assert client.get("/api/datasets/local/runs", headers=auth()).json()["runs"] == []
    assert [d["id"] for d in client.get("/api/datasets").json()["datasets"]] == ["local"]


def test_malformed_files_are_reported_and_symlinks_ignored(local_dir: Path, local_client: TestClient,
                                                            tmp_path: Path) -> None:  # fmt: skip
    (local_dir / "broken.jsonl").write_text("{not json\n")
    outside = tmp_path / "secret.jsonl"
    outside.write_text((SAMPLES / "traces" / "healthy_run.jsonl").read_text())
    os.symlink(outside, local_dir / "linked.jsonl")
    files = local_client.get("/api/datasets/local/files", headers=auth()).json()
    assert [f["name"] for f in files["files"]] == ["broken.jsonl", "outage.jsonl"]
    assert any("linked.jsonl: not a regular file" in s for s in files["skipped"])
    assert any("broken.jsonl:1: not valid JSON" in p for p in files["input_problems"])
    assert local_client.get("/api/datasets/local/overview", headers=auth()).json()["runs"] == 1


def test_new_files_are_picked_up_without_a_restart(local_dir: Path, local_client: TestClient) -> None:
    assert local_client.get("/api/datasets/local/overview", headers=auth()).json()["runs"] == 1
    shutil.copy(SAMPLES / "traces" / "healthy_run.jsonl", local_dir / "healthy.jsonl")
    assert local_client.get("/api/datasets/local/overview", headers=auth()).json()["runs"] == 2


# --------------------------------------------------------------------------- app surface and headers


def test_ui_and_security_headers(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200 and "<title>AOBS dashboard</title>" in page.text
    for response in (
        page,
        client.get("/api/datasets"),
        client.get("/static/app.js"),
        client.get("/api/datasets/nope/runs"),
    ):
        assert "script-src 'self'" in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_route_inventory_is_read_only(client: TestClient) -> None:
    routes = {(tuple(sorted(r.methods or ())), r.path) for r in client.app.routes if hasattr(r, "methods")}
    assert routes == {
        (("GET",), "/"),
        (("GET",), "/api/health"),
        (("GET",), "/api/datasets"),
        (("GET",), "/api/datasets/{dataset_id}/files"),
        (("GET",), "/api/datasets/{dataset_id}/overview"),
        (("GET",), "/api/datasets/{dataset_id}/runs"),
        (("GET",), "/api/datasets/{dataset_id}/runs/{trace_id}"),
        (("GET",), "/api/datasets/{dataset_id}/compare"),
        (("GET",), "/api/datasets/{dataset_id}/compare-groups"),
    }
    assert client.post("/api/datasets").status_code == 405


def test_the_dashboard_never_loads_the_database_layer() -> None:
    """No dashboard route reads stored, tenant-scoped data: the storage and auth layers are not imported."""
    probe = "import sys, aobs_server.dashboard; print(sorted(m for m in sys.modules if m.startswith('aobs_server.')))"
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True).stdout
    assert "aobs_server.storage" not in out and "aobs_server.auth" not in out


def test_static_files_never_use_inner_html() -> None:
    """Every API value is inserted as text; a trace cannot inject markup into the page."""
    source = (REPO_ROOT / "server/src/aobs_server/dashboard/static/app.js").read_text()
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert forbidden not in source, forbidden


# --------------------------------------------------------------------------- CLI


def test_cli_dashboard_starts_with_a_token_for_local_data(
    local_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started: dict[str, Any] = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: started.update(app=app, **kw))
    monkeypatch.setenv("AOBS_DASHBOARD_TOKEN", TOKEN)
    assert main(["dashboard", "--data-dir", str(local_dir), "--port", "8899"]) == 0
    out = capsys.readouterr().out
    assert started["host"] == "127.0.0.1" and started["port"] == 8899
    assert f"http://localhost:8899/#token={TOKEN}" in out
    client = TestClient(started["app"])
    assert client.get("/api/datasets/local/overview").status_code == 401
    assert client.get("/api/datasets/local/overview", headers=auth()).status_code == 200


def test_cli_dashboard_generates_a_random_token(
    local_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: None)
    monkeypatch.delenv("AOBS_DASHBOARD_TOKEN", raising=False)
    main(["dashboard", "--data-dir", str(local_dir)])
    first = capsys.readouterr().out.split("#token=")[1].split()[0]
    main(["dashboard", "--data-dir", str(local_dir)])
    second = capsys.readouterr().out.split("#token=")[1].split()[0]
    assert len(first) >= 32 and first != second


def test_cli_dashboard_samples_only_needs_no_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: None)
    assert main(["dashboard"]) == 0
    out = capsys.readouterr().out
    assert "http://localhost:8800/" in out and "token" not in out


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["dashboard", "--data-dir", "/nonexistent/aobs"], 1),
        (["dashboard", "--no-samples"], 2),
        (["dashboard", "--port", "0"], 2),
    ],
)
def test_cli_dashboard_rejects_bad_options(argv: list[str], code: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: pytest.fail("should not start"))
    assert main(argv) == code


def test_cli_dashboard_warns_when_listening_publicly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: None)
    main(["dashboard", "--host", "0.0.0.0"])
    assert "reachable from other machines" in capsys.readouterr().out
