"""Spans and the tracer.

A :class:`Span` records one operation. Used as a context manager it becomes the *current* span
(``contextvars``), so spans opened inside it — including in ``await``-ed coroutines and tasks
created inside it — become its children. Exceptions are recorded and re-raised unchanged.

Every method is safe to call in any state: on an ended span, on a non-recording span (SDK not
initialized or disabled), or with invalid values. Invalid values are dropped or normalized and
counted in diagnostics, never raised.
"""

from __future__ import annotations

import json
import math
import re
import threading
from collections.abc import Mapping, Sequence
from contextvars import ContextVar, Token
from types import TracebackType
from typing import Any, Literal

from . import _sanitize as S
from ._clock import format_ts, mono_ns, wall_ns
from ._diagnostics import Diagnostics
from ._ids import new_span_id, new_trace_id, uuid7
from ._version import SDK_NAME, __version__
from .config import Config
from .export.base import Exporter
from .export.processor import BatchProcessor
from .fingerprint import Unfingerprintable
from .privacy import ContentFormat, redact_error_message

Status = Literal["ok", "error", "cancelled"]
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
    "reasoning_tokens",
)
USAGE_SOURCES = ("provider_reported", "sdk_estimated")
_TRACE_RE = re.compile(r"^(?!0{32}\Z)[0-9a-f]{32}\Z")
_SPAN_RE = re.compile(r"^(?!0{16}\Z)[0-9a-f]{16}\Z")

_current: ContextVar[Span | None] = ContextVar("aobs_current_span", default=None)


def current_recording_span() -> Span | None:
    return _current.get()


