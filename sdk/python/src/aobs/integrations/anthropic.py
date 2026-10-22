"""Instrumentation for Anthropic Messages API clients.

    from anthropic import Anthropic
    client = aobs.integrations.anthropic.wrap(Anthropic())
    client.messages.create(model=..., max_tokens=..., messages=...)   # recorded as an ``llm`` span
    with client.messages.stream(model=..., max_tokens=..., messages=...) as stream: ...   # also

What it records (when the response contains it — nothing is invented):

* request: model, max_tokens, stream (and temperature / top_p when passed)
* response: response model, message id, ``stop_reason`` as the finish reason, provider request id
  (the ``request-id`` header)
* usage, ``provider_reported``: Anthropic reports ``input_tokens`` *excluding* prompt-cache tokens,
  so ``input_tokens`` is recorded as input_tokens + cache_read_input_tokens +
  cache_creation_input_tokens (the canonical, inclusive meaning; see docs/event-model.md);
  ``cache_read_input_tokens`` -> cache_read_input_tokens, ``cache_creation_input_tokens`` ->
  cache_write_input_tokens, ``output_tokens`` -> output_tokens,
  ``output_tokens_details.thinking_tokens`` -> reasoning_tokens. ``total_tokens`` is not reported
  by Anthropic and stays unknown; so does every other absent field.
* streaming: time to the first ``content_block_delta`` event, event count, whether the stream
  completed. Output tokens are taken only from ``message_delta`` (the count in ``message_start``
  is a placeholder), so a stream closed before it keeps output tokens unknown. A stream closed or
  garbage-collected before the end is recorded as ``cancelled``.
* errors: exception type, HTTP status as ``code``, request id, ``retryable`` for 408/409/429/5xx
  (including 529 overloaded), connection and timeout errors. An ``error`` event in the middle of a
  stream (HTTP status 200) is recorded with its error type (e.g. ``overloaded_error``) as ``code``.
* content (subject to the capture policy): ``system`` and request messages, and the response
  content blocks, converted to the OpenTelemetry GenAI ``{role, parts}`` shape: ``tool_use`` ->
  ``tool_call`` parts, ``tool_result`` -> ``tool_call_response`` parts, ``thinking`` ->
  ``reasoning`` parts; other block types (images, documents, redacted thinking, server tools) are
  recorded by type only.
* ``input_fingerprint`` over the request arguments (excluding transport options).

Never recorded: cost (the Messages API does not report it), API keys, headers.

Duck-typed: it does not import ``anthropic``. Tested against ``anthropic`` 2.1.0. Calls through
``with_raw_response`` / ``with_streaming_response``, ``client.beta.messages``, batches and
``count_tokens`` are not instrumented.
"""

from __future__ import annotations

import contextlib
import inspect
import json
from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any
from urllib.parse import urlsplit

from .. import _api
from .._clock import mono_ns
from .._tracer import NonRecordingSpan, Span
from .._version import __version__

INTEGRATION = "anthropic"
CAPABILITIES = (
    "llm.usage",
    "llm.streaming",
    "llm.request_params",
    "llm.response_metadata",
    "llm.content",
    "llm.errors",
)
_TRANSPORT_KWARGS = frozenset({"timeout", "extra_headers", "extra_query", "stream"})
_RETRYABLE_STATUS = frozenset({408, 409, 429})
# Error ``type`` values of a mid-stream ``error`` event whose HTTP equivalent is retryable
# (rate_limit_error = 429, api_error = 500, timeout_error = 504, overloaded_error = 529).
_RETRYABLE_ERROR_TYPES = frozenset({"rate_limit_error", "api_error", "timeout_error", "overloaded_error"})
_MAX_STREAM_CONTENT_BYTES = 65_536


def wrap(client: Any, *, provider: str | None = None) -> Any:
    """Return a proxy of ``client`` whose ``messages.create`` / ``messages.stream`` /
    ``messages.parse`` are instrumented.

    ``provider`` names the service for filtering (``anthropic``, ``aws.bedrock``, ``gcp.vertex_ai``…).
    If omitted: ``anthropic`` for api.anthropic.com, otherwise ``anthropic_compatible``.
    """
    return _ClientProxy(client, provider or _infer_provider(client))


def _infer_provider(client: Any) -> str:
    try:
        host = urlsplit(str(client.base_url)).hostname or ""
    except Exception:
        return "anthropic_compatible"
    return "anthropic" if host == "api.anthropic.com" else "anthropic_compatible"


