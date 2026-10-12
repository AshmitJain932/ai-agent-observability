"""Background batching, encoding and delivery.

Guarantees (each is tested in ``test_sdk_processor.py`` / ``test_sdk_http.py``):

* **Bounded memory.** At most ``max_queue_size`` events are held (pending starts included). When
  full, new events are dropped and counted; the application is never blocked.
* **Start coalescing.** A ``span_start`` waits ``start_delay_s`` before it is eligible for export.
  If the span ends first, the start is discarded and only the self-contained ``span_end`` is sent.
* **Schema-sized batches.** ≤ ``max_batch_events`` events and ≤ ``max_batch_bytes`` bytes per
  request. An event too large on its own is degraded (content and attributes removed, content
  marked ``not_captured``) and, if still too large, dropped and counted.
* **Bounded retries.** Retryable failures are retried up to ``max_export_attempts`` times with
  jittered exponential backoff (``Retry-After`` honoured, capped); then the batch is dropped and
  counted. Retries are safe because ingest is idempotent.
* **Flush and shutdown are bounded** by their timeouts. ``flush`` returns False if it timed out.
* **Fork-safe.** In a forked child the queue is emptied (those events belong to the parent), locks
  are recreated and the worker thread restarts lazily.

Encoding (JSON) happens on the worker thread, not in the application's thread.
"""

from __future__ import annotations

import json
import os
import queue
import random
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable
from typing import Any

from .. import _sanitize as S
from .._clock import format_ts
from .._diagnostics import Diagnostics
from .._ids import uuid7
from .._version import SCHEMA_VERSION
from .base import EncodedBatch, Exporter, ExportResult

Event = dict[str, Any]
SpanKey = tuple[str, str]


