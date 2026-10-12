"""Public module-level API (re-exported from ``aobs``)."""

from __future__ import annotations

import atexit
import contextvars
import functools
import inspect
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar, overload

from ._diagnostics import Diagnostics, logger
from ._tracer import NON_RECORDING, NonRecordingSpan, Span, Tracer, current_recording_span
from .config import Config, ConfigError, build_config
from .export.base import Exporter
from .export.http import HttpExporter

F = TypeVar("F", bound=Callable[..., Any])
AnySpan = Span | NonRecordingSpan

_lock = threading.Lock()
_tracer: Tracer | None = None
_diag = Diagnostics()
_disabled_reason: str | None = "not initialized"
_atexit_registered = False


def init(*, exporter: Exporter | None = None, **options: Any) -> None:
    """Configure the SDK. See docs/sdk.md for every option and its environment variable.

    Calling ``init`` again shuts the previous configuration down (flushing it) and replaces it.
    Never raises for configuration problems unless ``strict=True``.
    """
    global _tracer, _diag, _disabled_reason, _atexit_registered
    with _lock:
        previous = _tracer
        _tracer = None
        if previous is not None:
            previous.processor.shutdown(previous.config.shutdown_timeout_s)
        diag = Diagnostics()
        strict = bool(options.get("strict", False))
        config: Config = build_config(diag, **options)
        _diag = diag
        if not config.enabled:
            _disabled_reason = "disabled by configuration (enabled=False or AOBS_ENABLED)"
            return
        if exporter is None and config.endpoint:
            exporter = HttpExporter(config.endpoint, config.api_key, config.headers)
        if exporter is None:
            message = "no exporter or endpoint configured (set AOBS_ENDPOINT); telemetry disabled"
            if strict:
                raise ConfigError(message)
            diag.inc("config_warnings")
            logger.warning("aobs: %s", message)
            _disabled_reason = message
            return
        _tracer = Tracer(config, exporter, diag)
        _disabled_reason = None
        if not _atexit_registered:
            atexit.register(_atexit_shutdown)
            _atexit_registered = True


def _atexit_shutdown() -> None:
    tracer = _tracer
    if tracer is not None:
        tracer.processor.shutdown(tracer.config.shutdown_timeout_s)


def shutdown(timeout_s: float | None = None) -> bool:
    """Flush queued events (bounded by ``timeout_s``), stop the exporter thread. Returns False if
    the timeout expired with events still unsent. Spans recorded afterwards are dropped."""
    global _tracer, _disabled_reason
    with _lock:
        tracer = _tracer
        _tracer = None
        _disabled_reason = "shut down"
    if tracer is None:
        return True
    return tracer.processor.shutdown(tracer.config.shutdown_timeout_s if timeout_s is None else timeout_s)


def flush(timeout_s: float = 5.0) -> bool:
    """Export everything queued, including starts of spans still open. Returns False on timeout
    (e.g. collector unreachable); never raises."""
    tracer = _tracer
    if tracer is None:
        return True
    return tracer.processor.flush(timeout_s)


def diagnostics() -> dict[str, Any]:
    """Counters for dropped/degraded/failed telemetry plus current state."""
    snapshot = _diag.snapshot()
    tracer = _tracer
    snapshot["enabled"] = tracer is not None
    snapshot["disabled_reason"] = _disabled_reason
    snapshot["queued"] = tracer.processor.queued() if tracer is not None else 0
    if tracer is not None:
        snapshot["capture"] = tracer.policy.mode
        snapshot["fingerprint_key_id"] = tracer.fingerprinter.key_id if tracer.fingerprinter else None
    return snapshot


def get_tracer() -> Tracer | None:
    return _tracer


# ---------------------------------------------------------------------------------- spans


def current_span() -> AnySpan:
    return current_recording_span() or NON_RECORDING


def _start(name: str, kind: str, *, new_trace: bool = False, parent: Span | None = None, **kwargs: Any) -> AnySpan:
    tracer = _tracer
    if tracer is None:
        return NON_RECORDING
    try:
        return tracer.start_span(
            name, kind, parent=parent if parent is not None else current_recording_span(), new_trace=new_trace, **kwargs
        )
    except Exception as exc:  # never break the application
        tracer.diag.error("start_span", repr(exc)[:200], count="internal_errors")
        return NON_RECORDING


def span(
    name: str,
    *,
    kind: str = "step",
    attributes: Mapping[str, Any] | None = None,
    links: Sequence[Mapping[str, str]] | None = None,
    parent: Span | None = None,
) -> AnySpan:
    """A generic span, child of the current span (or of ``parent``); a new trace if there is none."""
    return _start(name, kind, attributes=attributes, links=links, parent=parent)


