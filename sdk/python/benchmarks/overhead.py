"""Reproducible instrumentation-overhead benchmark for the aobs Python SDK.

    uv run python sdk/python/benchmarks/overhead.py                 # full run (~1 min)
    uv run python sdk/python/benchmarks/overhead.py --quick         # smoke run (~5 s)
    uv run python sdk/python/benchmarks/overhead.py --json out.json

Method
------
Each scenario times an operation WITHOUT instrumentation (baseline) and WITH it, in the same
process, alternating baseline/instrumented repeats so drift affects both equally:

* ``warmup`` operations first (not timed), then ``repeats`` x ``ops`` timed operations each;
* time per repeat with ``time.perf_counter_ns`` -> nanoseconds per operation;
* reported: median and p90 across repeats, and **overhead = median(instrumented) - median(baseline)**;
* the exporter is an in-process null exporter (encodes batches, discards them) so network time is
  excluded — this measures the SDK, not the collector; batches are flushed between repeats and the
  flush is NOT inside the timed region (it runs on the worker thread in real use);
* ``cpu_per_span_us`` additionally measures total process CPU (``time.process_time``: application
  thread + exporter thread, including JSON encoding) per span over a run that includes flushing;
* ``bytes_per_event`` is the mean encoded size of exported events;
* ``queued_bytes_per_event`` uses ``tracemalloc`` to measure memory held per queued event.

The garbage collector stays enabled (as in production); ``gc.collect()`` runs before each repeat.
Results depend on the machine — compare runs on the same machine only.
"""

from __future__ import annotations

import argparse
import contextlib
import gc
import json
import os
import platform
import statistics
import sys
import time
import tracemalloc
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import aobs
from aobs.export import EncodedBatch, ExportResult
from aobs.integrations import openai_compat


class NullExporter:
    """Accepts and discards batches (bytes are counted)."""

    def __init__(self) -> None:
        self.events = 0
        self.bytes = 0

    def export(self, batch: EncodedBatch, timeout_s: float) -> ExportResult:
        self.events += batch.event_count
        self.bytes += len(batch.body)
        return ExportResult("success")

    def shutdown(self) -> None:
        pass


# A fake OpenAI-compatible client: in-process, returns a prebuilt response with usage.
_MESSAGES = [
    {"role": "system", "content": "You are a support agent. " * 10},
    {"role": "user", "content": "Where is my order 1042? " * 30},
]  # ~1 KB of prompt
_RESPONSE = SimpleNamespace(
    id="chatcmpl-1",
    model="model-x-2026",
    _request_id="req_1",
    choices=[
        SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(role="assistant", content="It shipped.", tool_calls=None)
        )
    ],
    usage=SimpleNamespace(
        prompt_tokens=300,
        completion_tokens=8,
        total_tokens=308,
        prompt_tokens_details=None,
        completion_tokens_details=None,
    ),
)


class _Completions:
    def create(self, **kwargs: Any) -> Any:
        return _RESPONSE


class FakeClient:
    base_url = "http://fake.invalid/v1"

    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=_Completions())


def _lookup(order_id: str, verbose: bool = False) -> dict[str, str]:
    return {"order_id": order_id, "status": "shipped"}


def _time(op: Callable[[], Any], ops: int) -> float:
    gc.collect()
    started = time.perf_counter_ns()
    for _ in range(ops):
        op()
    return (time.perf_counter_ns() - started) / ops


def _scenarios() -> dict[str, tuple[Callable[[], Any], Callable[[], Any], str]]:
    """name -> (baseline op, instrumented op, capture mode)."""
    tool_metadata = aobs.tool(_lookup)
    fake = FakeClient()
    wrapped = openai_compat.wrap(FakeClient(), provider="fake")

    def span_base() -> None:
        with contextlib.nullcontext():
            pass

    def span_inst() -> None:
        with aobs.span("op"):
            pass

    return {
        "empty_span": (span_base, span_inst, "metadata_only"),
        "tool_call_metadata_only": (lambda: _lookup("1042"), lambda: tool_metadata("1042"), "metadata_only"),
        "tool_call_full_capture": (lambda: _lookup("1042"), lambda: tool_metadata("1042"), "full"),
        "llm_call_metadata_only": (
            lambda: fake.chat.completions.create(model="m", messages=_MESSAGES),
            lambda: wrapped.chat.completions.create(model="m", messages=_MESSAGES),
            "metadata_only",
        ),
        "llm_call_full_capture": (
            lambda: fake.chat.completions.create(model="m", messages=_MESSAGES),
            lambda: wrapped.chat.completions.create(model="m", messages=_MESSAGES),
            "full",
        ),
    }


