"""aobs — vendor-neutral instrumentation for AI agents.

Quick start::

    import aobs
    aobs.init(service_name="support-agent", endpoint="http://localhost:8000")   # or exporter=...

    @aobs.tool
    def get_order_status(order_id: str) -> dict: ...

    with aobs.run("answer_ticket", session_id=ticket_id):
        ...  # model calls (see aobs.integrations.openai_compat) and tool calls nest under the run

See docs/sdk.md for configuration, privacy controls and guarantees.
"""

from ._api import (
    agent,
    agent_span,
    current_span,
    diagnostics,
    event,
    flush,
    get_tracer,
    init,
    llm_span,
    run,
    shutdown,
    span,
    tool,
    tool_span,
    with_current_context,
)
from ._tracer import NonRecordingSpan, Span
from ._version import __version__
from .config import ConfigError

__all__ = [
    "ConfigError",
    "NonRecordingSpan",
    "Span",
    "__version__",
    "agent",
    "agent_span",
    "current_span",
    "diagnostics",
    "event",
    "flush",
    "get_tracer",
    "init",
    "llm_span",
    "run",
    "shutdown",
    "span",
    "tool",
    "tool_span",
    "with_current_context",
]