def run(
    name: str,
    *,
    agent: str | None = None,
    agent_version: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> AnySpan:
    """One agent run = one trace. Always starts a **new trace**; if a span is active, the run is
    linked to it with relation ``triggered_by``."""
    return _start(
        name,
        "agent",
        new_trace=True,
        session_id=session_id,
        attributes=attributes,
        agent={"name": agent or name, "version": agent_version, "id": agent_id},
    )


def agent_span(
    name: str,
    *,
    version: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> AnySpan:
    """A sub-agent invocation inside the current trace (kind ``agent``)."""
    return _start(
        name,
        "agent",
        session_id=session_id,
        attributes=attributes,
        agent={"name": name, "version": version, "id": agent_id},
    )


def tool_span(
    name: str, *, tool_type: str = "function", call_id: str | None = None, attributes: Mapping[str, Any] | None = None
) -> AnySpan:
    """A tool call (kind ``tool``). Use ``set_input``/``set_output``/``fingerprint_input`` on it."""
    return _start(name, "tool", tool={"name": name, "type": tool_type, "call_id": call_id}, attributes=attributes)


def llm_span(
    *,
    provider: str,
    request_model: str | None = None,
    operation: str = "chat",
    name: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> AnySpan:
    """A model call for providers without an integration. Report usage only if the provider
    returned it (``record_usage``); otherwise leave it unknown."""
    return _start(
        name or operation,
        "llm",
        attributes=attributes,
        llm={"provider": provider, "operation": operation, "request_model": request_model},
    )


def event(name: str, attributes: Mapping[str, Any] | None = None) -> None:
    """Add a point-in-time event to the current span (no-op if there is none)."""
    current = current_recording_span()
    if current is not None:
        current.event(name, attributes)


def with_current_context(fn: F) -> F:
    """Bind ``fn`` to the current context so spans it opens in another thread (thread pools,
    ``threading.Thread``) are children of the current span. ``asyncio`` tasks need no help."""
    ctx = contextvars.copy_context()

    @functools.wraps(fn)
    def bound(*args: Any, **kwargs: Any) -> Any:
        return ctx.copy().run(fn, *args, **kwargs)

    return bound  # type: ignore[return-value]


# ---------------------------------------------------------------------------------- decorators


def _bound_arguments(sig: inspect.Signature | None, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    if sig is None:
        return {"args": list(args), "kwargs": kwargs}
    try:
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
    except TypeError:
        return {"args": list(args), "kwargs": kwargs}
    arguments = dict(bound.arguments)
    params = list(sig.parameters)
    if params and params[0] in ("self", "cls"):
        arguments.pop(params[0], None)  # instance identity is not part of the input
    return arguments


def _record_call_input(
    sp: AnySpan, sig: inspect.Signature | None, args: tuple[Any, ...], kwargs: dict[str, Any], capture: bool
) -> None:
    if not sp.is_recording:
        return
    try:
        arguments = _bound_arguments(sig, args, kwargs)
        sp.fingerprint_input(arguments)
        if capture:
            sp.set_input(arguments, "json")
    except Exception as exc:  # pragma: no cover - defensive
        tracer = _tracer
        if tracer is not None:
            tracer.diag.error("decorator_input", repr(exc)[:200], count="internal_errors")


def _decorate(fn: F, start: Callable[[], AnySpan], capture: bool) -> F:
    if inspect.isgeneratorfunction(fn) or inspect.isasyncgenfunction(fn):
        # A span would end when the generator object is created, not when it is consumed.
        # Not instrumented in this version; calls are counted so the gap is visible.
        @functools.wraps(fn)
        def passthrough(*args: Any, **kwargs: Any) -> Any:
            tracer = _tracer
            if tracer is not None:
                tracer.diag.inc("uninstrumented_calls")
            return fn(*args, **kwargs)

        return passthrough  # type: ignore[return-value]

    try:
        sig: inspect.Signature | None = inspect.signature(fn)
    except (TypeError, ValueError):
        sig = None

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            sp = start()
            with sp:
                _record_call_input(sp, sig, args, kwargs, capture)
                result = await fn(*args, **kwargs)
                if capture and sp.is_recording:
                    sp.set_output(result)
                return result

        return async_wrapper  # type: ignore[return-value]

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        sp = start()
        with sp:
            _record_call_input(sp, sig, args, kwargs, capture)
            result = fn(*args, **kwargs)
            if capture and sp.is_recording:
                sp.set_output(result)
            return result

    return wrapper  # type: ignore[return-value]


@overload
def tool(fn: F, /) -> F: ...
@overload
def tool(*, name: str | None = None, tool_type: str = "function", capture: bool = True) -> Callable[[F], F]: ...


def tool(fn: Any = None, /, *, name: str | None = None, tool_type: str = "function", capture: bool = True) -> Any:
    """Decorate a tool function (sync or async). Each call becomes a ``tool`` span with an input
    fingerprint; arguments and return value are recorded as content subject to the capture
    policy (``capture=False`` never records them). Usable as ``@aobs.tool`` or ``@aobs.tool(...)``."""

    def decorator(func: F) -> F:
        tool_name = name or func.__name__
        return _decorate(func, lambda: tool_span(tool_name, tool_type=tool_type), capture)

    return decorator(fn) if callable(fn) else decorator


@overload
def agent(fn: F, /) -> F: ...
@overload
def agent(*, name: str | None = None, version: str | None = None, capture: bool = True) -> Callable[[F], F]: ...


def agent(fn: Any = None, /, *, name: str | None = None, version: str | None = None, capture: bool = True) -> Any:
    """Decorate a sub-agent function: each call becomes an ``agent`` span in the current trace
    (a new trace if none is active)."""

    def decorator(func: F) -> F:
        agent_name = name or func.__name__
        return _decorate(func, lambda: agent_span(agent_name, version=version), capture)

    return decorator(fn) if callable(fn) else decorator
