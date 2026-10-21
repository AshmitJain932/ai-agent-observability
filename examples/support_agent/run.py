"""Run the instrumented support agent and show the trace it produced.

    uv run python examples/support_agent/run.py                        # mock model, success
    uv run python examples/support_agent/run.py --scenario refund-outage   # tool fails
    uv run python examples/support_agent/run.py --scenario model-outage    # model provider fails (simulated 503)
    uv run python examples/support_agent/run.py --capture redacted --no-usage
    AOBS_EXAMPLE_MODE=live OPENAI_API_KEY=... AOBS_EXAMPLE_MODEL=<model> \\
        uv run python examples/support_agent/run.py                    # real OpenAI-compatible API

Telemetry is written to a JSON-lines file (``--out``, default ``traces.jsonl``);
``--endpoint`` sends it to the collector (``aobs-server serve``, key in ``AOBS_API_KEY``) instead. Afterwards the
script validates the file with the Phase 1 model library and prints the reconstructed trace —
exactly what the collector will see. Every run answers one ticket (``--ticket``, a fresh id by
default), recorded as the run's session id: ``db:session=<ticket>`` selects that run in the
collector's database.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent

import aobs  # aobs
from aobs.export import HttpExporter, JsonlFileExporter  # aobs
from aobs.integrations import openai_compat  # aobs

# SDK counters of events that did not reach the collector (docs/sdk.md, "Diagnostics counters").
LOSS_COUNTERS = (
    "events_dropped_queue_full",
    "events_dropped_after_shutdown",
    "events_dropped_export_failed",
    "events_dropped_oversize",
    "events_rejected_by_collector",
)
SCENARIOS = {  # question, refund API down, model provider down (both simulated)
    "success": ("Where is my order 1042?", False, False),
    "refund": ("Please refund order 1042, it arrived broken.", False, False),
    "refund-outage": ("Please refund order 1042, it arrived broken.", True, False),
    "model-outage": ("Where is my order 1042?", False, True),
}


def build_client(mode: str, report_usage: bool, model_outage: bool = False) -> tuple[Any, str]:
    if mode == "live" and model_outage:
        raise SystemExit("--scenario model-outage simulates a provider outage; it needs --mode mock")
    if mode == "live":
        from openai import OpenAI

        model = os.environ.get("AOBS_EXAMPLE_MODEL")
        if not model or not os.environ.get("OPENAI_API_KEY"):
            raise SystemExit("live mode needs OPENAI_API_KEY and AOBS_EXAMPLE_MODEL (OPENAI_BASE_URL optional)")
        return OpenAI(), model  # reads OPENAI_API_KEY / OPENAI_BASE_URL from the environment
    from mock_llm import mock_client

    return mock_client(report_usage=report_usage, fail_status=503 if model_outage else None), "mock-model"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="success")
    parser.add_argument("--mode", choices=["mock", "live"], default=os.environ.get("AOBS_EXAMPLE_MODE", "mock"))
    parser.add_argument("--capture", choices=["metadata_only", "redacted", "full"], default="metadata_only")
    parser.add_argument("--no-usage", action="store_true", help="mock model omits usage (shows 'unknown')")
    parser.add_argument("--out", default="traces.jsonl")
    parser.add_argument(
        "--endpoint", help="send to a collector (aobs-server serve; key in AOBS_API_KEY) instead of the file"
    )
    parser.add_argument(
        "--ticket", help="the support ticket this run answers (default: a fresh ticket-<scenario>-<id>)"
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    exporter = (
        HttpExporter(args.endpoint, os.environ.get("AOBS_API_KEY")) if args.endpoint else JsonlFileExporter(args.out)
    )
    aobs.init(  # aobs
        service_name="support-agent-example",
        environment="dev",
        capture=args.capture,
        exporter=exporter,
    )
    question, refund_outage, model_outage = SCENARIOS[args.scenario]
    ticket = args.ticket or f"ticket-{args.scenario}-{secrets.token_hex(4)}"
    client, model = build_client(args.mode, report_usage=not args.no_usage, model_outage=model_outage)
    client = openai_compat.wrap(client, provider="mock_llm" if args.mode == "mock" else None)  # aobs

    agent.REFUNDS.fail = refund_outage
    exit_code = 0
    try:
        reply = agent.answer(client, model, question, ticket_id=ticket)
        if not args.quiet:
            print(f"agent: {reply}")
    except Exception as exc:
        exit_code = 1
        if not args.quiet:
            print(f"agent failed: {type(exc).__name__}: {exc}")
    finally:
        ok = aobs.shutdown(timeout_s=5)  # aobs — flush before exit (also done automatically at exit)
    diagnostics = aobs.diagnostics()
    lost = sum(diagnostics.get(name, 0) for name in LOSS_COUNTERS)  # shutdown only says the flush finished
    if not ok or lost:
        print("warning: not all telemetry was exported", diagnostics)
    elif args.endpoint and not args.quiet:
        print(f"sent the run for ticket {ticket} to {args.endpoint}")
        print(f"  find it: uv run aobs-server traces list --project <project id> 'db:session={quote(ticket, safe='')}'")
    if not args.endpoint and not args.quiet:
        print_trace(Path(args.out))
    return exit_code


def print_trace(path: Path) -> None:
    """Validate every batch with the Phase 1 model library and print the newest trace."""
    try:
        from aobs_server.model import assemble_events, validate_batch
    except ImportError:
        print(f"telemetry written to {path} (install aobs-server to pretty-print it)")
        return
    accepted = []
    for line in path.read_bytes().splitlines():
        result = validate_batch(line)
        if not result.ok or result.rejected:
            print("INVALID BATCH:", result.batch_errors, result.rejected)
        accepted.extend(result.accepted)
    traces = assemble_events(accepted, datetime.now(UTC))
    newest = max(traces.values(), key=lambda t: t.started_at or datetime.min.replace(tzinfo=UTC))
    print(f"\ntrace {newest.trace_id}  status={newest.status}  spans={newest.span_count}")
    tokens = newest.tokens["input_tokens"]
    print(f"input tokens: {tokens.value} (reported by {tokens.reported_spans} of {tokens.expected_spans} LLM calls)")

    def show(span_id: str, depth: int) -> None:
        node = newest.spans[span_id]
        fields = node.record.merged
        detail = ""
        if node.kind == "llm":
            usage = fields.get("usage")
            detail = f"usage={usage.get('input_tokens')}/{usage.get('output_tokens')}" if usage else "usage=unknown"
        elif node.kind == "tool" and fields.get("input_fingerprint"):
            detail = f"input={fields['input_fingerprint'][:28]}…"
        error = fields.get("error")
        if error:
            detail += f"  {error['type']}: {error.get('message', '')}"
        print(
            f"{'  ' * depth}├─ {node.kind:<5} {node.name:<18} {node.duration_ms or 0:8.1f} ms  {node.status:<9} "
            f"{detail}"
        )
        for child in node.children:
            show(child, depth + 1)

    for root in newest.roots:
        show(root, 0)


if __name__ == "__main__":
    raise SystemExit(main())
