"""Server settings, read from ``AOBS_*`` environment variables.

Settings are a frozen dataclass built once at startup; invalid values raise :class:`ConfigError`
with the variable name, so a misconfigured server fails before it accepts traffic.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

DRIVER = "postgresql+psycopg"
SCHEMA_MAX_EVENTS = 1000  # the wire schema's own limit on events per batch
_ACCEPTED_SCHEMES = frozenset({"postgres", "postgresql", "postgresql+psycopg"})


class ConfigError(ValueError):
    """A setting is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    database_url: URL | None = None  # AOBS_DATABASE_URL; required to serve, optional for `config`
    clock_skew_threshold_s: float = 300.0  # AOBS_CLOCK_SKEW_THRESHOLD_S; |received_at - sent_at| flagged above
    max_body_bytes: int = 1_048_576  # AOBS_MAX_BODY_BYTES; larger ingest bodies get 413
    max_events_per_batch: int = SCHEMA_MAX_EVENTS  # AOBS_MAX_EVENTS_PER_BATCH; more events get 413

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        source = os.environ if env is None else env
        raw = source.get("AOBS_DATABASE_URL", "").strip()
        return cls(
            database_url=parse_database_url(raw) if raw else None,
            clock_skew_threshold_s=_number(
                source, "AOBS_CLOCK_SKEW_THRESHOLD_S", cls.clock_skew_threshold_s, 1, 2_592_000
            ),
            max_body_bytes=_integer(source, "AOBS_MAX_BODY_BYTES", cls.max_body_bytes, 1024, 16_777_216),
            max_events_per_batch=_integer(
                source, "AOBS_MAX_EVENTS_PER_BATCH", cls.max_events_per_batch, 1, SCHEMA_MAX_EVENTS
            ),
        )

    def require_database_url(self) -> URL:
        if self.database_url is None:
            raise ConfigError("AOBS_DATABASE_URL is not set")
        return self.database_url

    def describe(self) -> dict[str, str | float | int | None]:
        """Effective settings for display. Passwords are masked."""
        url = self.database_url
        return {
            "database_url": None if url is None else url.render_as_string(hide_password=True),
            "clock_skew_threshold_s": self.clock_skew_threshold_s,
            "max_body_bytes": self.max_body_bytes,
            "max_events_per_batch": self.max_events_per_batch,
        }


def _number(source: Mapping[str, str], name: str, default: float, low: float, high: float) -> float:
    raw = source.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low:g} and {high:g}, got {raw}")
    return value


def _integer(source: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = source.get(name, "").strip()
    if not raw:
        return default
    if not (raw.isascii() and raw.isdigit()):
        raise ConfigError(f"{name} must be a whole number, got {raw!r}")
    value = int(raw)
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}, got {raw}")
    return value


def parse_database_url(raw: str) -> URL:
    """Parse a PostgreSQL URL and normalize it to the psycopg 3 driver."""
    try:
        url = make_url(raw)
    except ArgumentError:
        raise ConfigError("AOBS_DATABASE_URL is not a valid database URL") from None
    if url.drivername not in _ACCEPTED_SCHEMES:
        raise ConfigError(
            f"AOBS_DATABASE_URL must be a PostgreSQL URL (postgresql://...), got scheme {url.drivername!r}"
        )
    return url.set(drivername=DRIVER)
