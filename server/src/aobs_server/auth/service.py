"""Key creation and authentication against storage."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Connection

from ..errors import Unauthorized
from ..storage import tenancy
from .keys import NewKey, generate_key, parse_bearer
from .principal import Principal


def create_key(
    conn: Connection, project_id: UUID, name: str, scopes: tuple[str, ...], environment: str | None
) -> tuple[UUID, NewKey]:
    """Create a key. The returned secret is shown to the operator once and is not stored."""
    key = generate_key()
    key_id = tenancy.insert_api_key(conn, project_id, name, key.prefix, key.key_hash, scopes, environment)
    return key_id, key


def authenticate(conn: Connection, authorization_header: str | None) -> Principal:
    """Principal for an ``Authorization: Bearer`` header; Unauthorized for anything else."""
    key = tenancy.find_active_key(conn, parse_bearer(authorization_header))
    if key is None:
        raise Unauthorized()
    return Principal(key_id=key.id, project_id=key.project_id, scopes=key.scopes, environment=key.environment)
