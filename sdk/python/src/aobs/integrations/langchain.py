"""LangChain / LangGraph callback handler.

    from aobs.integrations.langchain import CallbackHandler
    handler = CallbackHandler()
    graph.invoke(inputs, config={"callbacks": [handler]})     # or ainvoke / stream / astream

Each LangChain run becomes an aobs span; LangChain's ``run_id``/``parent_run_id`` give the nesting:

* the outermost chain or graph -> ``agent`` span (a new trace, or a child of the aobs span that is
  current when the graph is invoked, e.g. inside ``aobs.run(...)``); nested chains and LangGraph
  nodes -> ``step`` spans (nodes carry ``langgraph.node`` / ``langgraph.step`` attributes)
* chat model and LLM calls -> ``llm`` spans: provider and model from LangChain's ``ls_*`` run
  metadata (or invocation params), temperature / max_tokens, response model and id, finish
  reasons, streaming (time to first token, chunk count) when tokens are streamed
* tools -> ``tool`` spans (name, ``call_id``); retrievers -> ``retrieval`` spans
* usage, ``provider_reported``, only when LangChain reports it: ``AIMessage.usage_metadata``
  (``input_tokens``, ``output_tokens``, ``total_tokens``, ``input_token_details.cache_read`` /
  ``cache_creation``, ``output_token_details.reasoning``), else an OpenAI-shaped
  ``llm_output["token_usage"]`` (``prompt_tokens``, ``completion_tokens``, ``total_tokens``).
  Absent counts stay unknown; nothing is estimated.
* errors: an exception ends the span as ``error`` (``origin`` ``tool`` / ``provider`` /
  ``application``); cancellation (``CancelledError``, ``KeyboardInterrupt``) as ``cancelled``; a
  tool that returns an error ``ToolMessage`` (``handle_tool_error``) as ``error`` too. LangGraph's
  control-flow exceptions are not failures: an interrupt ends the node span ``cancelled``, a
  ``ParentCommand`` ``ok``, both with a ``langgraph.control_flow`` attribute.
* content (subject to the capture policy): model messages in the OpenTelemetry GenAI
  ``{role, parts}`` shape, tool arguments and results, retriever queries and documents, chain
  inputs and outputs. ``input_fingerprint`` over tool arguments, model requests and queries.

Never recorded: cost, API keys, headers, provider request ids (LangChain does not pass them on).

``import aobs.integrations.langchain`` needs only the standard library; ``langchain-core`` is
imported the first time ``CallbackHandler`` is used. Tested with ``langchain-core`` 1.6.13 and
``langgraph`` 1.2.16 using LangChain's fake chat models (no provider was called).
"""

from __future__ import annotations

import importlib
import threading
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from .. import _api
from .._clock import mono_ns
from .._tracer import NonRecordingSpan, Span, Status
from .._version import __version__
from .openai_compat import _int_or_none, _str_or_none

INTEGRATION = "langchain"
CAPABILITIES = (
    "llm.usage",
    "llm.streaming",
    "llm.request_params",
    "llm.response_metadata",
    "llm.content",
    "llm.errors",
    "tool.arguments",
    "tool.results",
    "tool.errors",
    "retrieval.content",
    "chain.content",
)
MAX_OPEN_RUNS = 10_000  # runs started but never ended (a LangChain bug or an abandoned stream) are capped
_ROLES = {
    "human": "user",
    "ai": "assistant",
    "system": "system",
    "developer": "system",
    "tool": "tool",
    "function": "tool",
}
_RETRYABLE_STATUS = frozenset({408, 409, 429})
_SESSION_KEYS = ("session_id", "thread_id", "conversation_id")  # the metadata keys LangSmith groups threads by
_HIDDEN_TAG = "langsmith:hidden"  # LangChain/LangGraph plumbing runs that tracers are told not to show
_MAX_DEPTH = 32

AnySpan = Span | NonRecordingSpan

_handler_class: type[Any] | None = None
_class_lock = threading.Lock()


