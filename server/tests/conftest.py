from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import URL

import pg

PG_FIXTURES = frozenset({"pg_server", "pg_database_url"})


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
