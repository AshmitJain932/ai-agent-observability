"""Engine creation."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL


def make_engine(url: URL | str, *, pool_size: int = 10) -> Engine:
    """Engine for the server. READ COMMITTED is explicit: the ingest locking protocol relies on
    each statement seeing everything committed before it starts (docs/architecture.md §5)."""
    return create_engine(
        url,
        isolation_level="READ COMMITTED",
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_pre_ping=True,
    )
