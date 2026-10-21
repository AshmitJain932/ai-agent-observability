"""Migrations create exactly the schema in aobs_server.storage.tables."""

from __future__ import annotations

import json

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect
from sqlalchemy.engine import URL

import pg
from aobs_server.cli import main
from aobs_server.storage import migrate
from aobs_server.storage.db import make_engine
from aobs_server.storage.tables import metadata

TABLES = {
    "organizations",
    "projects",
    "api_keys",
    "environments",
    "resources",
    "traces",
    "spans",
    "span_events",
    "span_content",
    "trace_tombstones",
    "ingest_diagnostics",
}


def test_metadata_lists_the_planned_tables() -> None:
    assert set(metadata.tables) == TABLES


def test_migrated_database_matches_metadata(engine: Engine) -> None:
    assert set(inspect(engine).get_table_names()) == TABLES | {"alembic_version"}
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), metadata)
    assert diff == []
    assert migrate.current_revision(engine) == "0001"


def test_tenant_tables_lead_with_project_id(engine: Engine) -> None:
    insp = inspect(engine)
    for table in TABLES - {"organizations", "projects", "api_keys", "ingest_diagnostics"}:
        assert insp.get_pk_constraint(table)["constrained_columns"][0] == "project_id", table


def test_downgrade_and_upgrade_round_trip(pg_server: pg.PgServer, pg_database_url: URL) -> None:
    migrate.upgrade(pg_database_url)
    migrate.downgrade(pg_database_url, "base")
    eng = make_engine(pg_database_url, pool_size=1)
    try:
        assert set(inspect(eng).get_table_names()) == {"alembic_version"}
    finally:
        eng.dispose()
    migrate.upgrade(pg_database_url)
    eng = make_engine(pg_database_url, pool_size=1)
    try:
        assert set(inspect(eng).get_table_names()) == TABLES | {"alembic_version"}
    finally:
        eng.dispose()


def test_cli_db_upgrade_and_current(
    pg_database_url: URL, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AOBS_DATABASE_URL", pg_database_url.render_as_string(hide_password=False))
    assert main(["db", "upgrade"]) == 0
    capsys.readouterr()
    assert main(["db", "current"]) == 0
    assert json.loads(capsys.readouterr().out) == {"revision": "0001"}


def test_cli_db_upgrade_without_database_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AOBS_DATABASE_URL", raising=False)
    assert main(["db", "upgrade"]) == 2
    assert "AOBS_DATABASE_URL is not set" in capsys.readouterr().err
