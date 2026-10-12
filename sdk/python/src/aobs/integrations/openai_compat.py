"""Instrumentation for OpenAI-compatible Chat Completions clients.

    from openai import OpenAI
    client = aobs.integrations.openai_compat.wrap(OpenAI(), provider="openai")
    client.chat.completions.create(model=..., messages=...)   # recorded as an ``llm`` span

What it records (when the response contains it — nothing is invented):

* request: model, temperature, top_p, max_tokens / max_completion_tokens, stream
* response: response model, id, finish reasons, provider request id (``x-request-id``)
* usage, ``provider_reported``: prompt_tokens -> input_tokens, completion_tokens -> output_tokens,
  total_tokens, prompt_tokens_details.cached_tokens -> cache_read_input_tokens,
  prompt_tokens_details.cache_write_tokens -> cache_write_input_tokens,
  completion_tokens_details.reasoning_tokens -> reasoning_tokens. Absent fields stay unknown.
  Streaming responses only carry usage when ``stream_options={"include_usage": True}``.
* streaming: time to first chunk, chunk count, whether the stream completed. A stream closed or
  garbage-collected before the end is recorded as ``cancelled``.
* errors: exception type, HTTP status as ``code``, request id, ``retryable`` for 408/409/429/5xx,
  connection and timeout errors.
* content (subject to the capture policy): request messages and response choices, converted to
  the OpenTelemetry GenAI ``{role, parts}`` shape.
* ``input_fingerprint`` over the request arguments (excluding transport options).

Never recorded: cost (Chat Completions responses do not report it), API keys, headers.

Duck-typed: it does not import ``openai``. Tested against ``openai`` 3.29.0. Calls through
``with_raw_response`` / ``with_streaming_response`` and other endpoints are not instrumented.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any
from urllib.parse import urlsplit

from .. import _api
from .._clock import mono_ns
from .._tracer import NonRecordingSpan, Span
from .._version import __version__

INTEGRATION = "openai_compat"
CAPABILITIES = (
    "llm.usage",
    "llm.streaming",
    "llm.request_params",
    "llm.response_metadata",
    "llm.content",
    "llm.errors",
)
_TRANSPORT_KWARGS = frozenset({"timeout", "extra_headers", "extra_query", "stream", "stream_options"})
_RETRYABLE_STATUS = frozenset({408, 409, 429})


def wrap(client: Any, *, provider: str | None = None) -> Any:
    """Return a proxy of ``client`` whose ``chat.completions.create`` is instrumented.

    ``provider`` names the service for filtering (``openai``, ``azure_openai``, ``groq``, ``vllm``…).
    If omitted: ``openai`` for api.openai.com, otherwise ``openai_compatible``.
    """
    return _ClientProxy(client, provider or _infer_provider(client))


def _infer_provider(client: Any) -> str:
    try:
        host = urlsplit(str(client.base_url)).hostname or ""
    except Exception:
        return "openai_compatible"
    return "openai" if host == "api.openai.com" else "openai_compatible"


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
    def chat(self) -> _ChatProxy:
        return _ChatProxy(self._aobs_client.chat, self._aobs_provider)

    def with_options(self, *args: Any, **kwargs: Any) -> _ClientProxy:
        return _ClientProxy(self._aobs_client.with_options(*args, **kwargs), self._aobs_provider)

    def copy(self, *args: Any, **kwargs: Any) -> _ClientProxy:
        return _ClientProxy(self._aobs_client.copy(*args, **kwargs), self._aobs_provider)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._aobs_client, name)

    def __repr__(self) -> str:
        return f"<aobs-instrumented {self._aobs_client!r}>"


class _ChatProxy:
    def __init__(self, chat: Any, provider: str) -> None:
        self._chat = chat
        self._provider = provider

    @property
    def completions(self) -> _CompletionsProxy:
        return _CompletionsProxy(self._chat.completions, self._provider)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._chat, name)


class _CompletionsProxy:
    def __init__(self, completions: Any, provider: str) -> None:
        self._completions = completions
        self._provider = provider

    def __getattr__(self, name: str) -> Any:
        return getattr(self._completions, name)

    def create(self, *args: Any, **kwargs: Any) -> Any:
        recorder = _Recorder.start(self._provider, kwargs)
        try:
            result = self._completions.create(*args, **kwargs)
        except BaseException as exc:
            recorder.fail(exc)
            raise
        # The async client's create() is not a coroutine function (verified with openai 3.29), so
        # detect async by the *result*.
        if inspect.isawaitable(result):
            return recorder.await_result(result, bool(kwargs.get("stream")))
        return recorder.handle(result, bool(kwargs.get("stream")), is_async=False)


class _Recorder:
    """Collects one call's data and ends its span exactly once."""

    def __init__(self, span: Span | NonRecordingSpan) -> None:
        self.span = span
        self.start_mono = mono_ns()
        self.first_chunk_ms: float | None = None
        self.chunks = 0
        self.finish_reasons: list[str] = []
        self.response_model: str | None = None
        self.response_id: str | None = None
        self.stream_text: list[str] = []
        self.stream_text_bytes = 0
        self.usage: Any = None
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
            tracer.diag.error("openai_compat", repr(exc)[:200], count="internal_errors")

    def _record_request(self, kwargs: Mapping[str, Any]) -> None:
        span = self.span
        max_tokens = kwargs.get("max_completion_tokens", kwargs.get("max_tokens"))
        span.set_llm(
            request={
                "temperature": kwargs.get("temperature"),
                "top_p": kwargs.get("top_p"),
                "max_tokens": max_tokens,
                "stream": kwargs.get("stream") if isinstance(kwargs.get("stream"), bool) else None,
            }
        )
        span.fingerprint_input({k: v for k, v in kwargs.items() if k not in _TRANSPORT_KWARGS})
        messages = kwargs.get("messages")
        if messages is not None:
            tracer = _api.get_tracer()
            if tracer is not None and tracer.policy.captures_content:
                span.set_input(to_otel_messages(messages), "messages")
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
                retryable = (isinstance(status, int) and (status in _RETRYABLE_STATUS or status >= 500)) or name in (
                    "APIConnectionError",
                    "APITimeoutError",
                )
                span.set_error(
                    exc,
                    origin=origin,
                    code=str(status) if isinstance(status, int) else None,
                    retryable=True if retryable else None,
                )
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
        self._finish_completion(result)
        return result

    def _finish_completion(self, response: Any) -> None:
        if self.done:
            return
        self.done = True
        span = self.span
        if span.is_recording:
            try:
                choices = getattr(response, "choices", None) or []
                reasons = [c.finish_reason for c in choices if isinstance(getattr(c, "finish_reason", None), str)]
                span.set_llm(
                    response_model=_str_or_none(getattr(response, "model", None)),
                    provider_request_id=_str_or_none(getattr(response, "_request_id", None)),
                    response={"id": _str_or_none(getattr(response, "id", None)), "finish_reasons": reasons},
                )
                _record_usage(span, getattr(response, "usage", None))
                if _captures_content():
                    span.set_output(_output_messages(choices), "messages")
                else:
                    span.set_output(None)
            except Exception as exc:  # pragma: no cover - defensive
                self._internal(exc)
        span.end()

    # ------------------------------------------------------------------ streaming

    def on_chunk(self, chunk: Any) -> None:
        if self.done or not self.span.is_recording:
            return
        try:
            self.chunks += 1
            if self.first_chunk_ms is None:
                self.first_chunk_ms = (mono_ns() - self.start_mono) / 1_000_000
            self.response_model = _str_or_none(getattr(chunk, "model", None)) or self.response_model
            self.response_id = _str_or_none(getattr(chunk, "id", None)) or self.response_id
            if getattr(chunk, "usage", None) is not None:
                self.usage = chunk.usage
            for choice in getattr(chunk, "choices", None) or []:
                reason = getattr(choice, "finish_reason", None)
                if isinstance(reason, str):
                    self.finish_reasons.append(reason)
                text = getattr(getattr(choice, "delta", None), "content", None)
                if isinstance(text, str) and _captures_content() and self.stream_text_bytes < 65_536:
                    self.stream_text.append(text)
                    self.stream_text_bytes += len(text.encode("utf-8"))
        except Exception as exc:  # pragma: no cover - defensive
            self._internal(exc)

    def finish_stream(
        self, completed: bool, stream: Any = None, exc: BaseException | None = None, end_mono: int | None = None
    ) -> None:
        if self.done:
            return
        if exc is not None:
            self._record_stream_metadata(completed=False, stream=stream)
            self.fail(exc)
            return
        self.done = True
        span = self.span
        if span.is_recording:
            try:
                self._record_stream_metadata(completed, stream)
                _record_usage(span, self.usage)
                if _captures_content():
                    span.set_output(
                        [{"role": "assistant", "parts": [{"type": "text", "content": "".join(self.stream_text)}]}],
                        "messages",
                    )
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

    def _record_stream_metadata(self, completed: bool, stream: Any) -> None:
        if not self.span.is_recording:
            return
        request_id = None
        response = getattr(stream, "response", None)
        headers = getattr(response, "headers", None)
        if headers is not None:
            try:
                request_id = headers.get("x-request-id")
            except Exception:
                request_id = None
        self.span.set_llm(
            response_model=self.response_model,
            provider_request_id=_str_or_none(request_id),
            response={"id": self.response_id, "finish_reasons": self.finish_reasons},
            streaming={
                "time_to_first_token_ms": self.first_chunk_ms,
                "chunk_count": self.chunks,
                "completed": completed,
            },
        )


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
            chunk = next(self._iterator)
        except StopIteration:
            self._recorder.finish_stream(True, self._stream)
            raise
        except BaseException as exc:
            self._recorder.finish_stream(False, self._stream, exc)
            raise
        self._recorder.on_chunk(chunk)
        return chunk

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
            chunk = await self._iterator.__anext__()
        except StopAsyncIteration:
            self._recorder.finish_stream(True, self._stream)
            raise
        except BaseException as exc:
            self._recorder.finish_stream(False, self._stream, exc)
            raise
        self._recorder.on_chunk(chunk)
        return chunk

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


