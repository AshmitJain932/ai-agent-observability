"""The action-safety layer (``aobs.safety``) on the example support agent: before the agent's refund
can reach the (simulated) payments API, a policy decides whether it may happen, and an ambiguous
timeout is never retried.

    uv run python examples/support_agent/safety_demo.py                         # every scenario
    uv run python examples/support_agent/safety_demo.py --scenario timeout-unknown

Everything is local and deterministic: the scripted mock model (``mock_llm.py``) and an
in-memory payments ledger (``payments.py``). No refund, payment or network call is real.

The evidence for a refund is the order the support ticket is about, looked up in the order
system. It never comes from the model: the model's request has to match it. The ticket also names
the logical refund (``action_key``), so every attempt at it sends the payments API the same
idempotency key (:class:`aobs.safety.IdempotencyKeys`). The guard only decides which attempt may
start; the payments API honouring that key is what makes a repeated request (a retry, another
process, or a paused one that resumes) a no-op. See ``protect_example.py`` for wrapping an existing tool in a few lines.
"""

from __future__ import annotations

import argparse
import secrets
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent
from mock_llm import mock_client
from payments import MockPayments, Mode, PaymentsRejected

import aobs
from aobs import safety
from aobs.export import JsonlFileExporter
from aobs.integrations import openai_compat

POLICY = Path(__file__).resolve().parent / "safety_policy.toml"


@dataclass(frozen=True)
class Scenario:
    question: str  # what the customer wrote (untrusted)
    ticket_order: str  # the order the ticket is about, from the support system
    mode: Mode = "ok"
    lookup_available: bool = True
    approve: bool | None = None  # None: no approver is available
    runs: int = 1  # the same request again (duplicate check)
    store: Literal["memory", "sqlite"] = "memory"
    restart: bool = False  # a new guard (a restarted process) for every run, sharing the store


SCENARIOS: dict[str, Scenario] = {
    "allowed": Scenario("Please refund order 1042, it arrived broken.", "1042"),
    "needs-approval": Scenario("Please refund order 2077, it arrived broken.", "2077"),
    "approved": Scenario("Please refund order 2077, it arrived broken.", "2077", approve=True),
    "wrong-order": Scenario("Please refund order 1042, it arrived broken.", "2077"),
    "unverified-order": Scenario("Please refund order 1042, it arrived broken.", "9999"),
    "rejected": Scenario("Please refund order 1042, it arrived broken.", "1042", mode="rejected"),
    "timeout-not-done": Scenario("Please refund order 1042, it arrived broken.", "1042", mode="timeout_before_commit"),
    "timeout-done": Scenario("Please refund order 1042, it arrived broken.", "1042", mode="timeout_after_commit"),
    "timeout-unknown": Scenario(
        "Please refund order 1042, it arrived broken.", "1042", mode="timeout_after_commit", lookup_available=False
    ),
    "timeout-pending": Scenario("Please refund order 1042, it arrived broken.", "1042", mode="timeout_pending"),
    "duplicate": Scenario("Please refund order 1042, it arrived broken.", "1042", runs=2),
    "restart": Scenario("Please refund order 1042, it arrived broken.", "1042", runs=2, store="sqlite", restart=True),
}


def for_model(result: safety.ActionResult) -> dict[str, Any]:
    """What the agent's model sees: the outcome, never a claim the refund happened unless it did."""
    if result.outcome == "SUCCEEDED":
        return {"status": "refunded", "reference": result.value["reference"]}
    if result.final_outcome == "SUCCEEDED":
        return {"status": "refunded_after_status_check"}
    if result.previous_outcome == "SUCCEEDED":
        return {"status": "already_refunded"}
    if result.previous_outcome in ("RESERVED", "EXECUTING", "UNKNOWN"):
        return {"status": "refund_in_progress_or_unknown", "note": "an earlier attempt is unresolved; do not retry"}
    if result.outcome == "NOT_EXECUTED":
        if result.approval == "unavailable":
            return {"status": "awaiting_approval"}
        return {"status": "refund_not_allowed", "reasons": list(result.decision.reason_codes)}
    if result.final_outcome == "FAILED":
        return {"status": "refund_not_made"}
    return {"status": "refund_outcome_unknown", "note": "escalated to a person; do not retry"}


