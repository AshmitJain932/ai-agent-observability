"""A small tool-calling support agent. Instrumentation lines are marked ``# aobs``.

The agent answers a customer question with an OpenAI-compatible chat model and two tools:
``get_order_status`` and ``issue_refund``. It works with any OpenAI-compatible client — the real
API in live mode, or a scripted mock model (``mock_llm.py``) that needs no credentials.
"""

from __future__ import annotations

import json
from typing import Any

import aobs  # aobs

ORDERS = {
    "1042": {"status": "delivered", "amount": 59.90, "carrier": "DHL"},
    "2077": {"status": "in_transit", "amount": 120.00, "carrier": "UPS"},
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_order_status",
            "description": "Look up an order's delivery status",
            "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "issue_refund",
            "description": "Refund an order",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}, "amount": {"type": "number"}},
                "required": ["order_id", "amount"],
            },
        },
    },
]


class RefundService:
    """Stands in for an external payments API. ``fail=True`` simulates an outage."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail


REFUNDS = RefundService()


@aobs.tool  # aobs
def get_order_status(order_id: str) -> dict[str, Any]:
    order = ORDERS.get(order_id)
    if order is None:
        return {"error": f"order {order_id} not found"}
    return {"order_id": order_id, **order}


@aobs.tool  # aobs
def issue_refund(order_id: str, amount: float) -> dict[str, Any]:
    if REFUNDS.fail:
        raise TimeoutError("payments API did not respond within 2s")
    return {"order_id": order_id, "refunded": amount, "reference": f"rf_{order_id}"}


TOOL_FUNCTIONS = {"get_order_status": get_order_status, "issue_refund": issue_refund}

SYSTEM_PROMPT = "You are a support agent. Use tools to look up orders and issue refunds when asked."


def answer(
    client: Any, model: str, question: str, ticket_id: str, max_steps: int = 6, tools: dict[str, Any] | None = None
) -> str:
    """Run the agent loop: model -> tool calls -> model ... until the model answers in text.
    ``tools`` replaces the tool functions by name (e.g. a guarded ``issue_refund``)."""
    functions = {**TOOL_FUNCTIONS, **(tools or {})}
    with aobs.run("answer_ticket", agent="support_agent", agent_version="1.0", session_id=ticket_id):  # aobs
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]
        for _ in range(max_steps):
            response = client.chat.completions.create(model=model, messages=messages, tools=TOOLS)
            message = response.choices[0].message
            if not message.tool_calls:
                return str(message.content)
            messages.append(message.model_dump(exclude_none=True))
            for call in message.tool_calls:
                arguments = json.loads(call.function.arguments)
                result = functions[call.function.name](**arguments)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)})
        raise RuntimeError(f"no answer after {max_steps} steps")
