"""Pydantic models for the v1 wire format.

These mirror ``schema/v1/ingest.schema.json`` exactly; the JSON Schema is normative. Agreement is
enforced by the conformance fixtures and by differential fuzzing in the test suite.

Conventions that make the two agree:

* Optional fields reject an explicit ``null`` (``_no_null``); absent means unknown. Only
  ``parent_span_id`` is nullable, and it is required.
* Integers follow JSON Schema semantics: ``5.0`` is an integer, ``true`` is not (``_int_like``).
* Regular expressions are the schema's ECMA patterns, compiled with ``$`` translated to ``\\Z``,
  because Python's ``$`` also matches before a trailing newline.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    model_validator,
)
from pydantic_core import PydanticCustomError

# --------------------------------------------------------------------------------------------
# Patterns — copied verbatim from the JSON Schema (ECMA-262 syntax).

TRACE_ID_PATTERN = r"^(?!0{32}$)[0-9a-f]{32}$"
SPAN_ID_PATTERN = r"^(?!0{16}$)[0-9a-f]{16}$"
UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
TIMESTAMP_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z$"
VOCAB_PATTERN = r"^[a-z][a-z0-9_-]*(\.[a-z0-9_-]+)*$"
ENVIRONMENT_PATTERN = r"^[a-z0-9][a-z0-9_.-]{0,63}$"
ATTRIBUTE_KEY_PATTERN = r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"
FINGERPRINT_PATTERN = r"^hmac-sha256:[a-z0-9_-]{1,32}:[0-9a-f]{64}$"
AMOUNT_PATTERN = r"^(0|[1-9][0-9]{0,14})(\.[0-9]{1,12})?$"
CURRENCY_PATTERN = r"^[A-Z]{3}$"
SCHEMA_VERSION_PATTERN = r"^1\.(0|[1-9][0-9]{0,3})$"

MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_DURATION_MS = 2_592_000_000  # 30 days


def ecma_to_python(pattern: str) -> re.Pattern[str]:
    """Compile an ECMA pattern whose only ``$`` characters are end anchors."""
    if r"\$" in pattern:
        raise ValueError("escaped '$' is not supported by ecma_to_python")
    return re.compile(pattern.replace("$", r"\Z"))


def _pattern(pattern: str, name: str) -> AfterValidator:
    compiled = ecma_to_python(pattern)

    def check(value: str) -> str:
        if compiled.search(value) is None:
            raise PydanticCustomError(
                "string_pattern_mismatch", "{name} does not match {pattern}", {"name": name, "pattern": pattern}
            )
        return value

    return AfterValidator(check)


def _no_null(value: Any) -> Any:
    if value is None:
        raise PydanticCustomError("null_not_allowed", "null is not allowed; omit the field if unknown")
    return value


def _int_like(value: Any) -> Any:
    # JSON Schema: 5.0 is an integer; booleans are never integers.
    if isinstance(value, float) and not isinstance(value, bool) and value.is_integer():
        return int(value)
    return value


_TIMESTAMP_RE = ecma_to_python(TIMESTAMP_PATTERN)


def _parse_timestamp(value: Any) -> Any:
    if not isinstance(value, str):
        raise PydanticCustomError("string_type", "timestamp must be a string")
    if _TIMESTAMP_RE.search(value) is None:
        raise PydanticCustomError("timestamp_format", "timestamp must be RFC 3339 UTC ending in 'Z'")
    try:
        # Python >= 3.11 accepts 'Z' and truncates fractions beyond 6 digits.
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise PydanticCustomError(
            "timestamp_invalid", "timestamp is not a real date/time: {e}", {"e": str(exc)}
        ) from exc


def _str(min_length: int, max_length: int) -> Any:
    return Field(min_length=min_length, max_length=max_length)


# --------------------------------------------------------------------------------------------
# Scalar types

TraceId = Annotated[str, _pattern(TRACE_ID_PATTERN, "trace_id")]
SpanId = Annotated[str, _pattern(SPAN_ID_PATTERN, "span_id")]
Uuid = Annotated[str, _pattern(UUID_PATTERN, "uuid")]
Vocab = Annotated[str, Field(max_length=64), _pattern(VOCAB_PATTERN, "identifier")]
Label = Annotated[str, _str(1, 256)]
ShortLabel = Annotated[str, _str(1, 128)]
Timestamp = Annotated[datetime, BeforeValidator(_parse_timestamp)]
TokenCount = Annotated[int, BeforeValidator(_int_like), Field(ge=0, le=MAX_SAFE_INTEGER)]
DurationMs = Annotated[float, Field(ge=0, le=MAX_DURATION_MS)]
Fingerprint = Annotated[str, _pattern(FINGERPRINT_PATTERN, "input_fingerprint")]

# "Optional but not null" variants. Absent -> default None (defaults are not validated);
# explicit null -> rejected by _no_null.
OptLabel = Annotated[Label | None, BeforeValidator(_no_null)]
OptShortLabel = Annotated[ShortLabel | None, BeforeValidator(_no_null)]
OptVocab = Annotated[Vocab | None, BeforeValidator(_no_null)]
OptBool = Annotated[bool | None, BeforeValidator(_no_null)]
OptFloat = Annotated[float | None, BeforeValidator(_no_null)]
OptTokenCount = Annotated[TokenCount | None, BeforeValidator(_no_null)]
OptDurationMs = Annotated[DurationMs | None, BeforeValidator(_no_null)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


# --------------------------------------------------------------------------------------------
# Attributes: free-form but bounded. Validated by one function so the rules are in one place.

_ATTR_KEY_RE = ecma_to_python(ATTRIBUTE_KEY_PATTERN)
MAX_ATTRIBUTES = 128
MAX_ATTRIBUTE_STRING = 4096
MAX_ATTRIBUTE_ARRAY = 128


def _check_scalar(value: Any, allow_array: bool) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        return
    if isinstance(value, str):
        if len(value) > MAX_ATTRIBUTE_STRING:
            raise PydanticCustomError("attribute_value", "attribute string longer than 4096 characters")
        return
    if allow_array and isinstance(value, list):
        if len(value) > MAX_ATTRIBUTE_ARRAY:
            raise PydanticCustomError("attribute_value", "attribute array longer than 128 items")
        for item in value:
            _check_scalar(item, allow_array=False)
        return
    raise PydanticCustomError(
        "attribute_value", "attribute values must be string, number, boolean or an array of those"
    )


def _check_attributes(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PydanticCustomError("dict_type", "attributes must be an object")
    if len(value) > MAX_ATTRIBUTES:
        raise PydanticCustomError("attribute_count", "more than 128 attributes")
    for key, item in value.items():
        if _ATTR_KEY_RE.search(key) is None:
            raise PydanticCustomError("attribute_key", "invalid attribute key {key!r}", {"key": key})
        _check_scalar(item, allow_array=True)
    return value


Attributes = Annotated[dict[str, Any], BeforeValidator(_no_null), BeforeValidator(_check_attributes)]
OptAttributes = Annotated[dict[str, Any] | None, BeforeValidator(_no_null), BeforeValidator(_check_attributes)]


# --------------------------------------------------------------------------------------------
# Resource (who sent the batch)


class Sdk(_Strict):
    name: Annotated[str, _str(1, 64)]
    version: Annotated[str, _str(1, 64)]
    language: Vocab
    runtime: OptShortLabel = None


class Integration(_Strict):
    name: Vocab
    version: Annotated[str, _str(1, 64)]
    capabilities: Annotated[list[Vocab], Field(max_length=64)]

    @model_validator(mode="after")
    def _unique_capabilities(self) -> Integration:
        if len(set(self.capabilities)) != len(self.capabilities):
            raise PydanticCustomError("unique_items", "capabilities must be unique")
        return self


class Capture(_Strict):
    mode: Literal["metadata_only", "redacted", "full"]
    error_messages: bool
    stack_traces: bool


class Resource(_Strict):
    service_name: ShortLabel
    service_version: OptShortLabel = None
    environment: Annotated[str, _pattern(ENVIRONMENT_PATTERN, "environment")]
    sdk: Sdk
    integrations: Annotated[list[Integration] | None, BeforeValidator(_no_null), Field(max_length=32)] = None
    capture: Capture


# --------------------------------------------------------------------------------------------
# Span blocks


class Link(_Strict):
    trace_id: TraceId
    span_id: SpanId
    relation: Vocab


class Agent(_Strict):
    name: ShortLabel
    id: OptShortLabel = None
    version: OptShortLabel = None


class LlmRequest(_Strict):
    temperature: OptFloat = None
    top_p: OptFloat = None
    max_tokens: OptTokenCount = None
    stream: OptBool = None


class LlmResponse(_Strict):
    id: OptLabel = None
    finish_reasons: Annotated[
        list[Annotated[str, _str(1, 64)]] | None, BeforeValidator(_no_null), Field(max_length=16)
    ] = None


class LlmStreaming(_Strict):
    time_to_first_token_ms: OptDurationMs = None
    chunk_count: OptTokenCount = None
    completed: OptBool = None


class Llm(_Strict):
    provider: OptVocab = None
    operation: OptVocab = None
    request_model: OptLabel = None
    response_model: OptLabel = None
    request: Annotated[LlmRequest | None, BeforeValidator(_no_null)] = None
    response: Annotated[LlmResponse | None, BeforeValidator(_no_null)] = None
    provider_request_id: OptLabel = None
    streaming: Annotated[LlmStreaming | None, BeforeValidator(_no_null)] = None


class Tool(_Strict):
    name: Label
    type: OptVocab = None
    call_id: OptLabel = None


class Retry(_Strict):
    attempt: Annotated[int, BeforeValidator(_int_like), Field(ge=1, le=10_000)]
    max_attempts: Annotated[
        int | None, BeforeValidator(_no_null), BeforeValidator(_int_like), Field(ge=1, le=10_000)
    ] = None


class Error(_Strict):
    type: Label
    message: Annotated[str | None, BeforeValidator(_no_null), Field(max_length=8192)] = None
    code: OptShortLabel = None
    origin: OptVocab = None
    retryable: OptBool = None
    stack: Annotated[str | None, BeforeValidator(_no_null), Field(max_length=32768)] = None


TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
    "reasoning_tokens",
)


class Usage(_Strict):
    """Token counts. Unknown counts are absent — never null, never zero-filled."""

    input_tokens: OptTokenCount = None
    output_tokens: OptTokenCount = None
    total_tokens: OptTokenCount = None
    cache_read_input_tokens: OptTokenCount = None
    cache_write_input_tokens: OptTokenCount = None
    reasoning_tokens: OptTokenCount = None
    source: Literal["provider_reported", "sdk_estimated"]

    @model_validator(mode="after")
    def _at_least_one_count(self) -> Usage:
        if not any(getattr(self, name) is not None for name in TOKEN_FIELDS):
            raise PydanticCustomError("usage_empty", "usage must report at least one token count")
        return self


class Cost(_Strict):
    amount: Annotated[str, _pattern(AMOUNT_PATTERN, "amount")]
    currency: Annotated[str, _pattern(CURRENCY_PATTERN, "currency")]
    source: Literal["provider_reported"]


ContentStatus = Literal["captured", "redacted", "not_captured", "unavailable"]


class ContentPart(_Strict):
    status: ContentStatus
    format: Annotated[Literal["text", "messages", "json"] | None, BeforeValidator(_no_null)] = None
    value: Any = None  # any JSON, including null; presence is tracked via model_fields_set
    truncated: OptBool = None
    redaction_count: Annotated[
        int | None, BeforeValidator(_no_null), BeforeValidator(_int_like), Field(ge=1, le=1_000_000)
    ] = None

    @property
    def has_value(self) -> bool:
        return "value" in self.model_fields_set

    @model_validator(mode="after")
    def _status_rules(self) -> ContentPart:
        carries_content = self.status in ("captured", "redacted")
        if carries_content:
            if self.format is None or not self.has_value:
                raise PydanticCustomError("content_missing", "captured/redacted content requires format and value")
        else:
            present = [n for n in ("format", "value", "truncated", "redaction_count") if n in self.model_fields_set]
            if present:
                raise PydanticCustomError(
                    "content_unexpected",
                    "{status} content must not include {fields}",
                    {"status": self.status, "fields": ", ".join(present)},
                )
        if self.status == "captured" and self.redaction_count is not None:
            raise PydanticCustomError("content_unexpected", "redaction_count is only valid for redacted content")
        return self


class Content(_Strict):
    input: Annotated[ContentPart | None, BeforeValidator(_no_null)] = None
    output: Annotated[ContentPart | None, BeforeValidator(_no_null)] = None

    @model_validator(mode="after")
    def _not_empty(self) -> Content:
        if self.input is None and self.output is None:
            raise PydanticCustomError("content_empty", "content must include input or output")
        return self


# --------------------------------------------------------------------------------------------
# Events


class _SpanFields(_Strict):
    trace_id: TraceId
    span_id: SpanId
    parent_span_id: SpanId | None  # required key; null means root
    kind: Vocab
    name: Label
    start_time: Timestamp
    links: Annotated[list[Link] | None, BeforeValidator(_no_null), Field(max_length=32)] = None
    session_id: OptLabel = None
    agent: Annotated[Agent | None, BeforeValidator(_no_null)] = None
    llm: Annotated[Llm | None, BeforeValidator(_no_null)] = None
    tool: Annotated[Tool | None, BeforeValidator(_no_null)] = None
    retry: Annotated[Retry | None, BeforeValidator(_no_null)] = None
    input_fingerprint: Annotated[Fingerprint | None, BeforeValidator(_no_null)] = None
    attributes: OptAttributes = None


class SpanStart(_SpanFields):
    type: Literal["span_start"]


class SpanEnd(_SpanFields):
    type: Literal["span_end"]
    end_time: Timestamp
    duration_ms: DurationMs
    status: Literal["ok", "error", "cancelled"]
    error: Annotated[Error | None, BeforeValidator(_no_null)] = None
    usage: Annotated[Usage | None, BeforeValidator(_no_null)] = None
    cost: Annotated[Cost | None, BeforeValidator(_no_null)] = None
    content: Annotated[Content | None, BeforeValidator(_no_null)] = None

    @model_validator(mode="after")
    def _tool_block_for_tool_spans(self) -> SpanEnd:
        if self.kind == "tool" and self.tool is None:
            raise PydanticCustomError("tool_missing", "span_end with kind 'tool' requires a tool block")
        return self


class SpanEvent(_Strict):
    type: Literal["span_event"]
    event_id: Uuid
    trace_id: TraceId
    span_id: SpanId
    name: ShortLabel
    time: Timestamp
    attributes: OptAttributes = None


Event = Annotated[SpanStart | SpanEnd | SpanEvent, Field(discriminator="type")]
EVENT_TYPES = ("span_start", "span_end", "span_event")


class Envelope(_Strict):
    """The batch envelope. ``events`` are validated one by one, separately (see versioning)."""

    schema_version: Annotated[str, _pattern(SCHEMA_VERSION_PATTERN, "schema_version")]
    batch_id: Annotated[Uuid | None, BeforeValidator(_no_null)] = None
    sent_at: Timestamp
    resource: Resource
    events: Annotated[list[Any], Field(min_length=1, max_length=1000)]