class Tracer:
    def __init__(self, config: Config, exporter: Exporter, diag: Diagnostics) -> None:
        self.config = config
        self.diag = diag
        self.policy = config.policy
        self.fingerprinter = config.fingerprinter if config.fingerprint_inputs else None
        self._integrations: dict[str, dict[str, Any]] = {}
        self._resource_lock = threading.Lock()
        self._resource = self._encode_resource()
        self.processor = BatchProcessor(
            exporter,
            self.resource_bytes,
            diag,
            max_queue_size=config.max_queue_size,
            max_batch_events=config.max_batch_events,
            max_batch_bytes=config.max_batch_bytes,
            schedule_delay_s=config.schedule_delay_s,
            start_delay_s=config.start_delay_s,
            export_timeout_s=config.export_timeout_s,
            max_export_attempts=config.max_export_attempts,
            retry_backoff_initial_s=config.retry_backoff_initial_s,
            retry_backoff_max_s=config.retry_backoff_max_s,
        )

    # ------------------------------------------------------------------ resource

    def _encode_resource(self) -> bytes:
        import platform

        cfg = self.config
        resource: dict[str, Any] = {
            "service_name": cfg.service_name,
            "environment": cfg.environment,
            "sdk": {
                "name": SDK_NAME,
                "version": __version__,
                "language": "python",
                "runtime": S.label(
                    f"{platform.python_implementation().lower()}-{platform.python_version()}", S.MAX_SHORT_LABEL
                ),
            },
            "capture": {
                "mode": cfg.policy.mode,
                "error_messages": cfg.policy.error_messages,
                "stack_traces": cfg.policy.stack_traces,
            },
        }
        if cfg.service_version:
            resource["service_version"] = cfg.service_version
        if self._integrations:
            resource["integrations"] = [self._integrations[k] for k in sorted(self._integrations)][:32]
        return json.dumps(resource, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def resource_bytes(self) -> bytes:
        return self._resource

    def register_integration(self, name: str, version: str, capabilities: Sequence[str]) -> None:
        key = S.vocab(name)
        entry: dict[str, Any] = {
            "name": key,
            "version": S.label(version, 64, "unknown"),
            "capabilities": sorted({S.vocab(c) for c in capabilities})[:64],
        }
        with self._resource_lock:
            if self._integrations.get(key) == entry:
                return
            self._integrations[key] = entry
            self._resource = self._encode_resource()

    # ------------------------------------------------------------------ spans

    def start_span(
        self,
        name: str,
        kind: str,
        *,
        parent: Span | None,
        new_trace: bool = False,
        links: Sequence[Mapping[str, str]] | None = None,
        attributes: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        agent: Mapping[str, Any] | None = None,
        tool: Mapping[str, Any] | None = None,
        llm: Mapping[str, Any] | None = None,
    ) -> Span:
        span = Span(self, S.label(name), S.vocab(kind, "custom"))
        if new_trace or parent is None:
            span.trace_id = new_trace_id()
            span.parent_span_id = None
            if new_trace and parent is not None:
                span.add_link(parent.trace_id, parent.span_id, "triggered_by")
        else:
            span.trace_id = parent.trace_id
            span.parent_span_id = parent.span_id
        span.span_id = new_span_id()
        if session_id is not None:
            span.session_id = S.label(session_id)
        if agent:
            span._agent = {k: S.label(v, S.MAX_SHORT_LABEL) for k, v in agent.items() if v is not None}
        if span.kind == "tool":
            span._tool = {"name": span.name}
        if tool:
            span.set_tool(**tool)
        if llm:
            span.set_llm(**llm)
        for link in links or ():
            span.add_link(link.get("trace_id", ""), link.get("span_id", ""), link.get("relation", "follows_from"))
        if attributes:
            span.set_attributes(attributes)
        span._start()
        return span


class Span:
    """A recording span. Obtain one from ``aobs.span()``, ``aobs.run()``, ``aobs.tool_span()``,
    ``aobs.llm_span()`` or the decorators; don't construct it directly."""

    __slots__ = (
        "_agent",
        "_attributes",
        "_content",
        "_cost",
        "_ended",
        "_error",
        "_fingerprint",
        "_links",
        "_llm",
        "_lock",
        "_retry",
        "_start_mono",
        "_start_wall",
        "_status",
        "_token",
        "_tool",
        "_tracer",
        "_usage",
        "kind",
        "name",
        "parent_span_id",
        "session_id",
        "span_id",
        "trace_id",
    )

    is_recording = True

    def __init__(self, tracer: Tracer, name: str, kind: str) -> None:
        self._tracer = tracer
        self.name = name
        self.kind = kind
        self.trace_id = ""
        self.span_id = ""
        self.parent_span_id: str | None = None
        self.session_id: str | None = None
        self._agent: dict[str, str] | None = None
        self._tool: dict[str, Any] | None = None
        self._llm: dict[str, Any] | None = None
        self._retry: dict[str, int] | None = None
        self._links: list[dict[str, str]] | None = None
        self._attributes: dict[str, Any] | None = None
        self._fingerprint: str | None = None
        self._usage: dict[str, Any] | None = None
        self._cost: dict[str, str] | None = None
        self._content: dict[str, Any] | None = None
        self._error: dict[str, Any] | None = None
        self._status: Status | None = None
        self._ended = False
        self._token: Token[Span | None] | None = None
        self._lock = threading.Lock()
        self._start_wall = 0
        self._start_mono = 0

    def __repr__(self) -> str:
        return f"<aobs.Span {self.kind} {self.name!r} trace={self.trace_id} span={self.span_id}>"

    # ------------------------------------------------------------------ lifecycle

    def _start(self) -> None:
        self._start_wall = wall_ns()
        self._start_mono = mono_ns()
        self._tracer.diag.inc("spans_started")
        self._tracer.processor.on_start((self.trace_id, self.span_id), self._event("span_start"))

    def __enter__(self) -> Span:
        try:
            self._token = _current.set(self)
        except Exception:  # pragma: no cover - defensive
            self._tracer.diag.inc("internal_errors")
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> Literal[False]:
        try:
            if exc is not None:
                self.record_exception(exc)
            self.end()
        except Exception as internal:  # pragma: no cover - defensive; never mask the user's exception
            self._tracer.diag.error("span_exit", repr(internal)[:200], count="internal_errors")
        finally:
            if self._token is not None:
                try:
                    _current.reset(self._token)
                except ValueError:  # exited in a different context than entered
                    _current.set(None)
                self._token = None
        return False

    def end(self, status: Status | None = None, *, _end_mono: int | None = None) -> None:
        """End the span (idempotent). Status: explicit argument > set_status/set_error/exception > ok.

        ``_end_mono`` (internal) ends the span at an earlier monotonic instant, for spans ended
        later on the worker thread on behalf of a finalizer."""
        with self._lock:
            if self._ended:
                self._tracer.diag.inc("span_double_end")
                return
            self._ended = True
        try:
            if status in ("ok", "error", "cancelled"):
                self._status = status
            elapsed = (_end_mono if _end_mono is not None else mono_ns()) - self._start_mono
            event = self._event("span_end")
            event["end_time"] = format_ts(self._start_wall + max(elapsed, 0))
            event["duration_ms"] = S.duration_ms(elapsed)
            event["status"] = self._status or "ok"
            if self._error is not None and event["status"] == "error":
                event["error"] = self._error
            if self._usage is not None:
                event["usage"] = self._usage
            if self._cost is not None:
                event["cost"] = self._cost
            if self._content is not None:
                event["content"] = self._content
            self._tracer.diag.inc("spans_ended")
            self._tracer.processor.on_end((self.trace_id, self.span_id), event)
        except Exception as internal:
            self._tracer.diag.error("span_end", repr(internal)[:200], count="internal_errors")

    @property
    def ended(self) -> bool:
        return self._ended

    def _event(self, event_type: str) -> dict[str, Any]:
        event: dict[str, Any] = {
            "type": event_type,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "kind": self.kind,
            "name": self.name,
            "start_time": format_ts(self._start_wall),
        }
        if self.session_id is not None:
            event["session_id"] = self.session_id
        if self._agent:
            event["agent"] = dict(self._agent)
        if self._tool:
            event["tool"] = dict(self._tool)
        if self._llm:
            event["llm"] = {k: dict(v) if isinstance(v, dict) else v for k, v in self._llm.items()}
        if self._retry:
            event["retry"] = dict(self._retry)
        if self._links:
            event["links"] = list(self._links)
        if self._fingerprint is not None:
            event["input_fingerprint"] = self._fingerprint
        if self._attributes:
            event["attributes"] = dict(self._attributes)
        return event

    # ------------------------------------------------------------------ recording

    def set_attribute(self, key: str, value: Any) -> None:
        self.set_attributes({key: value})

    def set_attributes(self, values: Mapping[str, Any]) -> None:
        if self._ended:
            return
        existing = self._attributes or {}
        clean = S.attributes(dict(values), self._tracer.diag, existing=len(existing))
        if clean:
            self._attributes = {**existing, **clean}

    def add_link(self, trace_id: str, span_id: str, relation: str = "follows_from") -> None:
        if self._ended:
            return
        if not (
            isinstance(trace_id, str)
            and _TRACE_RE.match(trace_id)
            and isinstance(span_id, str)
            and _SPAN_RE.match(span_id)
        ):
            self._tracer.diag.inc("values_sanitized")
            return
        links = self._links or []
        if len(links) < 32:
            links.append({"trace_id": trace_id, "span_id": span_id, "relation": S.vocab(relation, "follows_from")})
            self._links = links

    def set_retry(self, attempt: int, max_attempts: int | None = None) -> None:
        if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 10_000:
            self._tracer.diag.inc("values_sanitized")
            return
        retry = {"attempt": attempt}
        if isinstance(max_attempts, int) and not isinstance(max_attempts, bool) and 1 <= max_attempts <= 10_000:
            retry["max_attempts"] = max_attempts
        self._retry = retry

    def set_tool(self, *, name: str | None = None, type: str | None = None, call_id: str | None = None) -> None:
        tool = dict(self._tool or {"name": self.name})
        if name is not None:
            tool["name"] = S.label(name)
        if type is not None:
            tool["type"] = S.vocab(type)
        if call_id is not None:
            tool["call_id"] = S.label(call_id)
        self._tool = tool

    def set_llm(self, **fields: Any) -> None:
        """Set ``llm`` block fields: provider, operation, request_model, response_model,
        provider_request_id, request={temperature, top_p, max_tokens, stream},
        response={id, finish_reasons}, streaming={time_to_first_token_ms, chunk_count, completed}."""
        llm = self._llm if self._llm is not None else {}
        for key, value in fields.items():
            if value is None:
                continue
            if key in ("provider", "operation"):
                llm[key] = S.vocab(value)
            elif key in ("request_model", "response_model", "provider_request_id"):
                llm[key] = S.label(value)
            elif key == "request" and isinstance(value, Mapping):
                llm["request"] = {**llm.get("request", {}), **_llm_request(value)}
            elif key == "response" and isinstance(value, Mapping):
                llm["response"] = {**llm.get("response", {}), **_llm_response(value)}
            elif key == "streaming" and isinstance(value, Mapping):
                llm["streaming"] = {**llm.get("streaming", {}), **_llm_streaming(value)}
            else:
                self._tracer.diag.inc("values_sanitized")
        for key in ("request", "response", "streaming"):
            if key in llm and not llm[key]:
                del llm[key]
        self._llm = llm

    def fingerprint_input(self, value: Any) -> None:
        """Set ``input_fingerprint`` (HMAC; the key never leaves the process). No-op if disabled."""
        fp = self._tracer.fingerprinter
        if fp is None or self._ended:
            return
        try:
            self._fingerprint = fp.fingerprint(value)
        except Unfingerprintable:
            # No stable form (e.g. an object whose repr is its memory address): unknown, never wrong.
            self._fingerprint = None
            self._tracer.diag.inc("fingerprints_unavailable")
        except Exception as exc:
            self._tracer.diag.error("fingerprint", repr(exc)[:200], count="internal_errors")

    def set_input(self, value: Any, format: ContentFormat | None = None) -> None:
        self._set_content("input", value, format)

    def set_output(self, value: Any, format: ContentFormat | None = None) -> None:
        self._set_content("output", value, format)

    def _set_content(self, direction: str, value: Any, fmt: ContentFormat | None) -> None:
        if self._ended:
            return
        try:
            part, truncated = self._tracer.policy.content_part(value, fmt)
        except Exception as exc:
            self._tracer.diag.error("content", repr(exc)[:200], count="internal_errors")
            part, truncated = {"status": "unavailable"}, False
        if truncated:
            self._tracer.diag.inc("content_truncated")
        content = self._content or {}
        content[direction] = part
        self._content = content

    def set_content_unavailable(self, direction: Literal["input", "output"]) -> None:
        """Mark content the integration cannot observe (distinct from 'not captured by policy')."""
        content = self._content or {}
        content[direction] = {"status": "unavailable"}
        self._content = content

    def record_usage(
        self,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        cache_read_input_tokens: int | None = None,
        cache_write_input_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        source: str = "provider_reported",
    ) -> None:
        """Record token counts exactly as reported. Omitted or invalid counts stay *unknown* —
        never zero. ``source`` must be ``provider_reported`` or ``sdk_estimated``."""
        if self._ended:
            return
        if source not in USAGE_SOURCES:
            self._tracer.diag.error(
                "usage", f"invalid usage source {source!r}; usage not recorded", count="invalid_usage_values"
            )
            return
        given = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
            "cache_write_input_tokens": cache_write_input_tokens,
            "reasoning_tokens": reasoning_tokens,
        }
        usage: dict[str, Any] = {}
        for name, value in given.items():
            if value is None:
                continue
            count = S.token_count(value)
            if count is None:
                self._tracer.diag.inc("invalid_usage_values")
                continue
            usage[name] = count
        if usage:
            usage["source"] = source
            self._usage = usage

    def record_cost(self, amount: Any, currency: str = "USD") -> None:
        """Record a cost **reported by the provider itself**. Never call this with an estimate; the
        platform computes estimates server-side from usage and versioned prices."""
        if self._ended:
            return
        value = S.money(amount)
        code = S.currency(currency)
        if value is None or code is None:
            self._tracer.diag.error(
                "cost", "invalid cost amount or currency; cost not recorded", count="invalid_cost_values"
            )
            return
        self._cost = {"amount": value, "currency": code, "source": "provider_reported"}

    def set_status(self, status: Status) -> None:
        if status in ("ok", "error", "cancelled") and not self._ended:
            self._status = status

    def set_error(
        self,
        error: BaseException | str,
        *,
        message: str | None = None,
        origin: str | None = None,
        code: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        """Mark the span failed without raising (e.g. a tool returned an error result)."""
        if self._ended:
            return
        if isinstance(error, BaseException):
            self._error = _error_block(error, self, origin)
        else:
            block: dict[str, Any] = {"type": S.label(error), "origin": S.vocab(origin or _default_origin(self.kind))}
            if message is not None and self._tracer.policy.error_messages:
                text = redact_error_message(message)
                if text:
                    block["message"] = text
            self._error = block
        if code is not None:
            self._error["code"] = S.label(code, S.MAX_SHORT_LABEL)
        if isinstance(retryable, bool):
            self._error["retryable"] = retryable
        self._status = "error"

    def record_exception(self, exc: BaseException) -> None:
        """Record an exception that is ending the span. Non-``Exception`` base exceptions
        (``CancelledError``, ``KeyboardInterrupt``, ``SystemExit``, ``GeneratorExit``) mean
        *cancelled*, not failed."""
        if self._ended:
            return
        if isinstance(exc, Exception):
            self._error = _error_block(exc, self, None)  # the exception that ended the span wins
            self._status = "error"
        else:
            self._status = "cancelled"

    def event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        """Record a point-in-time span event (e.g. ``retry.scheduled``, ``handoff``)."""
        try:
            event: dict[str, Any] = {
                "type": "span_event",
                "event_id": uuid7(),
                "trace_id": self.trace_id,
                "span_id": self.span_id,
                "name": S.label(name, S.MAX_SHORT_LABEL),
                "time": format_ts(wall_ns()),
            }
            attrs = S.attributes(dict(attributes or {}), self._tracer.diag)
            if attrs:
                event["attributes"] = attrs
            self._tracer.processor.on_event(event)
        except Exception as exc:
            self._tracer.diag.error("span_event", repr(exc)[:200], count="internal_errors")


class NonRecordingSpan:
    """Returned when the SDK is not initialized or disabled. Same interface; does nothing."""

    is_recording = False
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    ended = False

    def __enter__(self) -> NonRecordingSpan:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        return False

    def __getattr__(self, name: str) -> Any:
        return _noop


def _noop(*args: Any, **kwargs: Any) -> None:
    return None


NON_RECORDING = NonRecordingSpan()


def _default_origin(kind: str) -> str:
    if kind == "tool":
        return "tool"
    if kind in ("llm", "embedding"):
        return "provider"
    return "application"


def _error_type(exc: BaseException) -> str:
    cls = type(exc)
    module = cls.__module__
    name = cls.__qualname__ if module == "builtins" else f"{module}.{cls.__qualname__}"
    return S.label(name)


def _error_block(exc: BaseException, span: Span, origin: str | None) -> dict[str, Any]:
    policy = span._tracer.policy
    if origin is None:
        origin = "timeout" if isinstance(exc, TimeoutError) else _default_origin(span.kind)
    block: dict[str, Any] = {"type": _error_type(exc), "origin": S.vocab(origin)}
    message = policy.error_message(exc)
    if message:
        block["message"] = message
    stack = policy.stack(exc)
    if stack:
        block["stack"] = stack
    return block


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _llm_request(value: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("temperature", "top_p"):
        number = _finite(value.get(key))
        if number is not None:
            out[key] = number
    max_tokens = S.token_count(value.get("max_tokens"))
    if max_tokens is not None:
        out["max_tokens"] = max_tokens
    if isinstance(value.get("stream"), bool):
        out["stream"] = value["stream"]
    return out


def _llm_response(value: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if value.get("id") is not None:
        out["id"] = S.label(value["id"])
    reasons = value.get("finish_reasons")
    if isinstance(reasons, (list, tuple)):
        clean = [S.label(r, 64) for r in reasons if isinstance(r, str) and r][:16]
        if clean:
            out["finish_reasons"] = clean
    return out


def _llm_streaming(value: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    ttft = _finite(value.get("time_to_first_token_ms"))
    if ttft is not None and ttft >= 0:
        out["time_to_first_token_ms"] = min(ttft, S.MAX_DURATION_MS)
    chunks = S.token_count(value.get("chunk_count"))
    if chunks is not None:
        out["chunk_count"] = chunks
    if isinstance(value.get("completed"), bool):
        out["completed"] = value["completed"]
    return out