def __getattr__(name: str) -> Any:
    if name == "CallbackHandler":
        return _callback_handler_class()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _callback_handler_class() -> type[Any]:
    """Build ``CallbackHandler`` (a ``langchain_core`` ``BaseCallbackHandler``) on first use."""
    global _handler_class
    with _class_lock:
        if _handler_class is None:
            try:
                base = importlib.import_module("langchain_core.callbacks").BaseCallbackHandler
            except ImportError as exc:
                raise ImportError(
                    "aobs.integrations.langchain.CallbackHandler needs langchain-core (pip install langchain-core)"
                ) from exc
            _handler_class = type(
                "CallbackHandler",
                (_Callbacks, base),
                {"__module__": __name__, "__qualname__": "CallbackHandler", "__doc__": _Callbacks.__doc__},
            )
        return _handler_class


def _register() -> None:
    tracer = _api.get_tracer()
    if tracer is not None:
        tracer.register_integration(INTEGRATION, __version__, CAPABILITIES)


def _internal(exc: Exception) -> None:
    tracer = _api.get_tracer()
    if tracer is not None:
        tracer.diag.error("langchain", repr(exc)[:200], count="internal_errors")


def _captures_content() -> bool:
    tracer = _api.get_tracer()
    return tracer is not None and tracer.policy.captures_content


def _fingerprints() -> bool:
    tracer = _api.get_tracer()
    return tracer is not None and tracer.fingerprinter is not None


class _Run:
    __slots__ = ("attach", "chunks", "first_token_ms", "span", "start_mono")

    def __init__(self, span: AnySpan | None, attach: Span | None) -> None:
        self.span = span  # None for a hidden run: it has no span, its children attach to `attach`
        self.attach = attach  # the span children of this run are parented to
        self.start_mono = mono_ns()
        self.chunks = 0
        self.first_token_ms: float | None = None