def _init(exporter: NullExporter, capture: str) -> None:
    aobs.init(
        service_name="bench",
        environment="bench",
        exporter=exporter,
        capture=capture,
        max_queue_size=1_000_000,
        schedule_delay_s=60.0,
        start_delay_s=60.0,
    )


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def run_benchmark(quick: bool = False) -> dict[str, Any]:
    ops, repeats, warmup = (2_000, 5, 500) if quick else (20_000, 15, 5_000)
    results: dict[str, Any] = {}
    for name, (baseline, instrumented, capture) in _scenarios().items():
        exporter = NullExporter()
        _init(exporter, capture)
        for _ in range(warmup):
            baseline()
            instrumented()
        aobs.flush(30)
        base_samples, inst_samples = [], []
        for _ in range(repeats):
            base_samples.append(_time(baseline, ops))
            inst_samples.append(_time(instrumented, ops))
            aobs.flush(30)  # outside the timed region
        # Total CPU (application + exporter thread) per instrumented op, including encoding/export.
        cpu_started = time.process_time()
        for _ in range(ops):
            instrumented()
        aobs.flush(30)
        cpu_inst = (time.process_time() - cpu_started) / ops
        cpu_started = time.process_time()
        for _ in range(ops):
            baseline()
        cpu_base = (time.process_time() - cpu_started) / ops
        diag = aobs.diagnostics()
        aobs.shutdown(10)
        dropped = sum(v for k, v in diag.items() if k.startswith("events_dropped") and isinstance(v, int))
        results[name] = {
            "capture": capture,
            "baseline_ns_median": round(statistics.median(base_samples)),
            "instrumented_ns_median": round(statistics.median(inst_samples)),
            "instrumented_ns_p90": round(_percentile(inst_samples, 0.9)),
            "overhead_us_median": round((statistics.median(inst_samples) - statistics.median(base_samples)) / 1000, 2),
            "cpu_per_op_us_total": round(max(cpu_inst - cpu_base, 0.0) * 1e6, 2),
            "bytes_per_event": round(exporter.bytes / exporter.events) if exporter.events else None,
            "events_exported": exporter.events,
            "events_dropped": dropped,
            "internal_errors": diag["internal_errors"],
        }

    # SDK not initialized: cost of instrumented code paths when telemetry is off.
    aobs.shutdown()
    disabled_tool = aobs.tool(_lookup)
    base = [_time(lambda: _lookup("1042"), ops) for _ in range(repeats)]
    inst = [_time(lambda: disabled_tool("1042"), ops) for _ in range(repeats)]
    results["tool_call_sdk_not_initialized"] = {
        "baseline_ns_median": round(statistics.median(base)),
        "instrumented_ns_median": round(statistics.median(inst)),
        "overhead_us_median": round((statistics.median(inst) - statistics.median(base)) / 1000, 2),
    }

    # Memory held per queued (not yet exported) event. Stay below max_batch_events so the worker is
    # not woken to export, and the 60 s schedule delay keeps everything queued while measuring.
    exporter = NullExporter()
    aobs.init(
        service_name="bench",
        environment="bench",
        exporter=exporter,
        max_queue_size=1_000_000,
        schedule_delay_s=60.0,
        start_delay_s=60.0,
        max_batch_events=1000,
    )
    tool = aobs.tool(_lookup)
    tool("warm")
    aobs.flush(10)
    gc.collect()
    tracemalloc.start()
    before = tracemalloc.get_traced_memory()[0]
    count = 900
    for _ in range(count):
        tool("1042")
    after = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    queued = aobs.diagnostics()["queued"]
    aobs.shutdown(10)
    results["memory"] = {"queued_events": queued, "queued_bytes_per_event": round((after - before) / max(queued, 1))}

    return {
        "environment": environment_info(),
        "method": {"ops": ops, "repeats": repeats, "warmup": warmup},
        "results": results,
    }


def environment_info() -> dict[str, Any]:
    cpu = platform.processor() or "unknown"
    with contextlib.suppress(OSError), open("/proc/cpuinfo") as f:
        for line in f:
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    return {
        "date": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "cpu": cpu,
        "cpu_count": os.cpu_count(),
        "aobs": aobs.__version__,
    }


def to_markdown(report: dict[str, Any]) -> str:
    env = report["environment"]
    lines = [
        f"Python {env['python']} on {env['platform']} — {env['cpu']} ({env['cpu_count']} CPUs), {env['date']}",
        f"ops={report['method']['ops']} repeats={report['method']['repeats']} warmup={report['method']['warmup']}",
        "",
        "| scenario | capture | baseline (ns/op) | instrumented (ns/op, median) | p90 | overhead (µs/op) "
        "| total CPU incl. export (µs/op) | bytes/event |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, r in report["results"].items():
        if name == "memory":
            continue
        lines.append(
            f"| {name} | {r.get('capture', '—')} | {r['baseline_ns_median']} | {r['instrumented_ns_median']} "
            f"| {r.get('instrumented_ns_p90', '—')} | {r['overhead_us_median']} "
            f"| {r.get('cpu_per_op_us_total', '—')} | {r.get('bytes_per_event', '—')} |"
        )
    mem = report["results"]["memory"]
    lines.append("")
    lines.append(f"Memory: {mem['queued_bytes_per_event']} bytes per queued event ({mem['queued_events']} queued)")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--json", help="write the full report to this file")
    args = parser.parse_args()
    report = run_benchmark(quick=args.quick)
    print(to_markdown(report))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=2)
            f.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
