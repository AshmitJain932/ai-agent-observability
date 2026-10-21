"""The PostgreSQL test harness itself: discovery, isolation from installed instances, cleanup."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

import pg

REPO = Path(__file__).resolve().parents[2]

# --------------------------------------------------------------------------- discovery (no server)


@pytest.mark.parametrize(
    ("output", "major"),
    [
        ("postgres (PostgreSQL) 16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)\n", 16),
        ("postgres (PostgreSQL) 16.4\n", 16),
        ("postgres (PostgreSQL) 17beta1\n", 17),
    ],
)
def test_parse_major(output: str, major: int) -> None:
    assert pg.parse_major(output) == major


def test_parse_major_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        pg.parse_major("not postgres")


def _fake_bindir(path: Path, version_line: str, tools: tuple[str, ...] = pg.TOOLS) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for tool in tools:
        script = path / tool
        script.write_text(f"#!/bin/sh\necho '{version_line}'\n")
        script.chmod(0o755)
    return path


def test_explicit_bin_that_is_missing_tools_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(pg.PgUnavailable) as info:
        pg.find_bindir({"AOBS_PG_BIN": str(tmp_path), "PATH": ""}, candidates=())
    message = str(info.value)
    assert "AOBS_PG_BIN" in message and "missing initdb" in message
    assert "AOBS_TEST_DATABASE_URL" in message and "-m 'not pg'" in message


def test_explicit_bin_is_not_silently_replaced_by_another_install(tmp_path: Path) -> None:
    good = _fake_bindir(tmp_path / "good", "postgres (PostgreSQL) 16.9")
    with pytest.raises(pg.PgUnavailable):
        pg.find_bindir({"AOBS_PG_BIN": str(tmp_path / "missing"), "PATH": str(good)}, candidates=())


def test_wrong_major_version_is_rejected(tmp_path: Path) -> None:
    old = _fake_bindir(tmp_path / "pg15", "postgres (PostgreSQL) 15.2")
    with pytest.raises(pg.PgUnavailable, match="PostgreSQL 15, but 16 is required"):
        pg.find_bindir({"AOBS_PG_BIN": str(old)}, candidates=())


def test_discovery_searches_path_and_candidates(tmp_path: Path) -> None:
    old = _fake_bindir(tmp_path / "pg15", "postgres (PostgreSQL) 15.2")
    good = _fake_bindir(tmp_path / "pg16", "postgres (PostgreSQL) 16.9")
    assert pg.find_bindir({"PATH": str(old)}, candidates=(str(good),)) == good


def test_nothing_found_explains_options(tmp_path: Path) -> None:
    with pytest.raises(pg.PgUnavailable) as info:
        pg.find_bindir({"PATH": str(tmp_path)}, candidates=())
    assert "no PostgreSQL binaries found" in str(info.value)
    assert "AOBS_TEST_DATABASE_URL" in str(info.value)


def test_unavailable_postgres_is_an_error_not_a_skip() -> None:
    """With no usable binaries, pg tests error out; `-m 'not pg'` deselects them instead."""
    env = {k: v for k, v in os.environ.items() if k != "AOBS_TEST_DATABASE_URL"}
    env["AOBS_PG_BIN"] = "/nonexistent/aobs-pg-bin"
    target = "server/tests/test_pg_harness.py::test_server_is_postgres_16"
    base = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", target]

    failed = subprocess.run(base, cwd=REPO, env=env, capture_output=True, text=True, timeout=300, check=False)
    assert failed.returncode != 0
    assert "1 error" in failed.stdout and "skipped" not in failed.stdout
    assert "AOBS_PG_BIN is set but unusable" in failed.stdout

    deselected = subprocess.run(
        [*base, "-m", "not pg"], cwd=REPO, env=env, capture_output=True, text=True, timeout=300, check=False
    )
    assert "1 deselected" in deselected.stdout, deselected.stdout


def test_stale_cluster_directories_are_reaped_only_when_ours_and_dead(tmp_path: Path) -> None:
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    dead_pid = int(dead.stdout)
    stale = tmp_path / "aobs-pg-stale"
    stale.mkdir()
    (stale / pg.MARKER_FILE).write_text(json.dumps({"pid": dead_pid}))
    live = tmp_path / "aobs-pg-live"
    live.mkdir()
    (live / pg.MARKER_FILE).write_text(json.dumps({"pid": os.getpid()}))
    unmarked = tmp_path / "aobs-pg-someone-else"
    unmarked.mkdir()
    unrelated = tmp_path / "other-dir"
    unrelated.mkdir()
    (unrelated / pg.MARKER_FILE).write_text(json.dumps({"pid": dead_pid}))

    reaped = pg.reap_stale_clusters(None, None, base_dir=str(tmp_path))

    assert reaped == [stale]
    assert not stale.exists()
    assert live.exists() and unmarked.exists() and unrelated.exists()


# --------------------------------------------------------------------------- live server


def test_server_is_postgres_16(pg_server: pg.PgServer) -> None:
    assert pg_server.server_major() == 16


def test_throwaway_cluster_is_private_and_socket_only(pg_server: pg.PgServer) -> None:
    if not isinstance(pg_server, pg.ThrowawayCluster):
        pytest.skip("AOBS_TEST_DATABASE_URL is set: the external server's settings are not ours")
    with pg_server.admin_connect() as conn:
        settings = {
            name: str(conn.execute(f"SHOW {name}").fetchone()[0])  # type: ignore[index]
            for name in ("listen_addresses", "data_directory", "unix_socket_directories", "fsync")
        }
    assert settings["listen_addresses"] == ""  # no TCP listener at all
    assert Path(settings["data_directory"]).resolve() == pg_server.data_dir.resolve()
    assert settings["unix_socket_directories"] == str(pg_server.socket_dir)
    assert settings["fsync"] == "on"
    assert not settings["data_directory"].startswith("/var/lib/postgresql")
    assert pg_server.root.name.startswith(pg.CLUSTER_DIR_PREFIX)


def test_each_database_is_fresh_and_independent(pg_server: pg.PgServer, pg_database_url: URL) -> None:
    engine = create_engine(pg_database_url)
    try:
        with engine.begin() as conn:
            assert conn.execute(text("SELECT current_database()")).scalar_one() == pg_database_url.database
            conn.execute(text("CREATE TABLE probe (id int)"))
            conn.execute(text("INSERT INTO probe VALUES (1)"))
    finally:
        engine.dispose()

    other = pg_server.create_database(template=pg_server.template)
    try:
        with psycopg.connect(pg_server.libpq_url(other)) as conn:
            exists = conn.execute("SELECT to_regclass('public.probe') IS NOT NULL").fetchone()
        assert exists == (False,)
    finally:
        pg_server.drop_database(other)


def test_created_databases_are_dropped(pg_server: pg.PgServer) -> None:
    name = pg_server.create_database()
    pg_server.drop_database(name)
    with pg_server.admin_connect() as conn:
        assert conn.execute("SELECT count(*) FROM pg_database WHERE datname = %s", (name,)).fetchone() == (0,)


@pytest.mark.parametrize("name", ["postgres", "template1", "aobs_test_not_mine"])
def test_refuses_to_drop_databases_it_did_not_create(pg_server: pg.PgServer, name: str) -> None:
    with pytest.raises(ValueError, match="refusing to drop"):
        pg_server.drop_database(name)