def _register() -> None:
    tracer = _api.get_tracer()
    if tracer is not None:
        tracer.register_integration(INTEGRATION, __version__, CAPABILITIES)


class _ClientProxy:
    def __init__(self, client: Any, provider: str) -> None:
        self._aobs_client = client
        self._aobs_provider = provider
        _register()

    @property
    def messages(self) -> _MessagesProxy:
        return _MessagesProxy(self._aobs_client.messages, self._aobs_provider)

    def with_options(self, *args: Any, **kwargs: Any) -> _ClientProxy:
        return _ClientProxy(self._aobs_client.with_options(*args, **kwargs), self._aobs_provider)

    def copy(self, *args: Any, **kwargs: Any) -> _ClientProxy:
        return _ClientProxy(self._aobs_client.copy(*args, **kwargs), self._aobs_provider)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._aobs_client, name)

    def __repr__(self) -> str:
        return f"<aobs-instrumented {self._aobs_client!r}>"


class _MessagesProxy:
    def __init__(self, messages: Any, provider: str) -> None:
        self._messages = messages
        self._provider = provider

    def __getattr__(self, name: str) -> Any:
        return getattr(self._messages, name)

    def create(self, *args: Any, **kwargs: Any) -> Any:
        return self._call(self._messages.create, args, kwargs)

    def parse(self, *args: Any, **kwargs: Any) -> Any:
        return self._call(self._messages.parse, args, kwargs)

    def _call(self, method: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        recorder = _Recorder.start(self._provider, kwargs)
        stream = bool(kwargs.get("stream"))
        try:
            result = method(*args, **kwargs)
        except BaseException as exc:
            recorder.fail(exc)
            raise
        # The async client's create() returns a coroutine; detect async by the *result*, as the
        # openai_compat integration does.
        if inspect.isawaitable(result):
            return recorder.await_result(result, stream)
        return recorder.handle(result, stream, is_async=False)

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        """``messages.stream(...)``: the request is made when the returned manager is entered, so
        the span starts there."""
        manager = self._messages.stream(*args, **kwargs)
        return _StreamManagerProxy(manager, self._provider, kwargs)


class _StreamManagerProxy:
    """Wraps ``MessageStreamManager`` / ``AsyncMessageStreamManager``.

    The helper's ``MessageStream`` reads every event (iteration, ``text_stream``,
    ``get_final_message``, ``until_done``) from its raw SSE stream, so instrumenting that raw stream
    observes all of them. If the helper's shape is not recognised the call is still recorded, without
    streaming details or output."""

    def __init__(self, manager: Any, provider: str, kwargs: Mapping[str, Any]) -> None:
        self._manager = manager
        self._provider = provider
        self._kwargs = {**kwargs, "stream": True}
        self._recorder: _Recorder | None = None
        self._raw: Any = None
        self._observed = False

    def _instrument(self, message_stream: Any, *, is_async: bool) -> None:
        recorder = self._recorder
        raw = getattr(message_stream, "_raw_stream", None)
        if recorder is None or raw is None:
            return
        try:
            if is_async and hasattr(raw, "__aiter__"):
                message_stream._raw_stream = _AsyncStreamProxy(raw, recorder)
            elif not is_async and hasattr(raw, "__iter__"):
                message_stream._raw_stream = _SyncStreamProxy(raw, recorder)
            else:
                return
            self._raw = raw
            self._observed = True
        except Exception as exc:  # pragma: no cover - defensive
            recorder._internal(exc)

    def _before_exit(self, exc: BaseException | None) -> None:
        # An exception leaving the block ends the call first; otherwise closing the stream (done by
        # the manager's exit) records it as completed or cancelled.
        if self._recorder is not None and exc is not None:
            self._recorder.finish_stream(False, self._raw, exc)

    def _after_exit(self) -> None:
        if self._recorder is not None and not self._observed:
            self._recorder.finish_unobserved()

    def __enter__(self) -> Any:
        self._recorder = recorder = _Recorder.start(self._provider, self._kwargs)
        try:
            message_stream = self._manager.__enter__()
        except BaseException as exc:
            recorder.fail(exc)
            raise
        self._instrument(message_stream, is_async=False)
        return message_stream

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> Any:
        self._before_exit(exc)
        try:
            return self._manager.__exit__(exc_type, exc, tb)
        finally:
            self._after_exit()

    async def __aenter__(self) -> Any:
        self._recorder = recorder = _Recorder.start(self._provider, self._kwargs)
        try:
            message_stream = await self._manager.__aenter__()
        except BaseException as exc:
            recorder.fail(exc)
            raise
        self._instrument(message_stream, is_async=True)
        return message_stream

    async def __aexit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> Any:
        self._before_exit(exc)
        try:
            return await self._manager.__aexit__(exc_type, exc, tb)
        finally:
            self._after_exit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)


