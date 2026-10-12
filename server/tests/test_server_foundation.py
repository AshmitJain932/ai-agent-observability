"""Settings, clock and command line."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from aobs_server.cli import main
from aobs_server.clock import FixedClock, SystemClock
from aobs_server.config import ConfigError, Settings

# --------------------------------------------------------------------------- settings


def test_defaults_without_environment() -> None:
    settings = Settings.from_env({})
    assert settings.database_url is None
    with pytest.raises(ConfigError, match="AOBS_DATABASE_URL is not set"):
        settings.require_database_url()


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+psycopg"])
def test_database_url_is_normalized_to_psycopg(scheme: str) -> None:
    url = Settings.from_env({"AOBS_DATABASE_URL": f"{scheme}://aobs:pw@db:5432/aobs"}).require_database_url()
    assert url.drivername == "postgresql+psycopg"
    assert (url.host, url.port, url.database, url.username) == ("db", 5432, "aobs", "aobs")


@pytest.mark.parametrize(
    "raw", ["sqlite:///aobs.db", "mysql://u@h/d", "postgresql+asyncpg://u@h/d", "not a url", "://"]
)
def test_non_postgres_or_malformed_urls_are_rejected(raw: str) -> None:
    with pytest.raises(ConfigError, match="AOBS_DATABASE_URL"):
        Settings.from_env({"AOBS_DATABASE_URL": raw})


def test_describe_masks_the_password() -> None:
    shown = Settings.from_env({"AOBS_DATABASE_URL": "postgresql://aobs:s3cr3t-pw@db/aobs"}).describe()
    assert shown["database_url"] is not None
    assert "s3cr3t-pw" not in shown["database_url"]
    assert "***" in shown["database_url"]


def test_config_error_does_not_echo_the_url() -> None:
    with pytest.raises(ConfigError) as info:
        Settings.from_env({"AOBS_DATABASE_URL": "mysql://user:s3cr3t-pw@db/x"})
    assert "s3cr3t-pw" not in str(info.value)


# --------------------------------------------------------------------------- clock


def test_system_clock_is_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)


def test_fixed_clock_moves_only_when_advanced() -> None:
    start = datetime(2026, 10, 12, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    clock = FixedClock(start)
    assert clock.now() == start and clock.now().tzinfo == UTC
    assert clock.now() == clock.now()
    assert clock.advance(timedelta(seconds=5)) == start + timedelta(seconds=5)


def test_fixed_clock_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FixedClock(datetime(2026, 10, 12))


# --------------------------------------------------------------------------- command line


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert capsys.readouterr().out.startswith("aobs-server 0.1.0")


def test_cli_requires_a_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main([])
    assert info.value.code == 2


def test_cli_config_prints_masked_settings(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("AOBS_DATABASE_URL", "postgresql://aobs:s3cr3t-pw@db/aobs")
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert "postgresql+psycopg://aobs:***@db/aobs" in out
    assert "s3cr3t-pw" not in out


def test_cli_config_error_exit_code(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("AOBS_DATABASE_URL", "sqlite:///x.db")
    assert main(["config"]) == 2
    assert "configuration error" in capsys.readouterr().err
