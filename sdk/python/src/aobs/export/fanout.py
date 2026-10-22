"""Fan-out exporter: deliver every batch to several exporters (e.g. the aobs collector *and* an
OpenTelemetry backend). ``aobs.init`` takes one exporter; pass a :class:`FanOutExporter` to use
more than one.

Retries are per destination. When one destination succeeds and another asks for a retry, the
processor retries the whole batch, but this exporter re-sends it only to the destinations that
have not finished it yet (keyed by ``batch_id``), so a backend that is not idempotent never sees
a batch twice because a different backend was down.

The combined result is the most pessimistic one: ``retry`` while any destination still wants a
retry (with the largest ``Retry-After``), then ``failure`` if any destination failed permanently
(the processor then counts the batch as dropped even though other destinations received it — a
failure is never hidden), else ``success``. ``rejected_events`` is the largest per-destination
count. Destinations are called one after another, each with the full ``timeout_s``.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

from .base import EncodedBatch, Exporter, ExportResult

# Batches with at least one destination still retrying. The processor sends one batch at a time,
# so this only needs to cover batches it gave up on before every destination finished.
MAX_TRACKED_BATCHES = 64


class FanOutExporter:
    def __init__(self, *exporters: Exporter) -> None:
        if not exporters:
            raise ValueError("FanOutExporter needs at least one exporter")
        self._exporters = tuple(exporters)
        self._init_state()

    def _init_state(self) -> None:
        self._lock = threading.Lock()
        self._finished: OrderedDict[str, dict[int, ExportResult]] = OrderedDict()

    def _after_fork_in_child(self) -> None:
        self._init_state()
        for exporter in self._exporters:
            hook = getattr(exporter, "_after_fork_in_child", None)
            if callable(hook):
                hook()

    def __repr__(self) -> str:
        return f"FanOutExporter({', '.join(repr(e) for e in self._exporters)})"

    def export(self, batch: EncodedBatch, timeout_s: float) -> ExportResult:
        with self._lock:
            finished = dict(self._finished.get(batch.batch_id, {}))
        retries: list[tuple[int, ExportResult]] = []
        for index, exporter in enumerate(self._exporters):
            if index in finished:
                continue
            try:
                result = exporter.export(batch, timeout_s)
                if not isinstance(result, ExportResult):
                    result = ExportResult("failure", message=f"exporter returned {type(result).__name__}")
            except Exception as exc:  # same contract as the processor: an exception is retryable
                result = ExportResult("retry", message=f"exporter raised {type(exc).__name__}: {exc}"[:200])
            if result.outcome == "retry":
                retries.append((index, result))
            else:
                finished[index] = result
        with self._lock:
            if retries:
                self._finished[batch.batch_id] = finished
                self._finished.move_to_end(batch.batch_id)
                while len(self._finished) > MAX_TRACKED_BATCHES:
                    self._finished.popitem(last=False)
            else:
                self._finished.pop(batch.batch_id, None)
        if retries:
            waits = [r.retry_after_s for _, r in retries if r.retry_after_s is not None]
            return ExportResult("retry", retry_after_s=max(waits) if waits else None, message=self._describe(retries))
        failures = [(i, r) for i, r in sorted(finished.items()) if r.outcome == "failure"]
        if failures:
            return ExportResult("failure", message=self._describe(failures))
        return ExportResult("success", rejected_events=max(r.rejected_events for r in finished.values()))

    def shutdown(self) -> None:
        """Shut every exporter down, even if one raises; the first exception is re-raised."""
        first: Exception | None = None
        for exporter in self._exporters:
            try:
                exporter.shutdown()
            except Exception as exc:
                first = first or exc
        if first is not None:
            raise first

    def _describe(self, results: list[tuple[int, ExportResult]]) -> str:
        parts = [f"{type(self._exporters[i]).__name__}[{i}]: {r.message or r.outcome}" for i, r in results]
        return "; ".join(parts)[:200]
