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
_ACCEPTED_SCHEMES = frozenset({"postgres", "postgresql", "postgresql+psycopg"})


class ConfigError(ValueError):
    """A setting is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    database_url: URL | None = None  # AOBS_DATABASE_URL; required to serve, optional for `config`

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        source = os.environ if env is None else env
        raw = source.get("AOBS_DATABASE_URL", "").strip()
        return cls(database_url=parse_database_url(raw) if raw else None)

    def require_database_url(self) -> URL:
        if self.database_url is None:
            raise ConfigError("AOBS_DATABASE_URL is not set")
        return self.database_url

    def describe(self) -> dict[str, str | None]:
        """Effective settings for display. Passwords are masked."""
        url = self.database_url
        return {"database_url": None if url is None else url.render_as_string(hide_password=True)}


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
