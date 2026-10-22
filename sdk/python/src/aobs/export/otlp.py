"""OTLP/HTTP JSON exporter: sends aobs spans to any OpenTelemetry backend (an OTel Collector,
Jaeger, Grafana Tempo, Honeycomb, ...) using only the standard library.

It implements :class:`~aobs.export.base.Exporter`: it decodes each wire-schema v1.0 batch and
converts it to an ``ExportTraceServiceRequest`` in the OTLP/JSON encoding (``POST /v1/traces``).
Only what the batch contains is converted, so the SDK's capture policy and redaction apply
unchanged; ``include_content=False`` additionally keeps captured content out of OTLP.

Span lifecycle. OTLP has no partial spans: a span is exported once, complete. In the aobs wire
model a ``span_end`` is self-contained (it repeats every ``span_start`` field), while a
``span_start`` only exists so collectors can show still-running work. So:

* ``span_end`` -> one OTLP span. It needs nothing from an earlier batch.
* ``span_start`` -> skipped (counted as ``starts_skipped``). Spans that never end (process
  killed, still open at shutdown) therefore never reach the OTLP backend.
* ``span_event`` -> held until its span ends, then attached as an OTLP span event. Usually the
  event arrives in an earlier batch than its span's end. At most ``max_pending_events`` are held
  (oldest spans' events evicted first, ``events_dropped_buffer_full``); events still held at
  shutdown are dropped (``events_dropped_unended``); an event recorded after its span was
  exported cannot be attached (``events_dropped_late``).

Status handling matches :class:`~aobs.export.http.HttpExporter`: 2xx success (OTLP
``partialSuccess.rejectedSpans`` is reported as ``rejected_events``), 408/429/5xx and network
errors retry (``Retry-After`` seconds honoured), other 4xx are permanent failures. Redirects are
not followed (they could forward authentication headers). A retried batch is re-sent byte for
byte; it is converted only once.
"""

from __future__ import annotations

import calendar
import gzip
import json
import math
import os
import re
import threading
import urllib.error
import urllib.request
from collections import Counter, OrderedDict
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from .._diagnostics import logger
from .._version import __version__
from .base import SUCCESS, EncodedBatch, ExportResult
from .http import MAX_RESPONSE_BYTES, RETRYABLE_STATUS, _NoRedirect, _retry_after

DEFAULT_ENDPOINT = "http://localhost:4318/v1/traces"
SCOPE_NAME = "aobs"

# OTLP enums are encoded as integers in OTLP/JSON.
SPAN_KIND_INTERNAL = 1
SPAN_KIND_CLIENT = 3
STATUS_CODE_UNSET = 0
STATUS_CODE_ERROR = 2

# aobs span kinds that are calls to a remote model service (OTel GenAI: inference and embeddings
# spans are CLIENT). Everything else, including tools and in-process agents, is INTERNAL.
CLIENT_KINDS = frozenset({"llm", "embedding"})

COUNTERS = (
    "spans_exported",
    "spans_failed",
    "spans_invalid",
    "starts_skipped",
    "events_attached",
    "events_dropped_late",
    "events_dropped_buffer_full",
    "events_dropped_unended",
    "events_invalid",
    "requests",
)

_TRACE_RE = re.compile(r"^(?!0{32}\Z)[0-9a-f]{32}\Z")
_SPAN_RE = re.compile(r"^(?!0{16}\Z)[0-9a-f]{16}\Z")
_TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?Z\Z")
_INT64 = (-(2**63), 2**63 - 1)
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

SpanKey = tuple[str, str]
Json = dict[str, Any]


