"""JSON-lines file exporter: one batch envelope per line. Useful offline and before a collector
exists. The file is created with mode 0600 because it may contain captured content."""

from __future__ import annotations

import os
import threading
from pathlib import Path

from .base import SUCCESS, EncodedBatch, ExportResult


class JsonlFileExporter:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def export(self, batch: EncodedBatch, timeout_s: float) -> ExportResult:
        with self._lock:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                data = memoryview(batch.body + b"\n")
                while data:
                    data = data[os.write(fd, data) :]
            finally:
                os.close(fd)
        return SUCCESS

    def shutdown(self) -> None:
        pass

    def _after_fork_in_child(self) -> None:
        self._lock = threading.Lock()
