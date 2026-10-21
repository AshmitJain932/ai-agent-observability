"""Alembic migrations, driven programmatically (no alembic.ini needed)."""

from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine
from sqlalchemy.engine import URL

SCRIPT_LOCATION = "aobs_server.storage:migrations"


def alembic_config(url: URL | str) -> Config:
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    rendered = url.render_as_string(hide_password=False) if isinstance(url, URL) else url
    config.set_main_option("sqlalchemy.url", rendered.replace("%", "%%"))
    return config


def head_revision() -> str | None:
    """The latest migration shipped with this server."""
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    return ScriptDirectory.from_config(config).get_current_head()


def upgrade(url: URL | str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: URL | str, revision: str) -> None:
    command.downgrade(alembic_config(url), revision)


def current_revision(engine: Engine) -> str | None:
    from alembic.runtime.migration import MigrationContext

    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()