def guarded_refund(
    guard: safety.ActionGuard,
    payments: MockPayments,
    ticket: str,
    ticket_order: str,
    approver: Callable[[safety.ActionContext], bool] | None,
    results: list[safety.ActionResult],
) -> Callable[..., dict[str, Any]]:
    """The agent's ``issue_refund`` tool, behind the guard. One refund per ticket (``action_key``)."""

    @aobs.tool(name="issue_refund")
    def send_refund(order_id: str, amount: float, idempotency_key: str) -> dict[str, Any]:
        try:
            return payments.refund(order_id, amount, idempotency_key)
        except PaymentsRejected as exc:  # refused before anything happened: a definite failure
            raise safety.ActionNotPerformed(type(exc).__name__) from exc

    def issue_refund(order_id: str, amount: float) -> dict[str, Any]:
        evidence = agent.get_order_status(ticket_order)  # the order system, not the model
        result = guard.run(
            "issue_refund",
            {"order_id": order_id, "amount": amount},
            evidence,
            lambda ctx: send_refund(ctx.request["order_id"], ctx.request["amount"], ctx.idempotency_key),
            action_key=f"refund:{ticket}",  # from the support system: the same for every attempt
            verify=lambda ctx: payments.status(ctx.idempotency_key),  # read-only: never refunds
            approver=approver,
        )
        results.append(result)
        return for_model(result)

    return issue_refund


@dataclass
class Outcome:
    scenario: str
    results: list[safety.ActionResult] = field(default_factory=list)
    payments: MockPayments = field(default_factory=MockPayments)
    replies: list[str] = field(default_factory=list)


def run_scenario(name: str, policy: safety.SafetyPolicy) -> Outcome:
    scenario = SCENARIOS[name]
    outcome = Outcome(name, payments=MockPayments(scenario.mode, scenario.lookup_available))
    approver = None if scenario.approve is None else (lambda ctx: scenario.approve is True)
    # The demo's store lives in a temporary directory, so secrets made for this run are enough. A real
    # deployment loads two stable random secrets (32+ bytes each) from its secret manager into every
    # process: one for the store, and a separate long-lived one for provider idempotency keys.
    store_secret = secrets.token_bytes(32)
    idempotency = safety.IdempotencyKeys(namespace="support-agent-demo/local", secret=secrets.token_bytes(32))
    with tempfile.TemporaryDirectory() as tmp:
        store: safety.ActionStore = (
            safety.SqliteActionStore(Path(tmp) / "actions.db", secret=store_secret)
            if scenario.store == "sqlite"
            else safety.MemoryActionStore()
        )
        guard = safety.ActionGuard(policy, store=store, idempotency=idempotency)
        for attempt in range(1, scenario.runs + 1):
            if scenario.restart:  # a new process: nothing in memory, only the store
                store = safety.SqliteActionStore(Path(tmp) / "actions.db", secret=store_secret)  # same secret
                guard = safety.ActionGuard(policy, store=store, idempotency=idempotency)
            tool = guarded_refund(
                guard, outcome.payments, f"T-{name}", scenario.ticket_order, approver, outcome.results
            )
            client = openai_compat.wrap(mock_client(), provider="mock_llm")
            reply = agent.answer(client, "mock-model", scenario.question, f"safety-{name}-{attempt}",
                                 tools={"issue_refund": tool})  # fmt: skip
            outcome.replies.append(reply)
    return outcome


def describe(outcome: Outcome) -> list[str]:
    lines = [f"{outcome.scenario}:"]
    for r in outcome.results:
        verified = f", verified {r.verified}" if r.verified else ""
        previous = f"  earlier attempt {r.previous_outcome}" if r.previous_outcome else ""
        lines.append(
            f"  decision {r.decision.verdict} {list(r.decision.reason_codes)}  approval {r.approval}  "
            f"outcome {r.outcome}{verified}  next {r.recovery}{previous}"
        )
    p = outcome.payments
    lines.append(f"  payments: {p.refund_calls} refund call(s), {p.status_calls} status lookup(s), "
                 f"{p.refunds_for('1042') + p.refunds_for('2077')} refund(s) made")  # fmt: skip
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", choices=["all", *SCENARIOS], default="all")
    parser.add_argument("--out", default="safety_traces.jsonl", help="trace file (JSONL) to write")
    args = parser.parse_args(argv)
    policy = safety.load_policy(POLICY)
    aobs.init(service_name="support-agent-safety-demo", environment="dev", exporter=JsonlFileExporter(args.out))
    try:
        for name in SCENARIOS if args.scenario == "all" else [args.scenario]:
            print("\n".join(describe(run_scenario(name, policy))))
    finally:
        aobs.shutdown(timeout_s=5)
    print(f"traces written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