class _Block:
    """One streamed content block, accumulated for content capture."""

    __slots__ = ("id", "name", "parts", "type")

    def __init__(self, block_type: str, block_id: str | None = None, name: str | None = None) -> None:
        self.type = block_type
        self.id = block_id
        self.name = name
        self.parts: list[str] = []


class _Recorder:
    """Collects one call's data and ends its span exactly once."""

    def __init__(self, span: Span | NonRecordingSpan) -> None:
        self.span = span
        self.start_mono = mono_ns()
        self.first_delta_ms: float | None = None
        self.events = 0
        self.stop_reason: str | None = None
        self.response_model: str | None = None
        self.response_id: str | None = None
        self.start_usage: Any = None
        self.delta_usage: Any = None
        self.blocks: dict[int, _Block] = {}
        self.content_bytes = 0
        self.done = False

    @classmethod
    def start(cls, provider: str, kwargs: Mapping[str, Any]) -> _Recorder:
        _register()
        model = kwargs.get("model")
        span = _api.llm_span(
            provider=provider, request_model=str(model) if model is not None else None, operation="chat"
        )
        recorder = cls(span)
        if span.is_recording:
            try:
                recorder._record_request(kwargs)
            except Exception as exc:  # pragma: no cover - defensive
                recorder._internal(exc)
        return recorder

    def _internal(self, exc: Exception) -> None:
        tracer = _api.get_tracer()
        if tracer is not None:
            tracer.diag.error("anthropic", repr(exc)[:200], count="internal_errors")

    def _record_request(self, kwargs: Mapping[str, Any]) -> None:
        span = self.span
        span.set_llm(
            request={
                "temperature": kwargs.get("temperature"),
                "top_p": kwargs.get("top_p"),
                "max_tokens": kwargs.get("max_tokens"),
                "stream": kwargs.get("stream") if isinstance(kwargs.get("stream"), bool) else None,
            }
        )
        span.fingerprint_input({k: v for k, v in kwargs.items() if k not in _TRANSPORT_KWARGS})
        messages = kwargs.get("messages")
        if messages is not None:
            if _captures_content():
                span.set_input(to_otel_messages(messages, system=kwargs.get("system")), "messages")
            else:
                span.set_input(None)  # records "not_captured"

    # ------------------------------------------------------------------ outcomes

    def fail(self, exc: BaseException) -> None:
        if self.done:
            return
        self.done = True
        span = self.span
        try:
            if span.is_recording and isinstance(exc, Exception):
                status = getattr(exc, "status_code", None)
                name = type(exc).__name__
                origin = "timeout" if "Timeout" in name else "provider"
                code = str(status) if isinstance(status, int) else None
                retryable = (isinstance(status, int) and (status in _RETRYABLE_STATUS or status >= 500)) or name in (
                    "APIConnectionError",
                    "APITimeoutError",
                )
                if isinstance(status, int) and 200 <= status < 300:
                    # An ``error`` event inside a stream that started with HTTP 200: the status says
                    # nothing about the failure; the event's error type does.
                    error_type = _str_or_none(_as_dict(_as_dict(getattr(exc, "body", None)).get("error")).get("type"))
                    code = error_type
                    retryable = error_type in _RETRYABLE_ERROR_TYPES
                span.set_error(exc, origin=origin, code=code, retryable=True if retryable else None)
                request_id = getattr(exc, "request_id", None)
                if isinstance(request_id, str):
                    span.set_llm(provider_request_id=request_id)
            elif span.is_recording:
                span.record_exception(exc)
        except Exception as internal:  # pragma: no cover - defensive
            self._internal(internal)
        span.end()

    async def await_result(self, awaitable: Any, stream: bool) -> Any:
        try:
            result = await awaitable
        except BaseException as exc:
            self.fail(exc)
            raise
        return self.handle(result, stream, is_async=True)

    def handle(self, result: Any, stream: bool, *, is_async: bool) -> Any:
        if stream:
            if is_async and hasattr(result, "__aiter__"):
                return _AsyncStreamProxy(result, self)
            if hasattr(result, "__iter__"):
                return _SyncStreamProxy(result, self)
        self._finish_message(result)
        return result

    def _finish_message(self, message: Any) -> None:
        if self.done:
            return
        self.done = True
        span = self.span
        if span.is_recording:
            try:
                stop_reason = _str_or_none(getattr(message, "stop_reason", None))
                span.set_llm(
                    response_model=_str_or_none(getattr(message, "model", None)),
                    provider_request_id=_str_or_none(getattr(message, "_request_id", None)),
                    response={
                        "id": _str_or_none(getattr(message, "id", None)),
                        "finish_reasons": [stop_reason] if stop_reason else [],
                    },
                )
                usage = getattr(message, "usage", None)
                _record_usage(span, usage, usage)
                if _captures_content():
                    span.set_output(_output_messages(getattr(message, "content", None), stop_reason), "messages")
                else:
                    span.set_output(None)
            except Exception as exc:  # pragma: no cover - defensive
                self._internal(exc)
        span.end()

    def finish_unobserved(self) -> None:
        """End a ``messages.stream`` call whose events could not be observed."""
        if self.done:
            return
        self.done = True
        if self.span.is_recording:
            self.span.set_content_unavailable("output")
        self.span.end()

    # ------------------------------------------------------------------ streaming

    def on_event(self, event: Any) -> None:
        if self.done or not self.span.is_recording:
            return
        try:
            self.events += 1
            kind = getattr(event, "type", None)
            if kind == "content_block_delta":
                if self.first_delta_ms is None:
                    self.first_delta_ms = (mono_ns() - self.start_mono) / 1_000_000
                self._on_delta(getattr(event, "index", None), getattr(event, "delta", None))
            elif kind == "message_start":
                message = getattr(event, "message", None)
                self.response_model = _str_or_none(getattr(message, "model", None)) or self.response_model
                self.response_id = _str_or_none(getattr(message, "id", None)) or self.response_id
                self.start_usage = getattr(message, "usage", None)
            elif kind == "message_delta":
                reason = _str_or_none(getattr(getattr(event, "delta", None), "stop_reason", None))
                self.stop_reason = reason or self.stop_reason
                if getattr(event, "usage", None) is not None:
                    self.delta_usage = event.usage
            elif kind == "content_block_start" and _captures_content():
                index = getattr(event, "index", None)
                block = getattr(event, "content_block", None)
                if isinstance(index, int) and block is not None:
                    self.blocks[index] = _Block(
                        str(getattr(block, "type", "unknown")),
                        _str_or_none(getattr(block, "id", None)),
                        _str_or_none(getattr(block, "name", None)),
                    )
                    text = getattr(block, "text", None)
                    if isinstance(text, str) and text:
                        self._append(self.blocks[index], text)
        except Exception as exc:  # pragma: no cover - defensive
            self._internal(exc)

    def _on_delta(self, index: Any, delta: Any) -> None:
        if not isinstance(index, int) or not _captures_content():
            return
        block = self.blocks.get(index)
        if block is None:
            return
        delta_type = getattr(delta, "type", None)
        field = {"text_delta": "text", "input_json_delta": "partial_json", "thinking_delta": "thinking"}.get(
            str(delta_type)
        )
        if field is not None:
            value = getattr(delta, field, None)
            if isinstance(value, str):
                self._append(block, value)

    def _append(self, block: _Block, text: str) -> None:
        if self.content_bytes < _MAX_STREAM_CONTENT_BYTES:
            block.parts.append(text)
            self.content_bytes += len(text.encode("utf-8"))

    def finish_stream(
        self, completed: bool, stream: Any = None, exc: BaseException | None = None, end_mono: int | None = None
    ) -> None:
        if self.done:
            return
        if exc is not None:
            self._record_stream_metadata(completed=False, stream=stream)
            self._record_stream_usage()
            self.fail(exc)
            return
        self.done = True
        span = self.span
        if span.is_recording:
            try:
                self._record_stream_metadata(completed, stream)
                self._record_stream_usage()
                if _captures_content():
                    message: dict[str, Any] = {"role": "assistant", "parts": self._stream_parts()}
                    if self.stop_reason:
                        message["finish_reason"] = self.stop_reason
                    span.set_output([message], "messages")
                else:
                    span.set_output(None)
                if not completed:
                    span.set_status("cancelled")
            except Exception as internal:  # pragma: no cover - defensive
                self._internal(internal)
        if end_mono is not None and isinstance(span, Span):
            span.end(_end_mono=end_mono)
        else:
            span.end()

    def _stream_parts(self) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        for _, block in sorted(self.blocks.items()):
            text = "".join(block.parts)
            if block.type == "text":
                parts.append({"type": "text", "content": text})
            elif block.type == "thinking":
                parts.append({"type": "reasoning", "content": text})
            elif block.type == "tool_use":
                arguments: Any = text  # incomplete JSON (stream cut short or capped): the text received
                with contextlib.suppress(ValueError):
                    arguments = json.loads(text) if text else {}
                parts.append({"type": "tool_call", "id": block.id, "name": block.name, "arguments": arguments})
            else:
                parts.append({"type": block.type})
        return parts

    def _record_stream_usage(self) -> None:
        if self.span.is_recording and (self.start_usage is not None or self.delta_usage is not None):
            # Input and cache counts: message_delta's cumulative values when present, else
            # message_start's. Output: message_delta only (message_start carries a placeholder).
            _record_usage(self.span, _Merged(self.delta_usage, self.start_usage), self.delta_usage)

    def _record_stream_metadata(self, completed: bool, stream: Any) -> None:
        if not self.span.is_recording:
            return
        request_id = None
        response = getattr(stream, "response", None)
        headers = getattr(response, "headers", None)
        if headers is not None:
            try:
                request_id = headers.get("request-id")
            except Exception:
                request_id = None
        self.span.set_llm(
            response_model=self.response_model,
            provider_request_id=_str_or_none(request_id),
            response={"id": self.response_id, "finish_reasons": [self.stop_reason] if self.stop_reason else []},
            streaming={
                "time_to_first_token_ms": self.first_delta_ms,
                "chunk_count": self.events,
                "completed": completed,
            },
        )


