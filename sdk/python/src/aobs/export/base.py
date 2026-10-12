"""The exporter interface. The SDK is not tied to one transport: anything implementing
:class:`Exporter` can receive batches.

An exporter receives a fully encoded batch — the exact bytes of a wire-schema v1.0 envelope — so
every exporter (HTTP, file, test) sees what the collector would see.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

Outcome = Literal["success", "retry", "failure"]


@dataclass(frozen=True)
class EncodedBatch:
    body: bytes  # UTF-8 JSON envelope (schema v1.0)
    event_count: int
    batch_id: str
    attempt: int  # 1-based; the body is rebuilt per attempt so ``sent_at`` is fresh


@dataclass(frozen=True)
class ExportResult:
    """``retry``: transient (network error, 408/429/5xx) — the processor retries with backoff.
    ``failure``: permanent (e.g. 400/401/403) — the batch is dropped and counted."""

    outcome: Outcome
    retry_after_s: float | None = None
    rejected_events: int = 0  # events the collector accepted the batch but rejected individually
    message: str = ""


SUCCESS = ExportResult("success")


@runtime_checkable
class Exporter(Protocol):
    def export(self, batch: EncodedBatch, timeout_s: float) -> ExportResult:
        """Deliver one batch. May raise; the processor treats an exception as a retryable error."""
        ...

    def shutdown(self) -> None:
        """Release resources. Called once, after the final flush."""
        ...
