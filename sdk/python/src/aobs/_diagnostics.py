"""Self-diagnostics: counters for everything the SDK drops, degrades or fails to send.

The SDK never raises into the host application after ``init()``; instead every swallowed problem
increments a counter here and (rate-limited) logs a warning on the ``aobs`` logger. Read with
``aobs.diagnostics()``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter
from typing import Any

logger = logging.getLogger("aobs")

# Counter names (documented in docs/sdk.md).
COUNTERS = (
    "spans_started",
    "spans_ended",
    "span_double_end",
    "events_enqueued",
    "starts_coalesced",
    "events_exported",
    "batches_exported",
    "export_attempts",
    "export_retries",
    "batches_failed",
    "events_dropped_queue_full",
    "events_dropped_after_shutdown",
    "events_dropped_export_failed",
    "events_dropped_oversize",
    "events_degraded_oversize",
    "events_rejected_by_collector",
    "attributes_dropped",
    "values_sanitized",
    "invalid_usage_values",
    "invalid_cost_values",
    "content_truncated",
    "uninstrumented_calls",
    "internal_errors",
    "config_warnings",
)

WARN_INTERVAL_S = 60.0


class Diagnostics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()
        self._last_error: str | None = None
        self._last_error_at: float | None = None
        self._last_warned: dict[str, float] = {}
        self._pid = os.getpid()

    def after_fork_in_child(self) -> None:
        self._lock = threading.Lock()
        self._pid = os.getpid()

    def inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counts[name] += amount

    def error(self, category: str, message: str, *, count: str | None = None, amount: int = 1) -> None:
        """Record a problem: bump ``count`` (if given), remember it, and warn at most once a minute."""
        now = time.time()
        with self._lock:
            if count:
                self._counts[count] += amount
            self._last_error = f"{category}: {message}"[:500]
            self._last_error_at = now
            last = self._last_warned.get(category, 0.0)
            should_warn = now - last >= WARN_INTERVAL_S
            if should_warn:
                self._last_warned[category] = now
        if should_warn:
            logger.warning(
                "aobs %s: %s (further messages suppressed for %ds; see aobs.diagnostics())",
                category,
                message,
                int(WARN_INTERVAL_S),
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            out: dict[str, Any] = {name: self._counts.get(name, 0) for name in COUNTERS}
            for name, value in self._counts.items():  # any counter not in the documented list
                out.setdefault(name, value)
            out["last_error"] = self._last_error
            out["last_error_at"] = self._last_error_at
        return out
