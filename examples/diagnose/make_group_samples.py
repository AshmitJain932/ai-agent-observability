"""Record groups of repeated runs for ``aobs-server compare-groups`` with the real SDK.

    uv run python examples/diagnose/make_group_samples.py            # rewrite examples/diagnose/groups/
    uv run python examples/diagnose/make_group_samples.py --out DIR

Every file holds several runs of the same task (an inventory agent: plan -> search_inventory ->
summarize). Step durations get seeded random jitter on top of real timing noise, so the runs vary
the way real runs do. Recorded with ``capture="metadata_only"``.

* ``baseline.jsonl``       10 healthy runs
* ``healthy_rerun.jsonl``  10 more healthy runs (different jitter): should show no regression
* ``slow.jsonl``           10 runs where the inventory search takes ~95 ms longer
* ``flaky.jsonl``          10 runs where the inventory database refuses connections: 7 runs fail
                           after 3 attempts, 3 succeed on the 2nd attempt
* ``outlier.jsonl``        10 healthy runs, one of which hits a 1.5 s cold start
* ``few_runs.jsonl``       3 slow runs: too few for a statistical conclusion
* ``partial_usage.jsonl``  10 healthy runs; in 6 of them the summary call reports no usage or cost
"""

from __future__ import annotations

import argparse
import random
import time
from pathlib import Path

import aobs
from aobs.export import JsonlFileExporter

HERE = Path(__file__).resolve().parent
PRICE_IN, PRICE_OUT = 0.0000005, 0.0000015  # what the (simulated) provider reports per token


def _sleep_ms(ms: float) -> None:
    time.sleep(max(ms, 0.0) / 1000)


def _llm(name: str, rng: random.Random, base_ms: float, tokens_in: int, report_usage: bool = True) -> None:
    with aobs.llm_span(provider="example", request_model="planner-small", name=name) as llm:
        llm.set_llm(response_model="planner-small")
        tin = tokens_in + rng.randint(-15, 15)
        tout = 40 + rng.randint(-5, 5)
        if report_usage:
            llm.record_usage(input_tokens=tin, output_tokens=tout, source="provider_reported")
            llm.record_cost(f"{tin * PRICE_IN + tout * PRICE_OUT:.8f}", "USD")
        _sleep_ms(rng.gauss(base_ms, base_ms * 0.15))


def one_run(
    rng: random.Random,
    search_ms: float = 35.0,
    failures: int = 0,
    report_summary_usage: bool = True,
) -> None:
    """One restock_check run. ``failures``: refused attempts before success (3 = never succeeds)."""
    try:
        with aobs.run("restock_check", agent="inventory_agent", agent_version="1.4.0"):
            _llm("plan", rng, 15, 420)
            previous = None
            succeeded = False
            for attempt in range(1, 4):
                try:
                    with aobs.tool_span("search_inventory", tool_type="db") as tool:
                        tool.set_retry(attempt, max_attempts=3)
                        tool.fingerprint_input({"sku": "A-1", "warehouse": "BER"})
                        if previous is not None:
                            tool.add_link(previous.trace_id, previous.span_id, "retry_of")
                        previous = tool
                        _sleep_ms(rng.gauss(search_ms, 6))
                        if attempt <= failures:
                            raise ConnectionError("inventory-db:5432 connection refused")
                    succeeded = True
                    break
                except ConnectionError:
                    _sleep_ms(10)
            _llm("summarize", rng, 25, 300, report_usage=report_summary_usage)
            if not succeeded:
                raise RuntimeError("inventory lookup failed after 3 attempts")
    except RuntimeError:
        pass


def record(path: Path, seed: int, plan: list[dict[str, object]]) -> None:
    path.unlink(missing_ok=True)
    aobs.init(service_name="inventory-agent", environment="staging", exporter=JsonlFileExporter(str(path)))
    rng = random.Random(seed)
    for options in plan:
        one_run(rng, **options)  # type: ignore[arg-type]
    aobs.shutdown(timeout_s=10)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=HERE / "groups")
    args = parser.parse_args(argv)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    healthy: dict[str, object] = {}
    record(out / "baseline.jsonl", 1, [healthy] * 10)
    record(out / "healthy_rerun.jsonl", 2, [healthy] * 10)
    record(out / "slow.jsonl", 3, [{"search_ms": 130.0}] * 10)
    record(out / "flaky.jsonl", 4, [{"failures": 3}] * 7 + [{"failures": 1}] * 3)
    record(out / "outlier.jsonl", 5, [healthy] * 6 + [{"search_ms": 1500.0}] + [healthy] * 3)
    record(out / "few_runs.jsonl", 6, [{"search_ms": 130.0}] * 3)
    record(out / "partial_usage.jsonl", 7, [{"report_summary_usage": False}] * 6 + [healthy] * 4)
    for path in sorted(out.glob("*.jsonl")):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