class BatchProcessor:
    def __init__(
        self,
        exporter: Exporter,
        resource: Callable[[], bytes],
        diag: Diagnostics,
        *,
        max_queue_size: int,
        max_batch_events: int,
        max_batch_bytes: int,
        schedule_delay_s: float,
        start_delay_s: float,
        export_timeout_s: float,
        max_export_attempts: int,
        retry_backoff_initial_s: float,
        retry_backoff_max_s: float,
    ) -> None:
        self._exporter = exporter
        self._resource = resource
        self._diag = diag
        self._max_queue = max_queue_size
        self._max_events = max_batch_events
        self._max_bytes = max_batch_bytes
        self._delay = schedule_delay_s
        self._start_delay = start_delay_s
        self._export_timeout = export_timeout_s
        self._max_attempts = max_export_attempts
        self._backoff_initial = retry_backoff_initial_s
        self._backoff_max = retry_backoff_max_s
        self._init_state()
        self._shutdown = False
        self._deadline: float | None = None  # monotonic; set by shutdown()
        ref = weakref.ref(self)

        def _child_hook() -> None:  # weak reference: the hook must not keep the processor alive
            processor = ref()
            if processor is not None:
                processor._after_fork_in_child()

        os.register_at_fork(after_in_child=_child_hook)

    def _init_state(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._abort = threading.Event()
        self._events: deque[Event] = deque()
        self._starts: dict[SpanKey, tuple[float, Event]] = {}
        # Callbacks queued from finalizers (__del__). SimpleQueue.put is reentrant-safe, so a
        # finalizer never touches our locks; the worker runs the callbacks from a safe context.
        self._deferred: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._flush_requested = 0
        self._flush_completed = 0
        self._thread: threading.Thread | None = None
        self._pid = os.getpid()

    def _after_fork_in_child(self) -> None:
        self._init_state()
        self._diag.after_fork_in_child()
        hook = getattr(self._exporter, "_after_fork_in_child", None)
        if callable(hook):
            hook()

    # ------------------------------------------------------------------ producer side (app threads)

    def _admit_locked(self) -> bool:
        if self._shutdown:
            self._diag.inc("events_dropped_after_shutdown")
            return False
        if len(self._events) + len(self._starts) >= self._max_queue:
            self._diag.error("queue_full", "event queue full; dropping new events", count="events_dropped_queue_full")
            return False
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="aobs-exporter", daemon=True)
            self._thread.start()
        return True

    def on_start(self, key: SpanKey, event: Event) -> None:
        with self._lock:
            if self._admit_locked():
                self._starts[key] = (time.monotonic(), event)
                self._diag.inc("events_enqueued")

    def on_end(self, key: SpanKey, event: Event) -> None:
        with self._lock:
            if self._starts.pop(key, None) is not None:
                self._diag.inc("starts_coalesced")
            if self._admit_locked():
                self._events.append(event)
                self._diag.inc("events_enqueued")
                if len(self._events) >= self._max_events:
                    self._cond.notify()

    def on_event(self, event: Event) -> None:
        with self._lock:
            if self._admit_locked():
                self._events.append(event)
                self._diag.inc("events_enqueued")
                if len(self._events) >= self._max_events:
                    self._cond.notify()

    def defer(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` later on the worker thread. Safe to call from ``__del__``."""
        self._deferred.put(callback)

    def _run_deferred(self) -> None:
        while True:
            try:
                callback = self._deferred.get_nowait()
            except queue.Empty:
                return
            try:
                callback()
            except Exception as exc:
                self._diag.error("deferred", repr(exc)[:200], count="internal_errors")

    def queued(self) -> int:
        with self._lock:
            return len(self._events) + len(self._starts)

    # ------------------------------------------------------------------ control

    def flush(self, timeout_s: float) -> bool:
        with self._cond:
            if self._thread is None and not self._events and not self._starts:
                return True
            self._flush_requested += 1
            target = self._flush_requested
            self._cond.notify_all()
            end = time.monotonic() + max(timeout_s, 0.0)
            while self._flush_completed < target:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    def shutdown(self, timeout_s: float) -> bool:
        """Export what is queued (within ``timeout_s``), stop the worker, shut the exporter down."""
        with self._cond:
            if self._shutdown:
                return True
            self._shutdown = True
            self._deadline = time.monotonic() + max(timeout_s, 0.0)
            thread = self._thread
            self._cond.notify_all()
        finished = True
        if thread is not None:
            thread.join(max(timeout_s, 0.0))
            if thread.is_alive():
                finished = False
                self._abort.set()  # stop any backoff sleep; the daemon thread exits soon after
                thread.join(0.5)
                with self._lock:
                    lost = len(self._events) + len(self._starts)
                if lost:
                    self._diag.error(
                        "shutdown_timeout",
                        f"{lost} events not exported before shutdown timeout",
                        count="events_dropped_after_shutdown",
                        amount=lost,
                    )
        try:
            self._exporter.shutdown()
        except Exception as exc:
            self._diag.error("exporter_shutdown", repr(exc)[:200], count="internal_errors")
        return finished

    # ------------------------------------------------------------------ worker

    def _take_locked(self, everything: bool) -> list[Event]:
        batch: list[Event] = []
        now = time.monotonic()
        for key in list(self._starts):
            if len(batch) >= self._max_events:
                break
            queued_at, event = self._starts[key]
            if everything or now - queued_at >= self._start_delay:
                del self._starts[key]
                batch.append(event)
        while self._events and len(batch) < self._max_events:
            batch.append(self._events.popleft())
        return batch

    def _run(self) -> None:
        while not self._abort.is_set():
            try:
                if self._run_once():
                    return
            except Exception as exc:  # never let the worker die silently
                self._diag.error("worker", repr(exc)[:200], count="internal_errors")
                time.sleep(0.1)
        # Aborted by a timed-out shutdown(): what is still queued was counted there.

    def _run_once(self) -> bool:
        """One cycle: wait, take a batch, export it. Returns True when the worker should exit."""
        self._run_deferred()
        with self._cond:
            flushing = self._shutdown or self._flush_requested > self._flush_completed
            if not flushing and len(self._events) < self._max_events:
                self._cond.wait(self._delay)
                flushing = self._shutdown or self._flush_requested > self._flush_completed
            target = self._flush_requested
        if flushing:
            self._run_deferred()  # ends recorded by finalizers during the wait belong in this flush
        with self._cond:
            batch = self._take_locked(everything=flushing)
        if batch:
            self._export(batch)
        with self._cond:
            empty = not self._events and not self._starts
            if flushing and empty:
                self._flush_completed = max(self._flush_completed, target)
                self._cond.notify_all()
            if self._shutdown and empty:
                self._thread = None
                self._cond.notify_all()
                return True
        return False

    # ------------------------------------------------------------------ encoding

    def _head(self, batch_id: str) -> bytes:
        return (
            b'{"schema_version":"'
            + SCHEMA_VERSION.encode()
            + b'","batch_id":"'
            + batch_id.encode()
            + b'","sent_at":"'
            + format_ts(time.time_ns()).encode()
            + b'","resource":'
            + self._resource()
            + b',"events":['
        )

    def _encode(self, event: Event, budget: int) -> bytes | None:
        data = _dumps(event, self._diag)
        if data is not None and len(data) <= budget:
            return data
        degraded = _degrade(event)
        data = _dumps(degraded, self._diag)
        if data is not None and len(data) <= budget:
            self._diag.error(
                "oversize_event", "event too large; content and attributes removed", count="events_degraded_oversize"
            )
            return data
        self._diag.error(
            "oversize_event", "event too large even without content; dropped", count="events_dropped_oversize"
        )
        return None

    def _export(self, events: list[Event]) -> None:
        overhead = len(self._head(uuid7())) + 2  # "]}" closing; sent_at has fixed width
        budget = self._max_bytes - overhead
        chunk: list[bytes] = []
        size = 0
        for event in events:
            data = self._encode(event, budget)
            if data is None:
                continue
            extra = len(data) + (1 if chunk else 0)
            if chunk and size + extra > budget:
                self._send(chunk)
                chunk, size, extra = [], 0, len(data)
            chunk.append(data)
            size += extra
        if chunk:
            self._send(chunk)

    def _send(self, chunk: list[bytes]) -> None:
        batch_id = uuid7()
        attempt = 0
        while True:
            attempt += 1
            if self._abort.is_set() or (self._deadline is not None and time.monotonic() >= self._deadline):
                self._diag.inc("batches_failed")
                self._diag.error(
                    "export_failed",
                    f"dropping batch of {len(chunk)} events: shutdown deadline reached",
                    count="events_dropped_export_failed",
                    amount=len(chunk),
                )
                return
            body = self._head(batch_id) + b",".join(chunk) + b"]}"
            self._diag.inc("export_attempts")
            try:
                result = self._exporter.export(EncodedBatch(body, len(chunk), batch_id, attempt), self._export_timeout)
                if not isinstance(result, ExportResult):
                    result = ExportResult("failure", message=f"exporter returned {type(result).__name__}")
            except Exception as exc:
                result = ExportResult("retry", message=f"exporter raised {type(exc).__name__}: {exc}"[:200])
            if result.outcome == "success":
                self._diag.inc("batches_exported")
                self._diag.inc("events_exported", len(chunk))
                if result.rejected_events:
                    self._diag.error(
                        "collector_rejected",
                        f"{result.rejected_events} events rejected by collector",
                        count="events_rejected_by_collector",
                        amount=result.rejected_events,
                    )
                return
            if result.outcome == "failure" or attempt >= self._max_attempts:
                self._diag.inc("batches_failed")
                self._diag.error(
                    "export_failed",
                    f"dropping batch of {len(chunk)} events after {attempt} attempt(s): {result.message}",
                    count="events_dropped_export_failed",
                    amount=len(chunk),
                )
                return
            delay = min(self._backoff_max, self._backoff_initial * 2 ** (attempt - 1)) * random.uniform(0.5, 1.0)
            if result.retry_after_s is not None:
                delay = max(delay, min(result.retry_after_s, self._backoff_max))
            if self._deadline is not None:
                delay = min(delay, max(self._deadline - time.monotonic(), 0.0))
            self._diag.inc("export_retries")
            self._abort.wait(delay)  # returns early on abort; the check at the loop top then drops the batch


def _dumps(event: Event, diag: Diagnostics) -> bytes | None:
    try:
        return json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except UnicodeEncodeError:
        diag.inc("values_sanitized")
        return _dumps(_clean_strings(event), diag)
    except (TypeError, ValueError) as exc:
        diag.error("encode", f"event not encodable: {exc}"[:200], count="internal_errors")
        return None


def _clean_strings(value: Any) -> Any:
    if isinstance(value, str):
        return S.clean_text(value)
    if isinstance(value, dict):
        return {S.clean_text(k): _clean_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean_strings(v) for v in value]
    return value


def _degrade(event: Event) -> Event:
    out = {k: v for k, v in event.items() if k != "attributes"}
    content = out.get("content")
    if isinstance(content, dict):
        out["content"] = {direction: {"status": "not_captured"} for direction in content}
    error = out.get("error")
    if isinstance(error, dict) and "stack" in error:
        out["error"] = {k: v for k, v in error.items() if k != "stack"}
    return out