class OtlpHttpExporter:
    """Export aobs batches as OTLP/HTTP JSON.

    ``endpoint`` is the full traces URL. When omitted it is read from
    ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``, then ``OTEL_EXPORTER_OTLP_ENDPOINT`` + ``/v1/traces``,
    then defaults to ``http://localhost:4318/v1/traces``. ``headers`` (e.g. a vendor API key)
    default to ``OTEL_EXPORTER_OTLP_TRACES_HEADERS`` / ``OTEL_EXPORTER_OTLP_HEADERS``
    (``k1=v1,k2=v2``). ``resource_attributes`` are added to (and override) the attributes derived
    from the batch resource.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        headers: Mapping[str, str] | None = None,
        *,
        compression: Literal["gzip"] | None = None,
        include_content: bool = True,
        resource_attributes: Mapping[str, Any] | None = None,
        max_pending_events: int = 4096,
        max_tracked_spans: int = 16384,
    ) -> None:
        self.url = endpoint or _env_endpoint()
        parts = urlsplit(self.url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"OTLP endpoint {self.url!r} is not an http(s) URL")
        if compression not in (None, "gzip"):
            raise ValueError(f"unsupported compression {compression!r}; use 'gzip' or None")
        extra_headers = dict(headers) if headers is not None else _env_headers()
        if extra_headers and parts.scheme == "http" and parts.hostname not in _LOCAL_HOSTS:
            logger.warning("aobs OTLP exporter: headers will be sent over plaintext HTTP to a non-local host")
        self._headers = {
            "Content-Type": "application/json",
            "User-Agent": f"aobs-python/{__version__}",
            **extra_headers,
        }
        self._compression = compression
        if compression == "gzip":
            self._headers["Content-Encoding"] = "gzip"
        self._include_content = include_content
        self._extra_resource = _attributes(dict(resource_attributes or {}))
        self._max_pending = max(0, int(max_pending_events))
        self._max_tracked = max(0, int(max_tracked_spans))
        # Do not follow redirects: a redirect could forward authentication headers elsewhere.
        self._opener = urllib.request.build_opener(_NoRedirect)
        self._init_state()

    def _init_state(self) -> None:
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()
        # span_events waiting for their span to end, keyed by span; insertion order = age.
        self._pending: OrderedDict[SpanKey, list[Json]] = OrderedDict()
        self._pending_count = 0
        # Spans already converted, so later events for them are counted as late, not buffered.
        self._ended: OrderedDict[SpanKey, None] = OrderedDict()
        # The converted request of the batch being sent: (batch_id, body, span count).
        self._inflight: tuple[str, bytes, int] | None = None

    def _after_fork_in_child(self) -> None:
        self._init_state()  # what was held belongs to the parent process

    def __repr__(self) -> str:
        return f"OtlpHttpExporter(url={self.url!r})"

    def diagnostics(self) -> dict[str, int]:
        """Counters (see ``COUNTERS``) plus ``pending_events`` currently held."""
        with self._lock:
            snapshot = {name: self._counts[name] for name in COUNTERS}
            snapshot["pending_events"] = self._pending_count
        return snapshot

    # ------------------------------------------------------------------ Exporter protocol

    def export(self, batch: EncodedBatch, timeout_s: float) -> ExportResult:
        with self._lock:
            if self._inflight is None or self._inflight[0] != batch.batch_id:
                try:
                    payload = json.loads(batch.body)
                except ValueError:
                    return ExportResult("failure", message="batch is not valid JSON")
                if not isinstance(payload, dict):
                    return ExportResult("failure", message="batch is not a JSON object")
                try:
                    body, spans = self._convert_locked(payload)
                except Exception as exc:  # a retry would convert (and buffer) the same events again
                    return ExportResult("failure", message=f"OTLP conversion failed: {type(exc).__name__}")
                self._inflight = (batch.batch_id, body, spans)
            _, body, spans = self._inflight
            if not spans:
                self._inflight = None
                return SUCCESS
        result = self._post(body, timeout_s)
        with self._lock:
            if result.outcome != "retry":
                self._inflight = None
                self._counts["spans_exported" if result.outcome == "success" else "spans_failed"] += spans
        return result

    def shutdown(self) -> None:
        with self._lock:
            unended = self._pending_count
            self._counts["events_dropped_unended"] += unended
            self._pending.clear()
            self._pending_count = 0
        if unended:
            logger.warning("aobs OTLP exporter: dropped %d span events whose span never ended", unended)

    # ------------------------------------------------------------------ conversion

    def _convert_locked(self, payload: Json) -> tuple[bytes, int]:
        events = payload.get("events")
        events = [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []
        for event in events:  # first, so events whose span ends in this same batch are attached
            if event.get("type") == "span_event":
                self._hold_event_locked(event)
        spans: list[Json] = []
        for event in events:
            kind = event.get("type")
            if kind == "span_start":
                self._counts["starts_skipped"] += 1
            elif kind == "span_end":
                span = self._span_locked(event)
                if span is not None:
                    spans.append(span)
        if not spans:
            return b"", 0
        resource = payload.get("resource")
        resource_attrs = _merge(
            _resource_attributes(resource if isinstance(resource, dict) else {}) + self._extra_resource
        )
        request = {
            "resourceSpans": [
                {
                    "resource": {"attributes": resource_attrs},
                    "scopeSpans": [{"scope": {"name": SCOPE_NAME, "version": __version__}, "spans": spans}],
                }
            ]
        }
        body = json.dumps(request, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if self._compression == "gzip":
            body = gzip.compress(body)
        return body, len(spans)

    def _hold_event_locked(self, event: Json) -> None:
        key = _key(event)
        if key is None:
            self._counts["events_invalid"] += 1
            return
        if key in self._ended:
            self._counts["events_dropped_late"] += 1
            return
        try:
            converted: Json = {"timeUnixNano": str(_unix_nanos(event["time"])), "name": str(event["name"])}
        except (KeyError, TypeError, ValueError):
            self._counts["events_invalid"] += 1
            return
        attrs = _attributes(event.get("attributes"))
        if attrs:
            converted["attributes"] = attrs
        if self._max_pending == 0:
            self._counts["events_dropped_buffer_full"] += 1
            return
        while self._pending_count >= self._max_pending:
            _, evicted = self._pending.popitem(last=False)
            self._pending_count -= len(evicted)
            self._counts["events_dropped_buffer_full"] += len(evicted)
        self._pending.setdefault(key, []).append(converted)
        self._pending_count += 1

    def _span_locked(self, event: Json) -> Json | None:
        key = _key(event)
        parent = event.get("parent_span_id")
        try:
            if key is None or (parent is not None and not _SPAN_RE.match(str(parent))):
                raise ValueError("invalid id")
            start = _unix_nanos(event["start_time"])
            end = max(_unix_nanos(event["end_time"]), start)
            span = _convert_span(event, include_content=self._include_content)
        except (KeyError, TypeError, ValueError):
            self._counts["spans_invalid"] += 1
            return None
        trace_id, span_id = key
        out: Json = {"traceId": trace_id, "spanId": span_id}
        if parent is not None:
            out["parentSpanId"] = parent
        out.update(span)
        out["startTimeUnixNano"] = str(start)
        out["endTimeUnixNano"] = str(end)
        held = self._pending.pop(key, [])
        self._pending_count -= len(held)
        span_events = held + out.pop("events", [])
        for item in span_events:
            if "timeUnixNano" not in item:  # the error's exception event happens at the end
                item["timeUnixNano"] = str(end)
        if span_events:
            out["events"] = span_events
            self._counts["events_attached"] += len(held)
        if self._max_tracked:
            self._ended[key] = None
            while len(self._ended) > self._max_tracked:
                self._ended.popitem(last=False)
        return out

    # ------------------------------------------------------------------ transport

    def _post(self, body: bytes, timeout_s: float) -> ExportResult:
        request = urllib.request.Request(self.url, data=body, headers=self._headers, method="POST")
        with self._lock:
            self._counts["requests"] += 1
        try:
            with self._opener.open(request, timeout=timeout_s) as response:
                return ExportResult("success", rejected_events=_rejected_spans(response.read(MAX_RESPONSE_BYTES)))
        except urllib.error.HTTPError as exc:
            status = exc.code
            retry_after = _retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            exc.close()
            if status in RETRYABLE_STATUS:
                return ExportResult("retry", retry_after_s=retry_after, message=f"OTLP HTTP {status}")
            return ExportResult("failure", message=f"OTLP HTTP {status}")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            return ExportResult("retry", message=f"OTLP {type(exc).__name__}: {reason}"[:200])


# ---------------------------------------------------------------------------------- mapping


def _convert_span(event: Json, *, include_content: bool) -> Json:
    """Name, kind, attributes, status, links and the error's exception event of one span_end."""
    kind = str(event["kind"])
    attrs: Json = dict(event.get("attributes") or {})  # structured fields below win on collision
    attrs["aobs.span.kind"] = kind
    status = event.get("status")
    if isinstance(status, str):
        attrs["aobs.status"] = status
    _put(attrs, "gen_ai.conversation.id", event.get("session_id"))
    _put(attrs, "aobs.input_fingerprint", event.get("input_fingerprint"))

    agent = _block(event, "agent")
    for field in ("name", "id", "version"):
        _put(attrs, f"gen_ai.agent.{field}", agent.get(field))

    tool = _block(event, "tool")
    _put(attrs, "gen_ai.tool.name", tool.get("name"))
    _put(attrs, "gen_ai.tool.call.id", tool.get("call_id"))
    _put(attrs, "aobs.tool.type", tool.get("type"))

    llm = _block(event, "llm")
    _put(attrs, "gen_ai.provider.name", llm.get("provider"))
    _put(attrs, "gen_ai.system", llm.get("provider"))  # deprecated name, still read by many backends
    _put(attrs, "gen_ai.request.model", llm.get("request_model"))
    _put(attrs, "gen_ai.response.model", llm.get("response_model"))
    _put(attrs, "aobs.llm.provider_request_id", llm.get("provider_request_id"))
    request = _block(llm, "request")
    for field in ("temperature", "top_p", "max_tokens"):
        _put(attrs, f"gen_ai.request.{field}", request.get(field))
    _put(attrs, "aobs.llm.request.stream", request.get("stream"))
    response = _block(llm, "response")
    _put(attrs, "gen_ai.response.id", response.get("id"))
    _put(attrs, "gen_ai.response.finish_reasons", response.get("finish_reasons"))
    streaming = _block(llm, "streaming")
    for field in ("time_to_first_token_ms", "chunk_count", "completed"):
        _put(attrs, f"aobs.llm.streaming.{field}", streaming.get(field))
    _put(attrs, "gen_ai.operation.name", _operation(kind, llm))

    usage = _block(event, "usage")
    _put(attrs, "gen_ai.usage.input_tokens", usage.get("input_tokens"))
    _put(attrs, "gen_ai.usage.output_tokens", usage.get("output_tokens"))
    _put(attrs, "gen_ai.usage.cache_read.input_tokens", usage.get("cache_read_input_tokens"))
    _put(attrs, "gen_ai.usage.cache_creation.input_tokens", usage.get("cache_write_input_tokens"))
    for field in ("total_tokens", "reasoning_tokens", "source"):
        _put(attrs, f"aobs.usage.{field}", usage.get(field))

    cost = _block(event, "cost")
    for field in ("amount", "currency", "source"):
        _put(attrs, f"aobs.cost.{field}", cost.get(field))

    retry = _block(event, "retry")
    _put(attrs, "aobs.retry.attempt", retry.get("attempt"))
    _put(attrs, "aobs.retry.max_attempts", retry.get("max_attempts"))

    content = _block(event, "content")
    for direction in ("input", "output"):
        block = content.get(direction)
        if isinstance(block, dict):
            _content(attrs, kind, direction, block, include_content)

    out: Json = {"name": str(event["name"]), "kind": SPAN_KIND_CLIENT if kind in CLIENT_KINDS else SPAN_KIND_INTERNAL}
    error = _block(event, "error")
    span_status: Json = {"code": STATUS_CODE_UNSET}
    if status == "error":
        span_status = {"code": STATUS_CODE_ERROR}
        if isinstance(error.get("message"), str) and error["message"]:
            span_status["message"] = error["message"]
        _put(attrs, "error.type", error.get("type") or "_OTHER")
        for field in ("code", "origin", "retryable"):
            _put(attrs, f"aobs.error.{field}", error.get(field))
        exception: Json = {}
        _put(exception, "exception.type", error.get("type"))
        _put(exception, "exception.message", error.get("message"))
        _put(exception, "exception.stacktrace", error.get("stack"))
        if exception:
            out["events"] = [{"name": "exception", "attributes": _attributes(exception)}]
    out["attributes"] = _attributes(attrs)
    links = [_link(link) for link in event.get("links") or () if isinstance(link, dict)]
    if any(link is not None for link in links):
        out["links"] = [link for link in links if link is not None]
    out["status"] = span_status
    return out


