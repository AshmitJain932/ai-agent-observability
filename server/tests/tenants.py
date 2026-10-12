"""Helpers to create organizations, projects and keys in a test database."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Engine

from aobs_server.auth.service import create_key
from aobs_server.project_settings import ProjectSettings
from aobs_server.storage import tenancy


def make_project(engine: Engine, name: str = "proj", **settings: Any) -> UUID:
    with engine.begin() as conn:
        org = tenancy.create_organization(conn, f"org-{name}")
        return tenancy.create_project(conn, org, name, ProjectSettings(**settings))


def make_key(
    engine: Engine, project_id: UUID, scopes: tuple[str, ...] = ("ingest",), environment: str | None = None
) -> tuple[UUID, str]:
    with engine.begin() as conn:
        key_id, key = create_key(conn, project_id, "test", scopes, environment)
    return key_id, key.secret


def bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}
