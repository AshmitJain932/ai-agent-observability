"""The single authorization decision point. Every project-scoped operation calls ``authorize``.

* The project in the request is not the key's project -> NotFound (404, same as "does not
  exist", so keys cannot probe for other projects).
* The key lacks the scope for the action -> Forbidden (403; only ever about the key's own project).
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from ..errors import Forbidden, NotFound
from .principal import Principal

Action = Literal["ingest", "read", "delete"]
SCOPE_FOR_ACTION: dict[Action, str] = {"ingest": "ingest", "read": "read", "delete": "admin"}


def require_scope(principal: Principal, action: Action) -> None:
    scope = SCOPE_FOR_ACTION[action]
    if scope not in principal.scopes:
        raise Forbidden(f"this API key does not have the '{scope}' scope")


def authorize(principal: Principal, project_id: str | UUID, action: Action) -> UUID:
    """Return the project id as a UUID if ``principal`` may perform ``action`` on it."""
    try:
        requested = project_id if isinstance(project_id, UUID) else UUID(str(project_id))
    except ValueError:
        raise NotFound() from None
    if requested != principal.project_id:
        raise NotFound()
    require_scope(principal, action)
    return requested
