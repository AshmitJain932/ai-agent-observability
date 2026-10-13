"""Record baseline/current trace pairs for ``aobs-server compare`` with the real SDK.

    uv run python examples/diagnose/make_compare_samples.py            # rewrite examples/diagnose/compare/
    uv run python examples/diagnose/make_compare_samples.py --out DIR

Pairs (each file holds one agent run):

* ``refund_ok.jsonl`` -> ``refund_outage.jsonl``: the Phase 2 support agent; the refund works in the
  baseline and times out in the current run (the run now fails).
* ``refund_ok.jsonl`` -> ``refund_ok_rerun.jsonl``: the same healthy task recorded twice; no regression
  should be reported (timings differ only by noise).
* ``inventory_v1.jsonl`` -> ``inventory_v2.jsonl``: an inventory agent after a "release": a bigger model,
  more tokens and provider-reported cost, a flaky database call that needs retries, a new slow supplier
  check, a dropped cache read, and a slower summary step.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from make_samples import _init, support_agent

import aobs

HERE = Path(__file__).resolve().parent


def inventory(path: Path, version: int) -> None:
    _init(path, "inventory-agent", capture="metadata_only")
    v2 = version == 2
    model = "planner-large" if v2 else "planner-small"
    with aobs.run("restock_check", agent="inventory_agent", agent_version=f"{version}.0.0"):
        with aobs.llm_span(provider="example", request_model=model, name="plan") as llm:
            llm.set_llm(response_model=model)
            llm.record_usage(
                input_tokens=1650 if v2 else 420, output_tokens=40 if v2 else 35, source="provider_reported"
            )
            llm.record_cost("0.00290" if v2 else "0.00021", "USD")
            time.sleep(0.01)
        if not v2:
            with aobs.tool_span("read_cache") as cache:
                cache.fingerprint_input({"sku": "A-1"})
                time.sleep(0.002)
        previous = None
        for attempt in range(1, 4 if v2 else 2):
            try:
                with aobs.tool_span("search_inventory", tool_type="db") as tool:
                    tool.set_retry(attempt, max_attempts=3)
                    tool.fingerprint_input({"sku": "A-1", "warehouse": "BER"})
                    if previous is not None:
                        tool.add_link(previous.trace_id, previous.span_id, "retry_of")
                    previous = tool
                    time.sleep(0.02)
                    if v2 and attempt < 3:
                        raise ConnectionError("inventory-db:5432 connection reset")
                break
            except ConnectionError:
                time.sleep(0.01)
        if v2:
            with aobs.tool_span("check_supplier", tool_type="http") as supplier:
                supplier.fingerprint_input({"sku": "A-1"})
                time.sleep(0.25)
        with aobs.llm_span(provider="example", request_model=model, name="summarize") as llm:
            llm.set_llm(response_model=model)
            llm.record_usage(
                input_tokens=1200 if v2 else 300, output_tokens=80 if v2 else 60, source="provider_reported"
            )
            llm.record_cost("0.00210" if v2 else "0.00018", "USD")
            time.sleep(0.2 if v2 else 0.03)
    aobs.shutdown(timeout_s=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=HERE / "compare")
    args = parser.parse_args(argv)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    support_agent(out / "refund_ok.jsonl", "refund")
    support_agent(out / "refund_ok_rerun.jsonl", "refund")
    support_agent(out / "refund_outage.jsonl", "refund-outage")
    inventory(out / "inventory_v1.jsonl", 1)
    inventory(out / "inventory_v2.jsonl", 2)
    for path in sorted(out.glob("*.jsonl")):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
