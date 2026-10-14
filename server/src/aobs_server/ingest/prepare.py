"""Pure per-event ingest steps, applied after schema validation and before merging.

For each accepted event (as its canonical JSON):

1. **NUL characters** in strings and object keys are replaced with U+FFFD. JSON allows
   ``\\u0000`` but PostgreSQL text and JSONB cannot store it.
2. **Privacy ceiling** (project settings, architecture §9): content above the ceiling becomes
   ``{"status": "not_captured"}`` (with a ``redacted`` ceiling, ``captured`` content is dropped:
   the server cannot verify it was redacted); error messages and stack traces are removed unless
   the project allows them; fields from newer schema minors (``extra``) are kept only under a
   ``full`` ceiling, because the server cannot know whether they carry content.
3. **Secret scrubbing** of error messages, stack traces, string attributes and ``redacted``
   content (``captured`` content under a ``full`` ceiling is stored as sent, as in the SDK);
   error messages and stack traces also lose email addresses and card numbers, under every
   ceiling. Scrubbed strings are cut back to their schema length limits.
4. **Content size limit**: a content part whose value (after scrubbing) is larger than 64 KiB is
   replaced with ``{"status": "not_captured"}``. Size is measured exactly as the SDK caps it
   (UTF-8 bytes of a string; compact JSON with ``ensure_ascii=False`` otherwise), so SDK output
   always fits.

The result is validated again against the wire model, so what is stored is always schema-valid;
its digest (used for conflict resolution) is computed on this post-policy form, so a stored
digest never encodes content the policy removed.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter, ValidationError

from ..model.versioning import AcceptedEvent, FieldError, RejectedEvent
from ..model.wire import Event, SpanEnd, SpanEvent, SpanStart
from ..project_settings import CAPTURE_LEVELS, ProjectSettings
from .scrub import scrub_error_text, scrub_secrets, scrub_to_fit

MAX_CONTENT_PART_BYTES = 65_536
MAX_ATTRIBUTE_CHARS = 4_096
MAX_ERROR_MESSAGE_CHARS = 8_192
MAX_STACK_CHARS = 32_768
MAX_REDACTION_COUNT = 1_000_000
REPLACEMENT = "\ufffd"

_EVENT: TypeAdapter[SpanStart | SpanEnd | SpanEvent] = TypeAdapter(Event)


@dataclass(frozen=True)
class Prepared:
    accepted: tuple[AcceptedEvent, ...]
    rejected: tuple[RejectedEvent, ...]
    counters: Counter[str]


def content_part_size(value: Any) -> int:
    """Bytes of a content value, measured the way the SDK's ``max_content_bytes`` cap measures."""
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def replace_nul(value: Any, counters: Counter[str]) -> Any:
    if isinstance(value, str):
        if "\x00" in value:
            counters["nul_characters_replaced"] += value.count("\x00")
            return value.replace("\x00", REPLACEMENT)
        return value
    if isinstance(value, dict):
        return {replace_nul(k, counters): replace_nul(v, counters) for k, v in value.items()}
    if isinstance(value, list):
        return [replace_nul(v, counters) for v in value]
    return value


def _scrub_tree(value: Any) -> tuple[Any, int]:
    if isinstance(value, str):
        return scrub_secrets(value)
    if isinstance(value, dict):
        total = 0
        out: dict[str, Any] = {}
        for key, item in value.items():
            out[key], n = _scrub_tree(item)
            total += n
        return out, total
    if isinstance(value, list):
        total = 0
        items = []
        for item in value:
            new, n = _scrub_tree(item)
            items.append(new)
            total += n
        return items, total
    return value, 0


