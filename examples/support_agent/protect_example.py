"""Protecting the agent's existing ``issue_refund`` tool with ``aobs.safety``, without changing the
tool or the agent loop. The additions are the lines marked ``# safety``.

    uv run python examples/support_agent/protect_example.py

The customer asks for the same refund three times: twice in one process, then once more after a
restart (a new guard on the same store). The tool runs once. Everything is local: the scripted mock
model and the example agent's stand-in refund tool; no refund or network call is real.

This tool takes no idempotency key and its payments API has no status lookup, so an ambiguous
failure could only be escalated, and nothing would stop a paused process that resumes from repeating
the call: see ``safety_demo.py`` for ``idempotency_param`` and ``verify``.
"""

from __future__ import annotations

import secrets
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent
from mock_llm import mock_client

from aobs import safety  # safety
from aobs.integrations import openai_compat

POLICY = Path(__file__).resolve().parent / "safety_policy.toml"


def tell_model(result: safety.ActionResult) -> dict[str, Any]:  # safety
    """What the model is told when the guard did not return the tool's own result."""  # safety
    if result.final_outcome == "SUCCEEDED" or result.previous_outcome == "SUCCEEDED":  # safety
        return {"status": "refunded"}  # safety
    if result.final_outcome == "UNKNOWN" or result.previous_outcome in ("RESERVED", "EXECUTING", "UNKNOWN"):  # safety
        return {"status": "outcome_unknown", "note": "escalated to a person; do not retry"}  # safety
    if result.final_outcome == "FAILED":  # safety
        return {"status": "not_refunded"}  # safety
    return {"status": "not_allowed", "reasons": list(result.decision.reason_codes)}  # safety


def protected_tools(guard: safety.ActionGuard, ticket_id: str, ticket_order: str) -> dict[str, Callable[..., Any]]:
    """The tools for one ticket: the evidence and the action key come from the ticket."""
    refund = guard.protect(  # safety
        agent.issue_refund,  # safety: the existing tool, unchanged
        action="issue_refund",  # safety
        evidence=lambda: agent.get_order_status(ticket_order),  # safety: the order system, not the model
        action_key=f"refund:{ticket_id}",  # safety: one refund per ticket
    )  # safety

    def issue_refund(order_id: str, amount: float) -> dict[str, Any]:  # safety
        try:  # safety
            return refund(order_id=order_id, amount=amount)  # safety
        except safety.ActionBlocked as blocked:  # safety
            return tell_model(blocked.result)  # safety

    return {"issue_refund": issue_refund}  # safety


def main() -> list[str]:
    question = "Please refund order 1042, it arrived broken."
    replies: list[str] = []
    store_secret = secrets.token_bytes(32)  # safety: in production, stable random secrets from a secret manager
    idempotency = safety.IdempotencyKeys(namespace="support-agent/example", secret=secrets.token_bytes(32))  # safety
    with tempfile.TemporaryDirectory() as tmp:
        policy = safety.load_policy(POLICY)  # safety
        store = Path(tmp) / "actions.db"
        for process in ("first", "first", "restarted"):
            if process == "restarted" or not replies:
                store_ = safety.SqliteActionStore(store, secret=store_secret)  # safety
                guard = safety.ActionGuard(policy, store=store_, idempotency=idempotency)  # safety
            client = openai_compat.wrap(mock_client(), provider="mock_llm")
            tools = protected_tools(guard, "T-1001", ticket_order="1042")  # safety
            replies.append(agent.answer(client, "mock-model", question, "T-1001", tools=tools))
            print(f"{process} process: {replies[-1]}")
        record = guard.inspect("issue_refund", "refund:T-1001")
        print(f"stored state of refund:T-1001: {record.state if record else None}")
    return replies


if __name__ == "__main__":
    main()
