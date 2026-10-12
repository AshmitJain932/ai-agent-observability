"""In-memory exporter, for tests and local inspection."""

from __future__ import annotations

import json
import threading
from typing import Any

from .base import SUCCESS, EncodedBatch, ExportResult


class InMemoryExporter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bodies: list[bytes] = []
        self.shut_down = False

    def export(self, batch: EncodedBatch, timeout_s: float) -> ExportResult:
        with self._lock:
            self._bodies.append(batch.body)
        return SUCCESS

    def shutdown(self) -> None:
        self.shut_down = True

    def _after_fork_in_child(self) -> None:
        self._lock = threading.Lock()
        self._bodies = []

    @property
    def bodies(self) -> list[bytes]:
        with self._lock:
            return list(self._bodies)

    @property
    def batches(self) -> list[dict[str, Any]]:
        return [json.loads(body) for body in self.bodies]

    def events(self) -> list[dict[str, Any]]:
        return [event for batch in self.batches for event in batch["events"]]

    def clear(self) -> None:
        with self._lock:
            self._bodies.clear()