class _Merged:
    """Attribute lookup over two usage objects: the first one's non-None value wins."""

    def __init__(self, primary: Any, fallback: Any) -> None:
        self._primary = primary
        self._fallback = fallback

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._primary, name, None)
        return value if value is not None else getattr(self._fallback, name, None)


class _SyncStreamProxy:
    def __init__(self, stream: Any, recorder: _Recorder) -> None:
        self._stream = stream
        self._recorder = recorder
        self._iterator: Iterator[Any] | None = None

    def __iter__(self) -> _SyncStreamProxy:
        return self

    def __next__(self) -> Any:
        if self._iterator is None:
            self._iterator = iter(self._stream)
        try:
            event = next(self._iterator)
        except StopIteration:
            self._recorder.finish_stream(True, self._stream)
            raise
        except BaseException as exc:
            self._recorder.finish_stream(False, self._stream, exc)
            raise
        self._recorder.on_event(event)
        return event

    def close(self) -> None:
        self._recorder.finish_stream(False, self._stream)
        self._stream.close()

    def __enter__(self) -> _SyncStreamProxy:
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> None:
        if exc is not None:
            self._recorder.finish_stream(False, self._stream, exc)
        self.close()

    def __del__(self) -> None:
        _abandoned(self)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class _AsyncStreamProxy:
    def __init__(self, stream: Any, recorder: _Recorder) -> None:
        self._stream = stream
        self._recorder = recorder
        self._iterator: AsyncIterator[Any] | None = None

    def __aiter__(self) -> _AsyncStreamProxy:
        return self

    async def __anext__(self) -> Any:
        if self._iterator is None:
            self._iterator = self._stream.__aiter__()
        try:
            event = await self._iterator.__anext__()
        except StopAsyncIteration:
            self._recorder.finish_stream(True, self._stream)
            raise
        except BaseException as exc:
            self._recorder.finish_stream(False, self._stream, exc)
            raise
        self._recorder.on_event(event)
        return event

    async def close(self) -> None:
        self._recorder.finish_stream(False, self._stream)
        await self._stream.close()

    async def __aenter__(self) -> _AsyncStreamProxy:
        return self

    async def __aexit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> None:
        if exc is not None:
            self._recorder.finish_stream(False, self._stream, exc)
        await self.close()

    def __del__(self) -> None:
        _abandoned(self)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _abandoned(proxy: Any) -> None:
    """Finalizer for a stream proxy that was never exhausted or closed: record it as cancelled.

    Runs inside ``__del__``, possibly during cyclic GC while this thread holds an SDK lock, so it
    must not take locks: it only enqueues the work (``SimpleQueue.put`` is reentrant-safe)."""
    recorder = getattr(proxy, "_recorder", None)
    if recorder is None or recorder.done:
        return
    end_mono = mono_ns()
    stream = getattr(proxy, "_stream", None)
    tracer = _api.get_tracer()
    if tracer is not None:
        tracer.processor.defer(lambda: recorder.finish_stream(False, stream, end_mono=end_mono))