class _Callbacks:
    """Records LangChain / LangGraph runs as aobs spans. Pass it in ``config={"callbacks": [handler]}``.

    One handler can serve any number of concurrent invocations (threads or asyncio tasks): runs are
    tracked by LangChain's ``run_id``. It never raises into LangChain and does no I/O."""

    # Recording only enqueues events, so async callback managers may call it directly on the event
    # loop (in order) instead of in a thread pool.
    run_inline = True
    raise_error = False

    def __init__(self) -> None:
        super().__init__()
        self._runs: dict[UUID, _Run] = {}
        self._runs_lock = threading.Lock()
        _register()

    def __deepcopy__(self, memo: dict[int, Any]) -> Any:
        return self  # copies of a config must keep reporting into the same run map

    def __copy__(self) -> Any:
        return self

    # ------------------------------------------------------------------ run bookkeeping

    def _start(
        self,
        run_id: UUID,
        parent_run_id: UUID | None,
        name: str,
        kind: str,
        *,
        hidden: bool = False,
        **fields: Any,
    ) -> AnySpan | None:
        """Open the span for ``run_id`` under its parent run's span (or, for a run whose parent this
        handler did not see, under the current aobs span). A hidden run gets no span (returns None);
        its children attach to its parent."""
        with self._runs_lock:
            parent = self._runs.get(parent_run_id) if parent_run_id is not None else None
            if len(self._runs) >= MAX_OPEN_RUNS:
                tracer = _api.get_tracer()
                if tracer is not None:
                    tracer.diag.inc("uninstrumented_calls")
                return None
        attach = parent.attach if parent is not None else None
        if hidden:
            state = _Run(None, attach)
            span: AnySpan | None = None
        else:
            if parent is None:
                _register()  # the SDK may have been initialized after the handler was created
            span = _api._start(name, kind, parent=attach, attributes={"langchain.run_id": str(run_id)}, **fields)
            state = _Run(span, span if isinstance(span, Span) else attach)
        with self._runs_lock:
            self._runs[run_id] = state
        return span

    def _tracked(self, run_id: UUID | None) -> bool:
        with self._runs_lock:
            return run_id is not None and run_id in self._runs

    def _pop(self, run_id: UUID) -> _Run | None:
        with self._runs_lock:
            return self._runs.pop(run_id, None)

    def _get(self, run_id: UUID) -> _Run | None:
        with self._runs_lock:
            return self._runs.get(run_id)

    def _fail(self, run_id: UUID, error: BaseException) -> None:
        state = self._pop(run_id)
        if state is None or state.span is None:
            return
        span = state.span
        if span.is_recording:
            try:
                bubble = _bubble_up_status(error)
                if bubble is not None:
                    span.set_attribute("langgraph.control_flow", type(error).__name__)
                    span.end(bubble)
                    return
                if isinstance(error, Exception) and isinstance(span, Span) and span.kind == "llm":
                    status = getattr(error, "status_code", None)
                    code = status if isinstance(status, int) and not isinstance(status, bool) else None
                    span.set_error(
                        error,
                        code=str(code) if code is not None else None,
                        retryable=True if code is not None and (code in _RETRYABLE_STATUS or code >= 500) else None,
                    )
                elif isinstance(error, Exception):
                    span.set_error(error)
                else:
                    span.record_exception(error)  # CancelledError, KeyboardInterrupt -> cancelled
            except Exception as exc:  # pragma: no cover - defensive
                _internal(exc)
        span.end()

    # ------------------------------------------------------------------ chains / graph nodes

    def on_chain_start(
        self,
        serialized: Mapping[str, Any] | None,
        inputs: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            name = _run_name(kwargs, serialized, "chain")
            metadata = metadata or {}
            is_root = not self._tracked(parent_run_id)
            fields: dict[str, Any] = {}
            if is_root:
                fields["agent"] = {"name": name}
                session = next((metadata[k] for k in _SESSION_KEYS if _is_id(metadata.get(k))), None)
                if session is not None:
                    fields["session_id"] = str(session)
            # Tags are inherited, so only chains honour the hidden tag: model and tool calls inside a
            # hidden chain are still recorded (under the nearest visible span).
            hidden = bool(tags) and _HIDDEN_TAG in (tags or ())
            span = self._start(run_id, parent_run_id, name, "agent" if is_root else "step", hidden=hidden, **fields)
            if span is None or not span.is_recording:
                return
            if not is_root and metadata.get("langgraph_node") == name:
                span.set_attribute("langgraph.node", name)
                if _int_or_none(metadata.get("langgraph_step")) is not None:
                    span.set_attribute("langgraph.step", metadata["langgraph_step"])
            span.set_input(to_jsonable(inputs) if _captures_content() else None)
        except Exception as exc:
            _internal(exc)

    def on_chain_end(self, outputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        try:
            state = self._pop(run_id)
            if state is None or state.span is None:
                return
            if state.span.is_recording:
                state.span.set_output(to_jsonable(outputs) if _captures_content() else None)
            state.span.end()
        except Exception as exc:
            _internal(exc)

    def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        try:
            self._fail(run_id, error)
        except Exception as exc:
            _internal(exc)

    # ------------------------------------------------------------------ models

    def on_chat_model_start(
        self,
        serialized: Mapping[str, Any] | None,
        messages: Sequence[Sequence[Any]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            flat = [m for batch in messages for m in batch]
            self._model_start("chat", flat, run_id, parent_run_id, metadata, kwargs)
        except Exception as exc:
            _internal(exc)

    def on_llm_start(
        self,
        serialized: Mapping[str, Any] | None,
        prompts: Sequence[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            self._model_start("completion", list(prompts), run_id, parent_run_id, metadata, kwargs)
        except Exception as exc:
            _internal(exc)

    def _model_start(
        self,
        operation: str,
        messages: list[Any],
        run_id: UUID,
        parent_run_id: UUID | None,
        metadata: Mapping[str, Any] | None,
        kwargs: Mapping[str, Any],
    ) -> None:
        metadata = metadata or {}
        params = kwargs.get("invocation_params")
        params = params if isinstance(params, Mapping) else {}
        model = (
            _str_or_none(metadata.get("ls_model_name"))
            or _str_or_none(params.get("model"))
            or _str_or_none(params.get("model_name"))
        )
        provider = _str_or_none(metadata.get("ls_provider")) or "unknown"
        name = _str_or_none(kwargs.get("name")) or operation
        span = self._start(
            run_id,
            parent_run_id,
            name,
            "llm",
            llm={"provider": provider, "operation": operation, "request_model": model},
        )
        if span is None or not span.is_recording:
            return
        span.set_llm(
            request={"temperature": metadata.get("ls_temperature"), "max_tokens": metadata.get("ls_max_tokens")}
        )
        captures, fingerprints = _captures_content(), _fingerprints()
        if not (captures or fingerprints):
            span.set_input(None)
            return
        converted = (
            to_otel_messages(messages)
            if operation == "chat"
            else [{"role": "user", "parts": [{"type": "text", "content": p}]} for p in messages]
        )
        if fingerprints:
            request = {k: v for k, v in params.items() if k not in ("stream", "timeout", "request_timeout")}
            span.fingerprint_input({"messages": converted, "invocation_params": to_jsonable(request)})
        span.set_input(converted if captures else None, "messages")

    def on_llm_new_token(self, token: str, *, run_id: UUID, **kwargs: Any) -> None:
        try:
            state = self._get(run_id)
            if state is None:
                return
            state.chunks += 1
            if state.first_token_ms is None:
                state.first_token_ms = (mono_ns() - state.start_mono) / 1_000_000
        except Exception as exc:
            _internal(exc)

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        try:
            state = self._pop(run_id)
            if state is None or state.span is None:
                return
            span = state.span
            if span.is_recording:
                try:
                    _record_llm_result(span, response)
                    if state.chunks:
                        span.set_llm(
                            streaming={
                                "time_to_first_token_ms": state.first_token_ms,
                                "chunk_count": state.chunks,
                                "completed": True,
                            }
                        )
                except Exception as exc:  # pragma: no cover - defensive
                    _internal(exc)
            span.end()
        except Exception as exc:
            _internal(exc)

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        try:
            state = self._get(run_id)
            if state is not None and state.span is not None and state.chunks and state.span.is_recording:
                state.span.set_llm(
                    streaming={
                        "time_to_first_token_ms": state.first_token_ms,
                        "chunk_count": state.chunks,
                        "completed": False,
                    }
                )
            self._fail(run_id, error)
        except Exception as exc:
            _internal(exc)

    # ------------------------------------------------------------------ tools

    def on_tool_start(
        self,
        serialized: Mapping[str, Any] | None,
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
        inputs: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            name = _run_name(kwargs, serialized, "tool")
            call_id = _str_or_none(kwargs.get("tool_call_id"))
            span = self._start(
                run_id, parent_run_id, name, "tool", tool={"name": name, "type": "function", "call_id": call_id}
            )
            if span is None or not span.is_recording:
                return
            arguments = to_jsonable(inputs) if isinstance(inputs, Mapping) else input_str
            span.fingerprint_input(arguments)
            span.set_input(arguments if _captures_content() else None)
        except Exception as exc:
            _internal(exc)

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        try:
            state = self._pop(run_id)
            if state is None or state.span is None:
                return
            span = state.span
            if span.is_recording:
                result = output
                if _is_message(output):  # a ToolMessage when the tool was invoked with a tool call
                    call_id = _str_or_none(getattr(output, "tool_call_id", None))
                    if call_id is not None:
                        span.set_tool(call_id=call_id)
                    result = getattr(output, "content", None)
                    if getattr(output, "status", None) == "error":  # the tool handled its own exception
                        span.set_error(
                            "ToolReturnedError", message=result if isinstance(result, str) else None, origin="tool"
                        )
                span.set_output(to_jsonable(result) if _captures_content() else None)
            span.end()
        except Exception as exc:
            _internal(exc)

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        try:
            self._fail(run_id, error)
        except Exception as exc:
            _internal(exc)

    # ------------------------------------------------------------------ retrievers

    def on_retriever_start(
        self,
        serialized: Mapping[str, Any] | None,
        query: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            span = self._start(run_id, parent_run_id, _run_name(kwargs, serialized, "retriever"), "retrieval")
            if span is None or not span.is_recording:
                return
            span.fingerprint_input(query)
            span.set_input(query if _captures_content() else None)
        except Exception as exc:
            _internal(exc)

    def on_retriever_end(self, documents: Sequence[Any], *, run_id: UUID, **kwargs: Any) -> None:
        try:
            state = self._pop(run_id)
            if state is None or state.span is None:
                return
            span = state.span
            if span.is_recording:
                if isinstance(documents, Sequence):
                    span.set_attribute("retrieval.document_count", len(documents))
                span.set_output(to_jsonable(list(documents)) if _captures_content() else None)
            span.end()
        except Exception as exc:
            _internal(exc)

    def on_retriever_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        try:
            self._fail(run_id, error)
        except Exception as exc:
            _internal(exc)


# ---------------------------------------------------------------------------------- helpers


def _run_name(kwargs: Mapping[str, Any], serialized: Mapping[str, Any] | None, default: str) -> str:
    name = _str_or_none(kwargs.get("name"))
    if name is None and isinstance(serialized, Mapping):
        name = _str_or_none(serialized.get("name"))
        ids = serialized.get("id")
        if name is None and isinstance(ids, list) and ids:
            name = _str_or_none(ids[-1])
    return name or default


def _is_id(value: Any) -> bool:
    return (isinstance(value, str) and bool(value)) or (isinstance(value, int) and not isinstance(value, bool))


def _class_names(value: Any) -> set[str]:
    return {cls.__name__ for cls in type(value).__mro__}


def _is_message(value: Any) -> bool:
    return "BaseMessage" in _class_names(value)


def _bubble_up_status(error: BaseException) -> Status | None:
    """LangGraph raises ``GraphBubbleUp`` subclasses for control flow, not for failures."""
    names = _class_names(error)
    if "GraphBubbleUp" not in names:
        return None
    return "cancelled" if names & {"GraphInterrupt", "GraphDrained"} else "ok"


def to_jsonable(value: Any, _depth: int = 0) -> Any:
    """LangChain values -> JSON-able data: messages in the OTel GenAI shape, documents as
    ``{page_content, metadata}``, pydantic models dumped. Other values are left to the capture
    policy's own conversion."""
    if value is None or isinstance(value, (str, int, float, bool)) or _depth > _MAX_DEPTH:
        return value
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v, _depth + 1) for v in value]
    if _is_message(value):
        return _otel_message(value)
    if "Document" in _class_names(value) and hasattr(value, "page_content"):
        return {
            "page_content": getattr(value, "page_content", None),
            "metadata": to_jsonable(getattr(value, "metadata", None), _depth + 1),
        }
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return to_jsonable(dump(), _depth + 1)
        except Exception:
            return value
    return value


def to_otel_messages(messages: Sequence[Any]) -> list[dict[str, Any]]:
    """LangChain messages (or ``(role, text)`` tuples / plain strings) -> OpenTelemetry GenAI
    ``{role, parts}`` messages. Text, tool calls and tool results are kept; other content blocks
    (images, audio, files, reasoning) are recorded by type only."""
    out: list[dict[str, Any]] = []
    for message in messages:
        if _is_message(message):
            out.append(_otel_message(message))
        elif isinstance(message, str):
            out.append({"role": "user", "parts": [{"type": "text", "content": message}]})
        elif isinstance(message, (list, tuple)) and len(message) == 2 and isinstance(message[1], str):
            out.append({"role": str(message[0]), "parts": [{"type": "text", "content": message[1]}]})
        else:
            out.append({"role": "unknown", "parts": []})
    return out


def _otel_message(message: Any) -> dict[str, Any]:
    kind = str(getattr(message, "type", "unknown"))
    base = kind.removesuffix("MessageChunk").lower() if kind.endswith("MessageChunk") else kind
    role = _ROLES.get(base) or _str_or_none(getattr(message, "role", None)) or base
    content = getattr(message, "content", None)
    parts: list[dict[str, Any]] = []
    if role == "tool":
        parts.append({"type": "tool_call_response", "id": getattr(message, "tool_call_id", None), "response": content})
    elif isinstance(content, str):
        if content:
            parts.append({"type": "text", "content": content})
    elif isinstance(content, (list, tuple)):
        for block in content:
            if isinstance(block, str):
                parts.append({"type": "text", "content": block})
            elif isinstance(block, Mapping) and block.get("type") == "text":
                parts.append({"type": "text", "content": block.get("text")})
            elif isinstance(block, Mapping):
                parts.append({"type": str(block.get("type", "unknown"))})
    for call in getattr(message, "tool_calls", None) or []:
        if isinstance(call, Mapping):
            parts.append(
                {
                    "type": "tool_call",
                    "id": call.get("id"),
                    "name": call.get("name"),
                    "arguments": to_jsonable(call.get("args")),
                }
            )
    return {"role": role, "parts": parts}


def _record_llm_result(span: AnySpan, response: Any) -> None:
    generations = [g for batch in getattr(response, "generations", None) or [] for g in batch]
    llm_output = getattr(response, "llm_output", None)
    llm_output = llm_output if isinstance(llm_output, Mapping) else {}
    reasons: list[str] = []
    response_model = _str_or_none(llm_output.get("model_name"))
    response_id: str | None = None
    usage: Mapping[str, Any] | None = None
    for generation in generations:
        info = getattr(generation, "generation_info", None)
        info = info if isinstance(info, Mapping) else {}
        message = getattr(generation, "message", None)
        meta = getattr(message, "response_metadata", None)
        meta = meta if isinstance(meta, Mapping) else {}
        reason = next(
            (r for r in (info.get("finish_reason"), meta.get("finish_reason"), meta.get("stop_reason")) if r), None
        )
        if isinstance(reason, str):
            reasons.append(reason)
        response_model = response_model or _str_or_none(meta.get("model_name")) or _str_or_none(meta.get("model"))
        response_id = response_id or _str_or_none(meta.get("id"))
        message_usage = getattr(message, "usage_metadata", None)
        if usage is None and isinstance(message_usage, Mapping):
            usage = message_usage  # n>1 choices repeat the call's usage on each message: take it once
    span.set_llm(response_model=response_model, response={"id": response_id, "finish_reasons": reasons})
    if usage is not None:
        _record_usage_metadata(span, usage)
    else:
        _record_token_usage(span, llm_output.get("token_usage") or llm_output.get("usage"))
    if _captures_content():
        span.set_output([_output_message(g) for g in generations], "messages")
    else:
        span.set_output(None)


def _output_message(generation: Any) -> dict[str, Any]:
    message = getattr(generation, "message", None)
    if message is not None and _is_message(message):
        out = _otel_message(message)
    else:
        out = {"role": "assistant", "parts": [{"type": "text", "content": getattr(generation, "text", None)}]}
    info = getattr(generation, "generation_info", None)
    meta = getattr(message, "response_metadata", None)
    for source in (info, meta):
        if isinstance(source, Mapping) and isinstance(source.get("finish_reason"), str):
            out["finish_reason"] = source["finish_reason"]
            break
    return out


def _record_usage_metadata(span: AnySpan, usage: Mapping[str, Any]) -> None:
    """LangChain ``UsageMetadata``: ``input_tokens`` already includes cached tokens, like ours."""
    input_details = usage.get("input_token_details")
    input_details = input_details if isinstance(input_details, Mapping) else {}
    output_details = usage.get("output_token_details")
    output_details = output_details if isinstance(output_details, Mapping) else {}
    span.record_usage(
        input_tokens=_int_or_none(usage.get("input_tokens")),
        output_tokens=_int_or_none(usage.get("output_tokens")),
        total_tokens=_int_or_none(usage.get("total_tokens")),
        cache_read_input_tokens=_int_or_none(input_details.get("cache_read")),
        cache_write_input_tokens=_int_or_none(input_details.get("cache_creation")),
        reasoning_tokens=_int_or_none(output_details.get("reasoning")),
        source="provider_reported",
    )


def _record_token_usage(span: AnySpan, usage: Any) -> None:
    """OpenAI-shaped ``llm_output`` usage only: other shapes may count cached tokens separately."""
    if not isinstance(usage, Mapping) or not ({"prompt_tokens", "completion_tokens"} & usage.keys()):
        return
    span.record_usage(
        input_tokens=_int_or_none(usage.get("prompt_tokens")),
        output_tokens=_int_or_none(usage.get("completion_tokens")),
        total_tokens=_int_or_none(usage.get("total_tokens")),
        source="provider_reported",
    )
