"""The sample recordings installed with the package, and ``aobs-server demo`` (the Docker demo's seed
step, docs/docker.md): the samples sent through a real collector on a local port into PostgreSQL."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.engine import URL

import aobs_server
from aobs_server.api import create_app
from aobs_server.cli import main
from aobs_server.config import Settings
from aobs_server.dashboard import DashboardConfig
from aobs_server.dashboard import create_app as create_dashboard
from aobs_server.dashboard.datasets import PACKAGED_SAMPLES, REPO_EXAMPLES
from aobs_server.demo import SAMPLE_DIRS, SEED_KEY_NAME, sample_batches
from aobs_server.storage import tenancy
from aobs_server.storage.tables import organizations, projects
from ingest_support import LiveServer
from support import REPO_ROOT

TOKEN = "demo-test-dashboard-token"
SAMPLE_IDS = ["sample-repeated", "sample-pairs", "sample-failures"]


def jsonl_files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for name in SAMPLE_DIRS for p in (root / name).glob("*.jsonl")}


# --------------------------------------------------------------------------- installed sample data


def test_packaged_samples_are_identical_to_the_repository_examples() -> None:
    assert PACKAGED_SAMPLES is not None
    packaged, examples = jsonl_files(PACKAGED_SAMPLES), jsonl_files(REPO_EXAMPLES)
    assert len(examples) == 17
    # After re-recording examples/diagnose, copy them: cp examples/diagnose/<dir>/*.jsonl \
    # server/src/aobs_server/dashboard/samples/<dir>/ for <dir> in groups, compare, traces.
    assert packaged.keys() == examples.keys()
    assert [name for name in examples if packaged[name] != examples[name]] == []


def test_dashboard_shows_the_installed_samples_by_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert PACKAGED_SAMPLES is not None
    assert PACKAGED_SAMPLES.is_relative_to(Path(aobs_server.__file__).resolve().parent)
    assert DashboardConfig().samples_dir == PACKAGED_SAMPLES
    started: dict[str, Any] = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: started.update(app=app))
    assert main(["dashboard"]) == 0
    assert "sample data not found" not in capsys.readouterr().out
    client = TestClient(started["app"])
    assert [d["id"] for d in client.get("/api/datasets").json()["datasets"]] == SAMPLE_IDS
    assert client.get("/api/datasets/sample-repeated/overview").json()["runs"] == 63


def test_samples_dir_still_accepts_the_repository_examples(monkeypatch: pytest.MonkeyPatch) -> None:
    started: dict[str, Any] = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: started.update(app=app))
    assert main(["dashboard", "--samples-dir", str(REPO_EXAMPLES)]) == 0
    assert started["app"].state.config.samples_dir == REPO_EXAMPLES
    client = TestClient(started["app"])
    assert [d["id"] for d in client.get("/api/datasets").json()["datasets"]] == SAMPLE_IDS


def test_a_copy_of_the_package_outside_the_repository_still_has_its_samples(tmp_path: Path) -> None:
    """What an installed (non-editable) aobs-server sees: no examples/ folder anywhere above it."""
    shutil.copytree(Path(aobs_server.__file__).resolve().parent, tmp_path / "aobs_server")
    script = (
        "import json, aobs_server; from aobs_server.dashboard.datasets import PACKAGED_SAMPLES, sample_datasets; "
        "print(json.dumps([aobs_server.__file__, str(PACKAGED_SAMPLES), [s.id for s in sample_datasets()]]))"
    )
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    module, samples, ids = json.loads(result.stdout)
    assert Path(module).is_relative_to(tmp_path) and Path(samples).is_relative_to(tmp_path)
    assert ids == SAMPLE_IDS and not Path(samples).is_relative_to(REPO_ROOT)


# --------------------------------------------------------------------------- aobs-server demo


@pytest.fixture
def cli(db_url: URL, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Any:
    monkeypatch.setenv("AOBS_DATABASE_URL", db_url.render_as_string(hide_password=False))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    def run(*argv: str) -> tuple[int, str, str]:
        code = main(list(argv))
        out = capsys.readouterr()
        return code, out.out, out.err

    return run


@pytest.fixture
def collector(engine: Engine) -> Iterator[str]:
    with LiveServer(create_app(Settings(), engine)) as server:  # the system clock, as `serve` uses
        yield server.url


def sample_trace_ids() -> set[str]:
    assert PACKAGED_SAMPLES is not None
    return sample_batches(PACKAGED_SAMPLES)[1]


def seed_keys(engine: Engine, project_id: UUID) -> list[tenancy.ApiKeyInfo]:
    with engine.connect() as conn:
        return tenancy.list_api_keys(conn, project_id)


def test_demo_seed_sends_every_sample_trace_through_the_collector(cli: Any, collector: str, engine: Engine) -> None:
    code, out, err = cli("demo", "seed", "--collector", collector)
    assert code == 0, err
    result = json.loads(out)
    assert result["seeded"] is True
    assert result["batches_sent"] == 21
    assert result["stored_traces"] == len(sample_trace_ids()) == 73
    assert result["counts"]["accepted"] == result["counts"]["received"] > 0
    assert result["counts"].get("rejected", 0) == 0
    assert "aobs_ik_" not in out + err
    project_id = UUID(result["project_id"])
    with engine.connect() as conn:
        settings = tenancy.project_settings(conn, project_id)
    assert settings is not None and settings.capture_ceiling == "metadata_only"
    # The temporary ingest key is revoked before seed returns; only its hash was ever stored.
    (key,) = seed_keys(engine, project_id)
    assert key.name == SEED_KEY_NAME and key.scopes == ("ingest",) and key.revoked_at is not None

    code, out, _ = cli("demo", "project-id")
    assert code == 0 and out.strip() == str(project_id)
    dashboard = TestClient(
        create_dashboard(DashboardConfig(samples_dir=None, token=TOKEN, project_id=project_id, engine=engine))
    )
    overview = dashboard.get("/api/datasets/stored/overview", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert overview["runs"] == 73 and overview["failed"] > 0


def test_demo_seed_twice_sends_nothing_the_second_time(cli: Any, collector: str, engine: Engine) -> None:
    first = json.loads(cli("demo", "seed", "--collector", collector)[1])
    code, out, _ = cli("demo", "seed", "--collector", collector)
    second = json.loads(out)
    assert code == 0
    assert second["seeded"] is False and second["batches_sent"] == 0
    assert (second["organization_id"], second["project_id"]) == (first["organization_id"], first["project_id"])
    assert second["stored_traces"] == 73
    assert len(seed_keys(engine, UUID(first["project_id"]))) == 1  # no second key was made
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(organizations)).scalar_one() == 1
        assert conn.execute(select(func.count()).select_from(projects)).scalar_one() == 1


def test_demo_seed_completes_a_partial_seed(cli: Any, collector: str, engine: Engine, tmp_path: Path) -> None:
    assert PACKAGED_SAMPLES is not None
    shutil.copytree(PACKAGED_SAMPLES / "traces", tmp_path / "traces")
    partial = json.loads(cli("demo", "seed", "--collector", collector, "--samples-dir", str(tmp_path))[1])
    assert partial["stored_traces"] == 5
    code, out, _ = cli("demo", "seed", "--collector", collector)
    full = json.loads(out)
    assert code == 0 and full["seeded"] is True and full["project_id"] == partial["project_id"]
    assert full["stored_traces"] == 73
    assert full["counts"]["duplicate"] > 0  # the five traces sent before are recognised, not stored twice
    keys = seed_keys(engine, UUID(full["project_id"]))
    assert len(keys) == 2 and all(k.revoked_at is not None for k in keys)


def test_demo_seed_without_a_collector_fails_and_revokes_its_key(cli: Any, engine: Engine) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = f"http://127.0.0.1:{s.getsockname()[1]}"
    code, out, err = cli("demo", "seed", "--collector", closed)
    assert code == 1 and out == ""
    assert "cannot send traces/" in err and "aobs_ik_" not in err
    code, out, _ = cli("demo", "project-id")
    assert code == 0  # the project exists; its key does not stay usable
    (key,) = seed_keys(engine, UUID(out.strip()))
    assert key.revoked_at is not None


def test_demo_project_id_before_seeding_is_an_error(cli: Any) -> None:
    code, out, err = cli("demo", "project-id")
    assert code == 1 and out == "" and "run `aobs-server demo seed`" in err


def test_demo_seed_rejects_a_collector_url_that_is_not_http(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AOBS_DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")  # never connected to
    assert main(["demo", "seed", "--collector", "file:///etc/passwd"]) == 1
    assert "must be an http:// or https:// URL" in capsys.readouterr().err