def _scrub_attributes(attributes: dict[str, Any], counters: Counter[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in attributes.items():
        if isinstance(value, str):
            value, n = scrub_to_fit(value, MAX_ATTRIBUTE_CHARS)
            counters["secrets_scrubbed"] += n
        elif isinstance(value, list):
            items = []
            for item in value:
                if isinstance(item, str):
                    item, n = scrub_to_fit(item, MAX_ATTRIBUTE_CHARS)
                    counters["secrets_scrubbed"] += n
                items.append(item)
            value = items
        out[key] = value
    return out


def _apply_content(content: dict[str, Any], settings: ProjectSettings, counters: Counter[str]) -> dict[str, Any]:
    ceiling = CAPTURE_LEVELS[settings.capture_ceiling]
    out: dict[str, Any] = {}
    for direction, part in content.items():
        status = part.get("status")
        if status in ("captured", "redacted"):
            level = CAPTURE_LEVELS["full" if status == "captured" else "redacted"]
            if level > ceiling:
                counters["content_dropped_policy"] += 1
                part = {"status": "not_captured"}
            else:
                if status == "redacted":
                    value, n = _scrub_tree(part.get("value"))
                    if n:
                        counters["secrets_scrubbed"] += n
                        count = min(part.get("redaction_count", 0) + n, MAX_REDACTION_COUNT)
                        part = {**part, "value": value, "redaction_count": count}
                if content_part_size(part.get("value")) > MAX_CONTENT_PART_BYTES:
                    counters["content_dropped_oversize"] += 1
                    part = {"status": "not_captured"}
        out[direction] = part
    return out


def _apply_error(error: dict[str, Any], settings: ProjectSettings, counters: Counter[str]) -> dict[str, Any]:
    error = dict(error)
    if "message" in error:
        if settings.error_messages:
            error["message"], n, personal = scrub_error_text(error["message"], MAX_ERROR_MESSAGE_CHARS)
            counters["secrets_scrubbed"] += n
            counters["personal_data_redacted"] += personal
        else:
            del error["message"]
            counters["error_messages_dropped_policy"] += 1
    if "stack" in error:
        if settings.stack_traces:
            error["stack"], n, personal = scrub_error_text(error["stack"], MAX_STACK_CHARS)
            counters["secrets_scrubbed"] += n
            counters["personal_data_redacted"] += personal
        else:
            del error["stack"]
            counters["stack_traces_dropped_policy"] += 1
    return error


def apply_policy(body: dict[str, Any], settings: ProjectSettings, counters: Counter[str]) -> dict[str, Any]:
    """Steps 1-4 on one event's canonical JSON (``__extra__`` holds newer-minor fields)."""
    body = replace_nul(body, counters)
    if "__extra__" in body and settings.capture_ceiling != "full":
        counters["unknown_fields_dropped_policy"] += 1
        del body["__extra__"]
    if "content" in body:
        body["content"] = _apply_content(body["content"], settings, counters)
    if "error" in body:
        body["error"] = _apply_error(body["error"], settings, counters)
    if "attributes" in body:
        body["attributes"] = _scrub_attributes(body["attributes"], counters)
    return body


def prepare_event(accepted: AcceptedEvent, settings: ProjectSettings, counters: Counter[str]) -> AcceptedEvent:
    """Return the event as it will be stored. Raises ValidationError only on an internal bug."""
    body = apply_policy(accepted.canonical(), settings, counters)
    extra = body.pop("__extra__", {})
    event = _EVENT.validate_python(body)
    return AcceptedEvent(index=accepted.index, event=event, extra=extra)


def prepare_events(events: tuple[AcceptedEvent, ...], settings: ProjectSettings) -> Prepared:
    counters: Counter[str] = Counter()
    accepted: list[AcceptedEvent] = []
    rejected: list[RejectedEvent] = []
    for item in events:
        try:
            accepted.append(prepare_event(item, settings, counters))
        except ValidationError as exc:  # pragma: no cover - the steps above keep events valid
            counters["internal_policy_errors"] += 1
            rejected.append(
                RejectedEvent(
                    item.index,
                    tuple(
                        FieldError(f"events/{item.index}", "policy_revalidation", e["msg"])
                        for e in exc.errors(include_url=False)
                    ),
                )
            )
    return Prepared(tuple(accepted), tuple(rejected), counters)


def prepare_resource(
    resource: dict[str, Any], envelope_extra: dict[str, Any], settings: ProjectSettings, counters: Counter[str]
) -> dict[str, Any]:
    """The resource as stored: NUL-free; newer-minor envelope fields only under a full ceiling."""
    stored: dict[str, Any] = replace_nul(resource, counters)
    if envelope_extra:
        if settings.capture_ceiling == "full":
            stored["__extra__"] = replace_nul(envelope_extra, counters)
        else:
            counters["unknown_fields_dropped_policy"] += 1
    return stored


def clock_skew_suspected(sent_at: datetime, received_at: datetime, threshold_s: float) -> bool:
    """True if the client's send time and the server's receive time differ by more than the
    threshold (either direction; network delay is included). Timestamps are never rewritten."""
    return abs((received_at - sent_at).total_seconds()) > threshold_s