def _record_usage(span: Span | NonRecordingSpan, usage: Any) -> None:
    """Map provider usage to canonical fields. Missing fields stay unknown (never zero)."""
    if usage is None:
        return
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    completion_details = getattr(usage, "completion_tokens_details", None)
    span.record_usage(
        input_tokens=_int_or_none(getattr(usage, "prompt_tokens", None)),
        output_tokens=_int_or_none(getattr(usage, "completion_tokens", None)),
        total_tokens=_int_or_none(getattr(usage, "total_tokens", None)),
        cache_read_input_tokens=_int_or_none(getattr(prompt_details, "cached_tokens", None)),
        cache_write_input_tokens=_int_or_none(getattr(prompt_details, "cache_write_tokens", None)),
        reasoning_tokens=_int_or_none(getattr(completion_details, "reasoning_tokens", None)),
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


def to_otel_messages(messages: Any) -> list[dict[str, Any]]:
    """OpenAI chat messages -> OpenTelemetry GenAI ``{role, parts}`` messages.

    Text is kept; tool calls and tool results are mapped; other part types (images, audio, files)
    are recorded by type only — their data is not captured."""
    out: list[dict[str, Any]] = []
    for message in messages if isinstance(messages, (list, tuple)) else []:
        md = _as_dict(message)
        role = md.get("role", "unknown")
        content = md.get("content")
        parts: list[dict[str, Any]] = []
        if role == "tool":
            parts.append({"type": "tool_call_response", "id": md.get("tool_call_id"), "response": content})
        elif isinstance(content, str):
            parts.append({"type": "text", "content": content})
        elif isinstance(content, (list, tuple)):
            for part in content:
                pd = _as_dict(part)
                if pd.get("type") == "text":
                    parts.append({"type": "text", "content": pd.get("text")})
                else:
                    parts.append({"type": str(pd.get("type", "unknown"))})
        for call in md.get("tool_calls") or []:
            cd = _as_dict(call)
            fn = _as_dict(cd.get("function"))
            parts.append(
                {"type": "tool_call", "id": cd.get("id"), "name": fn.get("name"), "arguments": fn.get("arguments")}
            )
        out.append({"role": role, "parts": parts})
    return out


def _output_messages(choices: Any) -> list[dict[str, Any]]:
    out = []
    for choice in choices:
        messages = to_otel_messages([getattr(choice, "message", None)])
        if messages:
            message = messages[0]
            reason = getattr(choice, "finish_reason", None)
            if isinstance(reason, str):
                message["finish_reason"] = reason
            out.append(message)
    return out