def _operation(kind: str, llm: Json) -> str | None:
    if kind == "agent":
        return "invoke_agent"
    if kind == "tool":
        return "execute_tool"
    operation = llm.get("operation")
    if isinstance(operation, str) and operation:
        return "text_completion" if operation == "completion" else operation
    return "embeddings" if kind == "embedding" else None


def _content(attrs: Json, kind: str, direction: str, block: Json, include: bool) -> None:
    status = block.get("status")
    if not isinstance(status, str):
        return
    has_value = status in ("captured", "redacted") and "value" in block
    if has_value and not include:
        attrs[f"aobs.content.{direction}.status"] = "not_exported"
        return
    attrs[f"aobs.content.{direction}.status"] = status
    _put(attrs, f"aobs.content.{direction}.format", block.get("format"))
    _put(attrs, f"aobs.content.{direction}.truncated", block.get("truncated"))
    _put(attrs, f"aobs.content.{direction}.redaction_count", block.get("redaction_count"))
    if not has_value:
        return
    value = block["value"]
    text = value if isinstance(value, str) and block.get("format") == "text" else _json(value)
    if kind == "tool":
        key = "gen_ai.tool.call.arguments" if direction == "input" else "gen_ai.tool.call.result"
    elif block.get("format") == "messages":
        key = f"gen_ai.{direction}.messages"
    else:
        key = f"aobs.content.{direction}.value"
    attrs[key] = text


