"""Alembic environment: online migrations only, URL from the programmatic config."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from aobs_server.storage.tables import metadata

target_metadata = metadata


def run_migrations_online() -> None:
    url = context.config.get_main_option("sqlalchemy.url")
    assert url, "sqlalchemy.url must be set"
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():  # pragma: no cover
    raise SystemExit("offline (SQL script) migrations are not supported")
run_migrations_online()
