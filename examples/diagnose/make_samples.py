"""Record the sample traces in ``traces/`` with the real SDK (no network, no API key).

    uv run python examples/diagnose/make_samples.py            # rewrite examples/diagnose/traces/
    uv run python examples/diagnose/make_samples.py --out DIR  # write somewhere else

Scenarios (each file holds one agent run):

* ``refund_outage.jsonl``      the Phase 2 support agent with the payments API down (tool timeout)
* ``retry_storm.jsonl``        an inventory agent retries a failing database call three times
* ``slow_and_repeated.jsonl``  a research agent re-fetches the same profile and waits on a slow search
* ``healthy_run.jsonl``        the support agent answering normally (no findings expected)
* ``crashed_run.jsonl``        the process dies mid-run: the run's start was exported, its end never was

``retry_storm`` and ``slow_and_repeated`` are recorded with ``capture="full"``, so the files contain
prompts, tool arguments and a password in an error message: ``aobs-server analyze`` must not show them.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "support_agent"))

import aobs
from aobs.export import JsonlFileExporter

HERE = Path(__file__).resolve().parent

CUSTOMER = {"customer_id": "c-7781", "email": "jane.doe@example.com"}


def _init(path: Path, service: str, capture: str = "metadata_only") -> None:
    path.unlink(missing_ok=True)
    aobs.init(service_name=service, environment="staging", capture=capture, exporter=JsonlFileExporter(str(path)))


def support_agent(path: Path, scenario: str) -> None:
    import run  # examples/support_agent/run.py: instruments and runs the Phase 2 example agent

    path.unlink(missing_ok=True)
    run.main(["--scenario", scenario, "--out", str(path), "--quiet"])


def retry_storm(path: Path) -> None:
    _init(path, "inventory-agent", capture="full")
    try:
        with aobs.run("restock_check", agent="inventory_agent", agent_version="0.3.1"):
            with aobs.llm_span(provider="example", request_model="planner-small") as llm:
                llm.set_input([{"role": "user", "content": "Is SKU A-1 in stock at the Berlin warehouse?"}], "messages")
                llm.set_output("call search_inventory(sku='A-1', warehouse='BER')", "text")
                # usage deliberately not recorded: this provider does not report it
            previous = None
            for attempt in range(1, 4):
                try:
                    with aobs.tool_span("search_inventory", tool_type="db") as tool:
                        tool.set_retry(attempt, max_attempts=3)
                        tool.fingerprint_input({"sku": "A-1", "warehouse": "BER"})
                        tool.set_input({"sku": "A-1", "warehouse": "BER"}, "json")
                        if previous is not None:
                            tool.add_link(previous.trace_id, previous.span_id, "retry_of")
                        previous = tool
                        time.sleep(0.02)
                        raise ConnectionError(
                            "could not connect to postgresql://inventory:hunter2pw@inventory-db:5432/stock"
                        )
                except ConnectionError:
                    time.sleep(0.01 * attempt)
            raise RuntimeError("inventory lookup failed after 3 attempts")
    except RuntimeError:
        pass
    aobs.shutdown(timeout_s=5)


def slow_and_repeated(path: Path) -> None:
    _init(path, "research-agent", capture="full")

    @aobs.tool
    def fetch_customer_profile(customer_id: str) -> dict[str, str]:
        time.sleep(0.005)
        return CUSTOMER

    with aobs.run("summarize_account", agent="research_agent", agent_version="2.0.0"):
        for _ in range(3):  # the agent loop forgets the result and asks again
            fetch_customer_profile("c-7781")
        with aobs.span("vector_search", kind="retrieval") as search:
            search.fingerprint_input({"query": "open tickets for c-7781"})
            time.sleep(0.4)
        with aobs.llm_span(provider="example", request_model="writer-large") as llm:
            llm.set_input([{"role": "user", "content": "Summarize the account of jane.doe@example.com"}], "messages")
            llm.set_output("Jane has two open tickets ...", "text")
            llm.record_usage(input_tokens=1840, output_tokens=210, source="provider_reported")
            time.sleep(0.03)
    aobs.shutdown(timeout_s=5)


def crashed_run(path: Path) -> None:
    _init(path, "billing-agent")
    run_span = aobs.run("close_invoice", agent="billing_agent")
    run_span.__enter__()  # entered but never exited: simulates the process dying mid-run
    with aobs.tool_span("load_invoice") as tool:
        tool.fingerprint_input({"invoice": "INV-2024-0042"})
    charge = aobs.tool_span("charge_card", tool_type="http")
    charge.__enter__()  # the process "dies" while waiting on the payment provider
    aobs.flush()  # the run's and this call's starts reach the file; their ends never do
    aobs.shutdown(timeout_s=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=HERE / "traces")
    args = parser.parse_args(argv)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    support_agent(out / "refund_outage.jsonl", "refund-outage")
    support_agent(out / "healthy_run.jsonl", "success")
    retry_storm(out / "retry_storm.jsonl")
    slow_and_repeated(out / "slow_and_repeated.jsonl")
    crashed_run(out / "crashed_run.jsonl")  # last: it leaves a span open in this process
    for path in sorted(out.glob("*.jsonl")):
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
