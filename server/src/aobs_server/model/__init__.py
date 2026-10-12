"""Canonical event model (pure: no I/O). Shared by ingest (merge) and query (assembly).

Typical use::

    result = validate_batch(raw_bytes)            # strict parse + schema/version policy
    traces = assemble_events(result.accepted, now) # merge duplicates, build trees, derive state
"""

from .assembly import AssemblyConfig, SpanNode, TokenTotal, TraceView, assemble, assemble_events
from .merge import MergeResult, SpanEventRecord, SpanRecord, merge_events
from .parsing import StrictJSONError, loads_strict
from .versioning import (
    KNOWN_MINOR,
    SUPPORTED_MAJOR,
    AcceptedEvent,
    BatchResult,
    FieldError,
    RejectedEvent,
    validate_batch,
)

__all__ = [
    "KNOWN_MINOR",
    "SUPPORTED_MAJOR",
    "AcceptedEvent",
    "AssemblyConfig",
    "BatchResult",
    "FieldError",
    "MergeResult",
    "RejectedEvent",
    "SpanEventRecord",
    "SpanNode",
    "SpanRecord",
    "StrictJSONError",
    "TokenTotal",
    "TraceView",
    "assemble",
    "assemble_events",
    "loads_strict",
    "merge_events",
    "validate_batch",
]
