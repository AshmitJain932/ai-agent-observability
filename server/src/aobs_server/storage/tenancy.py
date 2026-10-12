"""Organizations, projects and API keys."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Connection, Insert, insert, select, update

from ..project_settings import ProjectSettings
from .tables import api_keys, organizations, projects

SCOPES = ("ingest", "read", "admin")


@dataclass(frozen=True)
class ApiKeyInfo:
    """What may be shown about a stored key. There is no secret here to show."""

    id: UUID
    project_id: UUID
    name: str
    prefix: str
    scopes: tuple[str, ...]
    environment: str | None
    created_at: datetime
    revoked_at: datetime | None


def _returning_id(conn: Connection, statement: Insert) -> UUID:
    value = conn.execute(statement).scalar_one()
    assert isinstance(value, UUID)
    return value


def create_organization(conn: Connection, name: str) -> UUID:
    return _returning_id(conn, insert(organizations).values(name=name).returning(organizations.c.id))


def create_project(conn: Connection, organization_id: UUID, name: str, settings: ProjectSettings) -> UUID:
    return _returning_id(
        conn,
        insert(projects)
        .values(organization_id=organization_id, name=name, settings=settings.to_db())
        .returning(projects.c.id),
    )


def organization_exists(conn: Connection, organization_id: UUID) -> bool:
    return conn.execute(select(organizations.c.id).where(organizations.c.id == organization_id)).first() is not None


def project_settings(conn: Connection, project_id: UUID) -> ProjectSettings | None:
    raw = conn.execute(select(projects.c.settings).where(projects.c.id == project_id)).scalar_one_or_none()
    return None if raw is None else ProjectSettings.from_db(raw)


@dataclass(frozen=True)
class ActiveKey:
    id: UUID
    project_id: UUID
    scopes: frozenset[str]
    environment: str | None


def insert_api_key(
    conn: Connection,
    project_id: UUID,
    name: str,
    prefix: str,
    key_hash: bytes,
    scopes: tuple[str, ...],
    environment: str | None,
) -> UUID:
    """Store a key's hash and display prefix (never the key itself)."""
    unknown = set(scopes) - set(SCOPES)
    if unknown or not scopes:
        raise ValueError(f"scopes must be a non-empty subset of {SCOPES}")
    return _returning_id(
        conn,
        insert(api_keys)
        .values(
            project_id=project_id,
            name=name,
            prefix=prefix,
            key_hash=key_hash,
            scopes=sorted(set(scopes)),
            environment=environment,
        )
        .returning(api_keys.c.id),
    )


def list_api_keys(conn: Connection, project_id: UUID) -> list[ApiKeyInfo]:
    rows = conn.execute(
        select(
            api_keys.c.id,
            api_keys.c.project_id,
            api_keys.c.name,
            api_keys.c.prefix,
            api_keys.c.scopes,
            api_keys.c.environment,
            api_keys.c.created_at,
            api_keys.c.revoked_at,
        )
        .where(api_keys.c.project_id == project_id)
        .order_by(api_keys.c.created_at, api_keys.c.id)
    )
    return [
        ApiKeyInfo(r.id, r.project_id, r.name, r.prefix, tuple(r.scopes), r.environment, r.created_at, r.revoked_at)
        for r in rows
    ]


def revoke_api_key(conn: Connection, key_id: UUID, now: datetime) -> bool:
    result = conn.execute(
        update(api_keys).where(api_keys.c.id == key_id, api_keys.c.revoked_at.is_(None)).values(revoked_at=now)
    )
    return result.rowcount == 1


def find_active_key(conn: Connection, key_hash: bytes) -> ActiveKey | None:
    """The active key with this hash, or None (unknown or revoked)."""
    row = conn.execute(
        select(api_keys.c.id, api_keys.c.project_id, api_keys.c.scopes, api_keys.c.environment).where(
            api_keys.c.key_hash == key_hash, api_keys.c.revoked_at.is_(None)
        )
    ).first()
    if row is None:
        return None
    return ActiveKey(id=row.id, project_id=row.project_id, scopes=frozenset(row.scopes), environment=row.environment)
