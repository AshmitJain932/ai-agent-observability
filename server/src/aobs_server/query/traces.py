"""Read-only trace retrieval behind ``GET /v1/projects/{project_id}/traces/{trace_id}[/events]``.

The caller has already been authorized for the project (``auth.authorize``). Here:

* Every query filters on that project id, so another project's trace with the same id is never
  read. A trace id that is malformed, unknown in this project, or deleted (tombstoned) is
  :class:`~aobs_server.errors.NotFound`, the same answer as for another project's resources.
* Stored events go through the project's **current** privacy policy again (the M4 step), so
  lowering a project's capture ceiling, or turning off error messages or stack traces, also hides
  data stored before the change. Content values, and fields from newer schema minors, are omitted
  unless ``include_content`` is set; content status markers (captured, redacted, not_captured, ...)
  are always shown.
* Spans come from the shared assembly code (``aobs_server.model``), exactly as the SDK tests and
  the offline tools assemble a trace, and are paginated by span id. Span events are a separate,
  chronologically paginated listing because a trace may hold up to 100,000 of them.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections import Counter
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection

from ..errors import BadRequest, NotFound
from ..ingest.prepare import prepare_event
from ..model.assembly import AssemblyConfig, assemble
from ..model.merge import merge_events
from ..model.versioning import AcceptedEvent
from ..model.wire import SPAN_ID_PATTERN, TRACE_ID_PATTERN, UUID_PATTERN, ecma_to_python
from ..project_settings import ProjectSettings
from ..storage import tenancy
from ..storage.traces import event_from_json, list_span_events, load_trace_events, span_event_counts, trace_row

DEFAULT_PAGE = 500
MAX_PAGE = 1000
TRACE_ID = ecma_to_python(TRACE_ID_PATTERN)
SPAN_ID = ecma_to_python(SPAN_ID_PATTERN)
EVENT_ID = ecma_to_python(UUID_PATTERN)
CONTENT_METADATA = ("status", "format", "truncated", "redaction_count")
_CURSOR = re.compile(r"^[A-Za-z0-9_-]{1,200}\Z")


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat().replace("+00:00", "Z")


def _settings(conn: Connection, project_id: UUID) -> ProjectSettings:
    settings = tenancy.project_settings(conn, project_id)
    if settings is None:  # the project was deleted after the key was checked
        raise NotFound()
    return settings


def _require_trace(conn: Connection, project_id: UUID, trace_id: str) -> Any:
    if TRACE_ID.search(trace_id) is None:
        raise NotFound()
    row = trace_row(conn, project_id, trace_id)
    if row is None:
        raise NotFound()
    return row


def _under_current_policy(events: list[AcceptedEvent], settings: ProjectSettings) -> list[AcceptedEvent]:
    counters: Counter[str] = Counter()
    return [prepare_event(e, settings, counters) for e in events]


def _strip_content_values(fields: dict[str, Any]) -> None:
    """Without ``include_content``: content parts keep only their metadata, and fields from newer
    schema minors (``__extra__``, stored only under a full ceiling because they may hold content)
    are left out entirely."""
    fields.pop("__extra__", None)
    content = fields.get("content")
    if isinstance(content, dict):
        fields["content"] = {
            direction: {k: v for k, v in part.items() if k in CONTENT_METADATA}
            for direction, part in content.items()
            if isinstance(part, dict)
        }


def summary(row: Any, settings: ProjectSettings, now: datetime) -> dict[str, Any]:
    """The stored trace summary; a stored ``open`` status becomes in_progress or incomplete now."""
    status = row.status
    if status == "open":
        reference = row.stale_reference_time
        status = "in_progress" if reference is not None and now - reference < settings.stale_after else "incomplete"
    return {
        "status": status,
        "started_at": _iso(row.started_at),
        "ended_at": _iso(row.ended_at),
        "root_name": row.root_name,
        "agent_name": row.agent_name,
        "session_id": row.session_id,
        "span_count": row.span_count,
        "open_span_count": row.open_span_count,
        "error_count": row.error_count,
        "span_event_count": row.span_event_count,
        "kind_counts": row.kind_counts,
        "tokens": row.tokens,
        "conflicts": row.conflicts,
        "orphan_count": row.orphan_count,
        "truncated": row.truncated,
        "environments": list(row.environments),
        "first_received_at": _iso(row.first_received_at),
        "last_received_at": _iso(row.last_received_at),
    }


def get_trace(
    conn: Connection,
    project_id: UUID,
    trace_id: str,
    *,
    now: datetime,
    limit: int = DEFAULT_PAGE,
    cursor: str | None = None,
    include_content: bool = False,
) -> dict[str, Any]:
    """Summary, tree structure and one page of assembled spans (ordered by span id)."""
    if cursor is not None and SPAN_ID.search(cursor) is None:
        raise BadRequest("cursor must be a span id returned as next_cursor")
    row = _require_trace(conn, project_id, trace_id)
    settings = _settings(conn, project_id)
    events = _under_current_policy(load_trace_events(conn, project_id, trace_id, include_span_events=False), settings)
    view = assemble(merge_events(events), now, AssemblyConfig(stale_after=settings.stale_after)).get(trace_id)
    if view is None:  # pragma: no cover - a stored trace row always has stored spans or events
        raise NotFound()
    counts, orphan_events = span_event_counts(conn, project_id, trace_id)
    ordered = [sid for sid in sorted(view.spans) if cursor is None or sid > cursor]
    page = ordered[:limit]
    spans = []
    for sid in page:
        node = view.spans[sid].to_dict()
        for key in ("start_time", "end_time"):  # RFC 3339 with "Z", like every other timestamp here
            if node[key] is not None:
                node[key] = node[key].replace("+00:00", "Z")
        if not include_content:
            _strip_content_values(node["fields"])
        node["event_count"] = counts.get(sid, 0)
        spans.append(node)
    return {
        "project_id": str(project_id),
        "trace_id": trace_id,
        "summary": summary(row, settings, now),
        "roots": list(view.roots),
        "orphans": [{"span_id": s, "reason": r} for s, r in view.orphans],
        "orphan_event_count": orphan_events,
        "spans": spans,
        "page": {"limit": limit, "next_cursor": page[-1] if len(ordered) > len(page) else None},
        "content": "included" if include_content else "omitted",
    }


def encode_event_cursor(time: datetime, event_id: str) -> str:
    return base64.urlsafe_b64encode(f"{time.isoformat()}|{event_id}".encode()).decode().rstrip("=")


def decode_event_cursor(cursor: str) -> tuple[datetime, str]:
    """Inverse of :func:`encode_event_cursor`; BadRequest for anything it did not produce."""
    invalid = BadRequest("cursor must be a value returned as next_cursor")
    if _CURSOR.match(cursor) is None:
        raise invalid
    try:
        text = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("ascii")
        time_text, _, event_id = text.partition("|")
        time = datetime.fromisoformat(time_text)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise invalid from None
    if time.tzinfo is None or EVENT_ID.search(event_id) is None:
        raise invalid
    return time, event_id


def get_span_events(
    conn: Connection,
    project_id: UUID,
    trace_id: str,
    *,
    span_id: str | None = None,
    limit: int = DEFAULT_PAGE,
    cursor: str | None = None,
) -> dict[str, Any]:
    """One page of a trace's span events in (time, event_id) order, optionally for one span."""
    after = None if cursor is None else decode_event_cursor(cursor)
    if span_id is not None and SPAN_ID.search(span_id) is None:
        raise BadRequest("span_id must be 16 lowercase hex characters")
    _require_trace(conn, project_id, trace_id)
    settings = _settings(conn, project_id)
    rows = list_span_events(conn, project_id, trace_id, span_id=span_id, after=after, limit=limit + 1)
    page = rows[:limit]
    stored = _under_current_policy([event_from_json(r.event) for r in page], settings)
    events = []
    for item in stored:
        body = item.event.model_dump(mode="json", exclude_unset=True)
        body.pop("type", None)
        body.pop("trace_id", None)
        events.append(body)
    last = page[-1] if len(rows) > len(page) else None
    return {
        "project_id": str(project_id),
        "trace_id": trace_id,
        "span_id": span_id,
        "events": events,
        "page": {
            "limit": limit,
            "next_cursor": None if last is None else encode_event_cursor(last.time, last.event_id),
        },
    }
