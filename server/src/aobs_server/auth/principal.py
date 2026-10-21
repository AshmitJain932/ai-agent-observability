"""The authenticated caller: one API key, bound to one project."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class Principal:
    key_id: UUID
    project_id: UUID
    scopes: frozenset[str]
    environment: str | None  # if set, the key may only write this environment
