"""A LangGraph tool-calling agent recorded with the aobs LangChain callback handler.

    uv run python examples/langgraph_agent/run.py                       # success
    uv run python examples/langgraph_agent/run.py --scenario tool-error  # the tool raises
    uv run python examples/langgraph_agent/run.py --capture redacted     # also record (redacted) content

Runs offline: the model is langchain-core's ``GenericFakeChatModel`` playing back a scripted tool
call (with usage metadata, as a provider integration would report it) and a final answer. No
network, no API keys. Telemetry is written to ``--out`` (default ``traces.jsonl``); afterwards the
script validates the file with the collector's model library and prints the reconstructed trace.
Instrumentation lines are marked ``# aobs``.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

import aobs  # aobs
from aobs.export import JsonlFileExporter  # aobs
from aobs.integrations.langchain import CallbackHandler  # aobs

ORDERS = {"1042": "delivered", "2077": "in_transit"}


@tool
def get_order_status(order_id: str) -> str:
    """Look up an order's delivery status."""
    if order_id not in ORDERS:
        raise KeyError(f"no order {order_id}")
    return ORDERS[order_id]


def scripted_model(order_id: str) -> GenericFakeChatModel:
    """Stands in for a real chat model (e.g. ``ChatOpenAI``); the graph code is the same."""
    call = AIMessage(
        content="",
        tool_calls=[{"name": "get_order_status", "args": {"order_id": order_id}, "id": "call_1"}],
        usage_metadata={"input_tokens": 52, "output_tokens": 11, "total_tokens": 63},
        response_metadata={"finish_reason": "tool_calls", "model_name": "scripted-model"},
    )
    return GenericFakeChatModel(messages=iter([call, AIMessage("Your order 1042 has been delivered.")]))


def build_graph(model: GenericFakeChatModel) -> Any:
    def agent(state: MessagesState) -> dict[str, Any]:
        return {"messages": [model.invoke(state["messages"])]}

    builder = StateGraph(MessagesState)
    builder.add_node("agent", agent)
    builder.add_node("tools", ToolNode([get_order_status]))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition)
    builder.add_edge("tools", "agent")
    return builder.compile(name="support_agent")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", choices=["success", "tool-error"], default="success")
    parser.add_argument("--capture", choices=["metadata_only", "redacted", "full"], default="metadata_only")
    parser.add_argument("--out", default="traces.jsonl")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    aobs.init(
        service_name="langgraph-example", environment="dev", capture=args.capture, exporter=JsonlFileExporter(args.out)
    )  # aobs
    graph = build_graph(scripted_model("1042" if args.scenario == "success" else "9999"))
    handler = CallbackHandler()  # aobs
    exit_code = 0
    try:
        result = graph.invoke(
            {"messages": [HumanMessage("Where is my order 1042?")]},
            config={"callbacks": [handler], "configurable": {"thread_id": "ticket-1042"}},  # aobs
        )
        if not args.quiet:
            print(f"agent: {result['messages'][-1].content}")
    except Exception as exc:
        exit_code = 1
        if not args.quiet:
            print(f"agent failed: {type(exc).__name__}: {exc}")
    finally:
        aobs.shutdown(timeout_s=5)  # aobs
    if not args.quiet:
        print_trace(Path(args.out))
    return exit_code


def print_trace(path: Path) -> None:
    from aobs_server.model import assemble_events, validate_batch

    accepted = []
    for line in path.read_bytes().splitlines():
        result = validate_batch(line)
        if not result.ok or result.rejected:
            print("INVALID BATCH:", result.batch_errors, result.rejected)
        accepted.extend(result.accepted)
    traces = assemble_events(accepted, datetime.now(UTC))
    newest = max(traces.values(), key=lambda t: t.started_at or datetime.min.replace(tzinfo=UTC))
    print(f"\ntrace {newest.trace_id}  status={newest.status}  spans={newest.span_count}")

    def show(span_id: str, depth: int) -> None:
        node = newest.spans[span_id]
        usage = node.record.merged.get("usage")
        error = node.record.merged.get("error")
        detail = f"usage={usage.get('input_tokens')}/{usage.get('output_tokens')}" if usage else ""
        if error:
            detail += f"  {error['type']}: {error.get('message', '')}"
        print(f"{'  ' * depth}├─ {node.kind:<5} {node.name:<18} {node.status:<9} {detail}")
        for child in node.children:
            show(child, depth + 1)

    for root in newest.roots:
        show(root, 0)


if __name__ == "__main__":
    raise SystemExit(main())
