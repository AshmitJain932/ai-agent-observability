"""`aobs-server serve`: refuses to start on a missing or outdated database; never logs SQL."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.engine import URL

from aobs_server.cli import main


def test_serve_needs_a_database_url(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("AOBS_DATABASE_URL", raising=False)
    assert main(["serve"]) == 2
    assert "AOBS_DATABASE_URL is not set" in capsys.readouterr().err


def test_serve_refuses_an_unreachable_database_without_echoing_the_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AOBS_DATABASE_URL", "postgresql://aobs:s3cr3t-pw@127.0.0.1:1/aobs?connect_timeout=2")
    assert main(["serve"]) == 1
    err = capsys.readouterr().err
    assert "cannot connect to the database" in err and "s3cr3t-pw" not in err


def test_serve_refuses_an_unmigrated_database(
    pg_database_url: URL, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AOBS_DATABASE_URL", pg_database_url.render_as_string(hide_password=False))
    assert main(["serve"]) == 1
    assert "run `aobs-server db upgrade`" in capsys.readouterr().err


@pytest.fixture
def restore_logging() -> Iterator[None]:
    loggers = [logging.getLogger("aobs"), logging.getLogger("sqlalchemy")]
    saved = [(lg, list(lg.handlers), lg.level) for lg in loggers]
    yield
    for lg, handlers, level in saved:
        lg.handlers[:] = handlers
        lg.setLevel(level)


@pytest.mark.usefixtures("restore_logging")
def test_serve_starts_the_collector_on_a_migrated_database(
    db_url: URL, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import uvicorn

    started: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: started.update(app=app, **kw))
    monkeypatch.setenv("AOBS_DATABASE_URL", db_url.render_as_string(hide_password=False))
    assert main(["serve", "--port", "8123"]) == 0
    assert (started["host"], started["port"]) == ("127.0.0.1", 8123)
    assert any(getattr(r, "path", None) == "/v1/ingest" for r in started["app"].routes)
    assert "POST /v1/ingest" in capsys.readouterr().out
    assert not logging.getLogger("sqlalchemy.engine").isEnabledFor(logging.INFO)  # no SQL in the logs
