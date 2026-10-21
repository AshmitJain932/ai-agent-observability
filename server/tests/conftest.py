from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.engine import URL

import pg
from aobs_server.storage import migrate
from aobs_server.storage.db import make_engine

PG_FIXTURES = frozenset({"pg_server", "pg_database_url", "migrated_template", "db_url", "engine"})


@pytest.hookimpl(tryfirst=True)  # before `-m` deselection runs
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Every test that uses a PostgreSQL fixture is marked ``pg`` automatically."""
    for item in items:
        if PG_FIXTURES & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.pg)


@pytest.fixture(scope="session")
def pg_server() -> Iterator[pg.PgServer]:
    try:
        server = pg.start_from_env()
    except pg.PgUnavailable as exc:
        pytest.fail(str(exc), pytrace=False)
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def pg_database_url(pg_server: pg.PgServer) -> Iterator[URL]:
    """SQLAlchemy URL of a fresh database, cloned from the session template, dropped afterwards."""
    name = pg_server.create_database(template=pg_server.template)
    try:
        yield pg_server.sqlalchemy_url(name)
    finally:
        pg_server.drop_database(name)


@pytest.fixture(scope="session")
def migrated_template(pg_server: pg.PgServer) -> str:
    """A database migrated to the latest schema once per session; per-test databases clone it."""
    name = pg_server.create_database(template=pg_server.template)
    migrate.upgrade(pg_server.sqlalchemy_url(name))
    return name


@pytest.fixture
def db_url(pg_server: pg.PgServer, migrated_template: str) -> Iterator[URL]:
    """SQLAlchemy URL of a fresh, fully migrated database, dropped afterwards."""
    name = pg_server.create_database(template=migrated_template)
    try:
        yield pg_server.sqlalchemy_url(name)
    finally:
        pg_server.drop_database(name)


@pytest.fixture
def engine(db_url: URL) -> Iterator[Engine]:
    eng = make_engine(db_url, pool_size=4)
    try:
        yield eng
    finally:
        eng.dispose()
