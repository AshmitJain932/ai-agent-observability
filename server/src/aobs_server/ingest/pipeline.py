"""One ingest batch, end to end (docs/architecture.md §5, docs/ingestion.md).

``parse_body`` runs before anything expensive: strict JSON and the event-count limit.
``validate`` needs no database:

1. validate the envelope and every event (ADR-010); an invalid envelope rejects the batch;
2. an environment-bound key may only write its own environment.

``ingest`` then runs inside the caller's database transaction:

3. flag suspected clock skew (``sent_at`` vs the server's ``received_at``; never rewritten);
4. apply the project's privacy policy (M4: NUL replacement, capture ceiling, secret scrubbing,
   content size limit);
5. merge and store (``storage.traces``), recording per-event outcomes;
6. record non-zero counters in ``ingest_diagnostics``.

The project always comes from the API key, never from the payload: the wire format has no project
field, and an unknown envelope field is rejected (or, for a newer schema minor, kept only as data).
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import Connection

from ..auth import Principal
from ..config import Settings
from ..errors import Forbidden, InvalidBatch, PayloadTooLarge, Unauthorized, UnsupportedSchemaVersion
from ..model.parsing import StrictJSONError, loads_strict
from ..model.versioning import BatchResult, FieldError, validate_batch
from ..storage import tenancy
from ..storage.traces import record_diagnostics, store_events
from .prepare import clock_skew_suspected, prepare_events, prepare_resource
from .scrub import scrub_secrets

logger = logging.getLogger("aobs.ingest")

MAX_MESSAGE_CHARS = 300
MAX_ERRORS_PER_EVENT = 10
MAX_BATCH_ERRORS = 20


def safe_text(text: str) -> str:
    """Client-supplied fragments (unknown keys, bad values) may echo into error messages: scrub
    secrets and cut them short so a response never carries a credential or a large blob back."""
    scrubbed, _ = scrub_secrets(text)
    return scrubbed if len(scrubbed) <= MAX_MESSAGE_CHARS else scrubbed[: MAX_MESSAGE_CHARS - 1] + "…"


def _error(error: FieldError) -> dict[str, str]:
    return {"path": safe_text(error.path), "type": error.type, "message": safe_text(error.message)}


def parse_body(body: bytes, max_events: int) -> Any:
    """Strict JSON, then the event-count limit (before the envelope or any event is validated)."""
    try:
        data = loads_strict(body)
    except StrictJSONError as exc:
        error = FieldError("", "invalid_json", str(exc))
        raise InvalidBatch("The request body is not valid JSON", extensions={"errors": [_error(error)]}) from None
    events = data.get("events") if isinstance(data, dict) else None
    if isinstance(events, list) and len(events) > max_events:
        raise PayloadTooLarge(
            f"The batch has {len(events)} events; at most {max_events} are accepted per request",
            extensions={"limit": "events_per_batch", "max": max_events},
        )
    return data


def validate(data: Any, principal: Principal) -> BatchResult:
    """Steps 1-2, without the database: the envelope and every event, then the key's environment."""
    if not isinstance(data, Mapping):  # never hand a JSON string back to the parser
        error = FieldError("", "dict_type", "batch must be a JSON object")
        raise InvalidBatch(extensions={"errors": [_error(error)]})
    result = validate_batch(data)
    if result.envelope is None:
        errors = [_error(e) for e in result.batch_errors[:MAX_BATCH_ERRORS]]
        if any(e.type == "unsupported_schema_version" for e in result.batch_errors):
            raise UnsupportedSchemaVersion(errors[0]["message"], extensions={"errors": errors})
        raise InvalidBatch(extensions={"errors": errors})
    environment = result.envelope.resource.environment
    if principal.environment is not None and environment != principal.environment:
        raise Forbidden(f"this API key may only write the '{principal.environment}' environment")
    return result


def ingest(
    conn: Connection,
    principal: Principal,
    data: Any,
    received_at: datetime,
    settings: Settings,
) -> dict[str, Any]:
    """Store one batch (raw parsed JSON, or the result of :func:`validate`) inside the caller's
    transaction, after the project's privacy policy. Returns the response body."""
    result = data if isinstance(data, BatchResult) else validate(data, principal)
    envelope = result.envelope
    assert envelope is not None  # validate() raises otherwise
    environment = envelope.resource.environment
    project = tenancy.project_settings(conn, principal.project_id)
    if project is None:  # the project was deleted after the key was checked
        raise Unauthorized()

    counters: Counter[str] = Counter()
    skew = clock_skew_suspected(envelope.sent_at, received_at, settings.clock_skew_threshold_s)
    counters["clock_skew_suspected"] += int(skew)
    counters["newer_schema_minor"] += int(result.lenient)
    counters["unknown_fields_over_limit"] += len(result.extra_dropped)
    prepared = prepare_events(result.accepted, project)
    counters.update(prepared.counters)
    resource = prepare_resource(
        envelope.resource.model_dump(mode="json", exclude_unset=True), dict(result.envelope_extra), project, counters
    )
    stored = store_events(
        conn,
        principal.project_id,
        prepared.accepted,
        resource=resource,
        environment=environment,
        settings=project,
        now=received_at,
    )
    counters.update(stored.counters)

    rejected: list[dict[str, Any]] = [
        {"index": r.index, "errors": [_error(e) for e in r.errors[:MAX_ERRORS_PER_EVENT]]}
        for r in (*result.rejected, *prepared.rejected)
    ]
    dropped: list[dict[str, Any]] = []
    tally: Counter[str] = Counter()
    conflicts = 0
    for index, outcome in sorted(stored.outcomes.items()):
        tally[outcome.outcome] += 1
        conflicts += int(outcome.conflict)
        if outcome.outcome == "rejected":
            message = "this event_id is already stored for another trace or span"
            rejected.append(
                {"index": index, "errors": [{"path": f"events/{index}/event_id", "type": str(outcome.reason),
                                             "message": message}]}
            )  # fmt: skip
        elif outcome.outcome == "dropped":
            dropped.append({"index": index, "reason": outcome.reason})
    rejected.sort(key=lambda r: int(r["index"]))
    counters["rejected"] += len(result.rejected) + len(prepared.rejected)
    counters["duplicates"] += tally["duplicate"]
    counters["conflicts"] += conflicts
    record_diagnostics(conn, principal.project_id, principal.key_id, envelope.batch_id, counters, received_at)

    counts = {
        "received": len(envelope.events),
        "accepted": tally["accepted"],
        "duplicate": tally["duplicate"],
        "dropped": len(dropped),
        "rejected": len(rejected),
    }
    logger.info(
        "ingest project=%s key=%s batch=%s received=%d accepted=%d duplicate=%d dropped=%d rejected=%d traces=%d",
        principal.project_id, principal.key_id, envelope.batch_id, counts["received"], counts["accepted"],
        counts["duplicate"], counts["dropped"], counts["rejected"], len(stored.trace_ids),
    )  # fmt: skip
    notices = {k: v for k, v in sorted(counters.items()) if v and k not in ("rejected", "duplicates", "conflicts")}
    return {
        "batch_id": envelope.batch_id,
        "received_at": received_at.isoformat().replace("+00:00", "Z"),
        "counts": counts,
        "conflicts": conflicts,
        "rejected": rejected,
        "dropped": dropped,
        "trace_ids": stored.trace_ids,
        "clock_skew_suspected": skew,
        "notices": notices,
    }