# ---------------------------------------------------------------------------------- helpers


def _captures_content() -> bool:
    tracer = _api.get_tracer()
    return tracer is not None and tracer.policy.captures_content


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _record_usage(span: Span | NonRecordingSpan, usage: Any, output_usage: Any) -> None:
    """Map Anthropic usage to canonical fields. Missing fields stay unknown (never zero).

    ``output_usage`` is the object output tokens are read from (the message's usage, or a stream's
    ``message_delta`` usage)."""
    if usage is None:
        return
    uncached = _int_or_none(getattr(usage, "input_tokens", None))
    cache_read = _int_or_none(getattr(usage, "cache_read_input_tokens", None))
    cache_write = _int_or_none(getattr(usage, "cache_creation_input_tokens", None))
    # Anthropic's input_tokens excludes cache reads and writes; the canonical input_tokens includes
    # them. A cache field the response leaves out (or null) adds nothing and itself stays unknown.
    input_tokens = None if uncached is None else uncached + (cache_read or 0) + (cache_write or 0)
    details = getattr(output_usage, "output_tokens_details", None)
    span.record_usage(
        input_tokens=input_tokens,
        output_tokens=_int_or_none(getattr(output_usage, "output_tokens", None)),
        cache_read_input_tokens=cache_read,
        cache_write_input_tokens=cache_write,
        reasoning_tokens=_int_or_none(getattr(details, "thinking_tokens", None)),
        source="provider_reported",
    )


