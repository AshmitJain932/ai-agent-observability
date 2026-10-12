"""Instrumentation overhead: the reproducible benchmark runs and stays within regression bounds.

The bounds are deliberately loose (10-20x the recorded baseline in docs/benchmarks.md) so the test
is stable on slow CI machines; it catches pathological regressions — e.g. I/O or locks held on the
recording path — not small slowdowns. Recorded numbers live in sdk/python/benchmarks/results/.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

BENCH_DIR = Path(__file__).resolve().parents[1] / "benchmarks"


@pytest.fixture(scope="module")
def bench() -> ModuleType:
    spec = importlib.util.spec_from_file_location("aobs_overhead_bench", BENCH_DIR / "overhead.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def report(bench: ModuleType) -> dict:
    return bench.run_benchmark(quick=True)


def test_benchmark_reports_every_scenario(report: dict) -> None:
    results = report["results"]
    for name in (
        "empty_span",
        "tool_call_metadata_only",
        "tool_call_full_capture",
        "llm_call_metadata_only",
        "llm_call_full_capture",
        "tool_call_sdk_not_initialized",
        "memory",
    ):
        assert name in results
    for name, result in results.items():
        if "events_dropped" in result:
            assert result["events_dropped"] == 0, name  # measurements are not distorted by drops
            assert result["internal_errors"] == 0, name
            assert result["events_exported"] > 0, name
    assert report["environment"]["python"]


def test_overhead_within_regression_bounds(report: dict) -> None:
    results = report["results"]
    assert results["empty_span"]["overhead_us_median"] < 500
    assert results["tool_call_metadata_only"]["overhead_us_median"] < 1000
    assert results["llm_call_metadata_only"]["overhead_us_median"] < 2000
    assert results["tool_call_sdk_not_initialized"]["overhead_us_median"] < 20
    assert results["memory"]["queued_bytes_per_event"] < 20_000


def test_metadata_only_events_are_smaller_than_full_capture(report: dict) -> None:
    results = report["results"]
    assert results["llm_call_metadata_only"]["bytes_per_event"] < results["llm_call_full_capture"]["bytes_per_event"]


def test_recorded_baselines_exist_and_are_well_formed() -> None:
    files = sorted((BENCH_DIR / "results").glob("*.json"))
    assert files, "no recorded baseline"
    for path in files:
        data = json.loads(path.read_text())
        assert data["method"]["repeats"] >= 15 and data["method"]["ops"] >= 20_000  # full runs only
        assert "empty_span" in data["results"]