def _link(link: Json) -> Json | None:
    trace_id, span_id = link.get("trace_id"), link.get("span_id")
    if not (isinstance(trace_id, str) and _TRACE_RE.match(trace_id)):
        return None
    if not (isinstance(span_id, str) and _SPAN_RE.match(span_id)):
        return None
    out: Json = {"traceId": trace_id, "spanId": span_id}
    if isinstance(link.get("relation"), str):
        out["attributes"] = _attributes({"aobs.link.relation": link["relation"]})
    return out


def _resource_attributes(resource: Json) -> list[Json]:
    attrs: Json = {}
    _put(attrs, "service.name", resource.get("service_name"))
    _put(attrs, "service.version", resource.get("service_version"))
    _put(attrs, "deployment.environment.name", resource.get("environment"))
    sdk = _block(resource, "sdk")
    _put(attrs, "telemetry.sdk.name", sdk.get("name"))
    _put(attrs, "telemetry.sdk.version", sdk.get("version"))
    _put(attrs, "telemetry.sdk.language", sdk.get("language"))
    _put(attrs, "aobs.sdk.runtime", sdk.get("runtime"))
    capture = _block(resource, "capture")
    for field in ("mode", "error_messages", "stack_traces"):
        _put(attrs, f"aobs.capture.{field}", capture.get(field))
    integrations = resource.get("integrations")
    if isinstance(integrations, list):
        names = [
            f"{item.get('name')}/{item.get('version')}"
            for item in integrations
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
        _put(attrs, "aobs.integrations", names or None)
    return _attributes(attrs)


# ---------------------------------------------------------------------------------- helpers


def _key(event: Json) -> SpanKey | None:
    trace_id, span_id = event.get("trace_id"), event.get("span_id")
    if isinstance(trace_id, str) and isinstance(span_id, str) and _TRACE_RE.match(trace_id) and _SPAN_RE.match(span_id):
        return trace_id, span_id
    return None


def _block(container: Json, name: str) -> Json:
    value = container.get(name)
    return value if isinstance(value, dict) else {}


def _put(attrs: Json, key: str, value: Any) -> None:
    if value is not None and value != "" and value != []:
        attrs[key] = value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _unix_nanos(ts: Any) -> int:
    """RFC 3339 UTC (``...Z``, up to 9 fractional digits) -> integer nanoseconds since the epoch,
    without going through a float."""
    match = _TS_RE.match(ts) if isinstance(ts, str) else None
    if match is None:
        raise ValueError(f"invalid timestamp {ts!r}")
    year, month, day, hour, minute, second = (int(g) for g in match.groups()[:6])
    moment = datetime(year, month, day, hour, minute, second, tzinfo=UTC)  # validates ranges
    fraction = (match.group(7) or "").ljust(9, "0")
    return calendar.timegm(moment.utctimetuple()) * 1_000_000_000 + int(fraction)


def _any_value(value: Any) -> Json | None:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        if _INT64[0] <= value <= _INT64[1]:
            return {"intValue": str(value)}  # int64 is a JSON string in OTLP/JSON
        return {"stringValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value} if math.isfinite(value) else None
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, (list, tuple)):
        values = [v for v in (_any_value(item) for item in value) if v is not None]
        return {"arrayValue": {"values": values}}
    if isinstance(value, dict):
        return {"stringValue": _json(value)}
    return None


def _attributes(values: Any) -> list[Json]:
    if not isinstance(values, dict):
        return []
    out: list[Json] = []
    for key, value in values.items():
        converted = _any_value(value)
        if isinstance(key, str) and key and converted is not None:
            out.append({"key": key, "value": converted})
    return out


def _merge(attrs: list[Json]) -> list[Json]:
    """Unique keys, later entries win (OTLP requires unique attribute keys)."""
    merged: dict[str, Json] = {}
    for attr in attrs:
        merged[attr["key"]] = attr
    return list(merged.values())


def _rejected_spans(body: bytes) -> int:
    if not body:
        return 0
    try:
        data = json.loads(body)
    except ValueError:
        return 0
    partial = data.get("partialSuccess") if isinstance(data, dict) else None
    rejected = partial.get("rejectedSpans") if isinstance(partial, dict) else None
    try:
        return max(int(rejected), 0) if rejected is not None else 0
    except (TypeError, ValueError):
        return 0


def _env_endpoint() -> str:
    traces = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    if traces:
        return traces
    base = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if base:
        return base.rstrip("/") + "/v1/traces"
    return DEFAULT_ENDPOINT


def _env_headers() -> dict[str, str]:
    raw = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS") or os.environ.get("OTEL_EXPORTER_OTLP_HEADERS") or ""
    headers: dict[str, str] = {}
    for item in raw.split(","):
        key, sep, value = item.partition("=")
        if sep and key.strip():
            headers[unquote(key.strip())] = unquote(value.strip())
    return headers