def _as_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, Mapping):
        return dict(obj)
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            result = dump(exclude_none=True)
            return result if isinstance(result, dict) else {}
        except Exception:
            return {}
    return {}


def _parts(content: Any) -> list[dict[str, Any]]:
    """Anthropic content (a string or a list of content blocks) -> GenAI parts."""
    if isinstance(content, str):
        return [{"type": "text", "content": content}]
    parts: list[dict[str, Any]] = []
    for block in content if isinstance(content, (list, tuple)) else []:
        bd = _as_dict(block)
        kind = bd.get("type")
        if kind == "text":
            parts.append({"type": "text", "content": bd.get("text")})
        elif kind == "tool_use":
            parts.append(
                {"type": "tool_call", "id": bd.get("id"), "name": bd.get("name"), "arguments": bd.get("input")}
            )
        elif kind == "tool_result":
            result = bd.get("content")
            part: dict[str, Any] = {
                "type": "tool_call_response",
                "id": bd.get("tool_use_id"),
                "response": result if isinstance(result, str) or result is None else _parts(result),
            }
            parts.append(part)
        elif kind == "thinking":
            parts.append({"type": "reasoning", "content": bd.get("thinking")})
        else:
            parts.append({"type": str(kind or "unknown")})
    return parts


def to_otel_messages(messages: Any, *, system: Any = None) -> list[dict[str, Any]]:
    """Anthropic ``system`` + messages -> OpenTelemetry GenAI ``{role, parts}`` messages.

    Text is kept; ``tool_use`` / ``tool_result`` blocks become ``tool_call`` /
    ``tool_call_response`` parts and ``thinking`` becomes ``reasoning``; other block types (images,
    documents, redacted thinking, server tools) are recorded by type only — their data is not
    captured."""
    out: list[dict[str, Any]] = []
    if isinstance(system, (str, list, tuple)):
        out.append({"role": "system", "parts": _parts(system)})
    for message in messages if isinstance(messages, (list, tuple)) else []:
        md = _as_dict(message)
        out.append({"role": md.get("role", "unknown"), "parts": _parts(md.get("content"))})
    return out


def _output_messages(content: Any, stop_reason: str | None) -> list[dict[str, Any]]:
    message: dict[str, Any] = {"role": "assistant", "parts": _parts(content)}
    if stop_reason:
        message["finish_reason"] = stop_reason
    return [message]
