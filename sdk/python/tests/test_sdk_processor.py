"""Batching, limits, flushing, coalescing, bounded memory, shutdown and fork safety."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

import aobs
from aobs.export import EncodedBatch, ExportResult, InMemoryExporter, JsonlFileExporter
from sdk_support import check_bodies, end_by_name, ends, flush


class BlockingExporter(InMemoryExporter):
    """Holds every export until released (simulates a hung collector)."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()

    def export(self, batch: EncodedBatch, timeout_s: float) -> ExportResult:
        self.release.wait(30)
        return super().export(batch, timeout_s)


def test_batches_respect_event_limit(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(max_batch_events=7)
    for i in range(30):
        with aobs.span(f"s{i}"):
            pass
    flush()
    sizes = [len(b["events"]) for b in sdk.batches]
    assert max(sizes) <= 7 and sum(sizes) == 30
    check_bodies(sdk.bodies)


def test_batches_respect_byte_limit(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(max_batch_bytes=20_000)
    for i in range(40):
        with aobs.span(f"s{i}", attributes={f"app.k{j}": "v" * 1000 for j in range(3)}):
            pass
    flush()
    assert len(sdk.bodies) > 5
    assert all(len(body) <= 20_000 for body in sdk.bodies)
    assert sum(len(b["events"]) for b in sdk.batches) == 40
    check_bodies(sdk.bodies)


def test_oversized_event_is_degraded_then_dropped(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(max_batch_bytes=16_384, capture="full", max_content_bytes=65_536)
    with aobs.span("big_content") as sp:
        sp.set_output("x" * 60_000)
    flush()
    degraded = end_by_name(sdk, "big_content")
    assert degraded["content"] == {"output": {"status": "not_captured"}}
    assert aobs.diagnostics()["events_degraded_oversize"] == 1
    check_bodies(sdk.bodies)


def test_flush_exports_everything_and_reports_success(sdk: InMemoryExporter) -> None:
    for i in range(1200):
        with aobs.span(f"s{i}"):
            pass
    assert aobs.flush(5) is True
    assert len(ends(sdk)) == 1200
    assert aobs.diagnostics()["queued"] == 0


def test_start_coalescing(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(start_delay_s=10.0)
    with aobs.span("short"):
        pass
    open_span = aobs.span("still_open")
    flush()
    types = sorted((e["name"], e["type"]) for e in sdk.events())
    # The short span's start was replaced by its end; the open span's start is exported by flush.
    assert types == [("short", "span_end"), ("still_open", "span_start")]
    open_span.end()
    flush()
    assert aobs.diagnostics()["starts_coalesced"] == 1


def test_starts_are_exported_after_start_delay(make_sdk: Callable[..., InMemoryExporter]) -> None:
    sdk = make_sdk(start_delay_s=0.05)
    sp = aobs.span("running")
    deadline = time.monotonic() + 2
    while not sdk.bodies and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [e["type"] for e in sdk.events()] == ["span_start"]  # visible while still running
    sp.end()


def test_queue_is_bounded_and_never_blocks(make_sdk: Callable[..., InMemoryExporter]) -> None:
    exporter = BlockingExporter()
    make_sdk(exporter=exporter, max_queue_size=100, max_batch_events=10)
    started = time.perf_counter()
    for i in range(1000):
        with aobs.span(f"s{i}"):
            pass
    elapsed = time.perf_counter() - started
    diag = aobs.diagnostics()
    assert diag["queued"] <= 100
    assert diag["events_dropped_queue_full"] >= 1000 - 100 - 10  # worker holds ≤ 1 batch in flight
    assert elapsed < 1.0  # producers were never blocked by the hung exporter
    exporter.release.set()
    flush()


def test_flush_times_out_with_hung_exporter_and_returns_false(make_sdk: Callable[..., InMemoryExporter]) -> None:
    exporter = BlockingExporter()
    make_sdk(exporter=exporter)
    with aobs.span("s"):
        pass
    started = time.perf_counter()
    assert aobs.flush(0.2) is False
    assert time.perf_counter() - started < 1.0
    exporter.release.set()
    assert aobs.flush(5) is True


def test_shutdown_is_bounded_with_hung_exporter(make_sdk: Callable[..., InMemoryExporter]) -> None:
    exporter = BlockingExporter()
    make_sdk(exporter=exporter)
    for i in range(5):
        with aobs.span(f"s{i}"):
            pass
    time.sleep(0.05)  # let the worker pick up the first batch and block
    with aobs.span("late"):
        pass
    started = time.perf_counter()
    assert aobs.shutdown(timeout_s=0.3) is False
    assert time.perf_counter() - started < 2.0
    exporter.release.set()


def test_spans_after_shutdown_are_dropped_and_counted(sdk: InMemoryExporter) -> None:
    tracer = aobs.get_tracer()
    assert tracer is not None
    aobs.shutdown()
    with aobs.span("after"):  # SDK is shut down: a non-recording span
        pass
    tracer.processor.on_end(("a" * 32, "b" * 16), {"type": "span_end"})  # direct late enqueue
    assert tracer.diag.snapshot()["events_dropped_after_shutdown"] == 1
    assert sdk.shut_down


def test_failing_exporter_is_retried_then_dropped(make_sdk: Callable[..., InMemoryExporter]) -> None:
    attempts: list[int] = []

    class Flaky(InMemoryExporter):
        def export(self, batch: EncodedBatch, timeout_s: float) -> ExportResult:
            attempts.append(batch.attempt)
            raise ConnectionError("collector down")

    make_sdk(exporter=Flaky(), max_export_attempts=3)
    with aobs.span("s"):
        pass
    aobs.flush(5)
    diag = aobs.diagnostics()
    assert attempts == [1, 2, 3]
    assert diag["export_retries"] == 2 and diag["events_dropped_export_failed"] == 1
    assert "collector down" in diag["last_error"]


def test_permanent_failure_is_not_retried(make_sdk: Callable[..., InMemoryExporter]) -> None:
    calls: list[int] = []

    class Rejecting(InMemoryExporter):
        def export(self, batch: EncodedBatch, timeout_s: float) -> ExportResult:
            calls.append(1)
            return ExportResult("failure", message="HTTP 401")

    make_sdk(exporter=Rejecting(), max_export_attempts=5)
    with aobs.span("s"):
        pass
    aobs.flush(5)
    assert calls == [1]
    assert aobs.diagnostics()["batches_failed"] == 1


def test_retried_batches_keep_their_id_but_refresh_sent_at(make_sdk: Callable[..., InMemoryExporter]) -> None:
    seen: list[tuple[str, str]] = []

    class FailOnce(InMemoryExporter):
        def export(self, batch: EncodedBatch, timeout_s: float) -> ExportResult:
            body = json.loads(batch.body)
            seen.append((body["batch_id"], body["sent_at"]))
            if batch.attempt == 1:
                time.sleep(0.002)
                return ExportResult("retry")
            return super().export(batch, timeout_s)

    make_sdk(exporter=FailOnce())
    with aobs.span("s"):
        pass
    flush()
    (id1, sent1), (id2, sent2) = seen
    assert id1 == id2 and sent1 < sent2


def test_jsonl_file_exporter_writes_private_valid_batches(
    make_sdk: Callable[..., InMemoryExporter], tmp_path: Path
) -> None:
    path = tmp_path / "out.jsonl"
    make_sdk(exporter=JsonlFileExporter(path))
    with aobs.run("answer"), aobs.span("child"):
        pass
    flush()
    lines = path.read_bytes().splitlines()
    check_bodies(lines)
    assert oct(path.stat().st_mode & 0o777) == "0o600"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork not available")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")  # "multi-threaded process ... fork()"
def test_fork_child_exports_its_own_events_only(make_sdk: Callable[..., InMemoryExporter], tmp_path: Path) -> None:
    path = tmp_path / "fork.jsonl"
    exporter = BlockingExporter()  # parent's events stay queued at fork time
    make_sdk(exporter=JsonlFileExporter(path), start_delay_s=10)
    tracer = aobs.get_tracer()
    assert tracer is not None
    tracer.processor._exporter = exporter  # parent exports block, so its queue is non-empty at fork
    with aobs.span("parent_span"):
        pass
    time.sleep(0.05)
    with aobs.span("parent_queued"):
        pass
    pid = os.fork()
    if pid == 0:  # child
        code = 1
        try:
            tracer.processor._exporter = JsonlFileExporter(path)
            with aobs.span("child_span"):
                pass
            code = 0 if aobs.flush(5) else 2
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    assert os.WEXITSTATUS(status) == 0
    names = [e["name"] for line in path.read_bytes().splitlines() for e in json.loads(line)["events"]]
    assert names == ["child_span"]  # the parent's queued events were not duplicated by the child
    exporter.release.set()


def test_worker_survives_exporter_returning_garbage(make_sdk: Callable[..., InMemoryExporter]) -> None:
    class Garbage(InMemoryExporter):
        def export(self, batch: EncodedBatch, timeout_s: float) -> ExportResult:
            return "ok"  # type: ignore[return-value]

    make_sdk(exporter=Garbage())
    with aobs.span("s"):
        pass
    aobs.flush(2)
    assert aobs.diagnostics()["batches_failed"] == 1
    with aobs.span("s2"):
        pass
    aobs.flush(2)
    assert aobs.diagnostics()["batches_failed"] == 2  # still processing


def test_python_version_supported() -> None:
    assert sys.version_info >= (3, 11)
