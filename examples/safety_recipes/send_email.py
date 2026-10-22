"""Recipe: an agent's ``send_email`` tool that sends each notification once, to the right person.

    uv run python examples/safety_recipes/send_email.py

A support agent may email the customer of its ticket from a fixed set of templates. Agents loop,
retry after a timeout and get restarted, and each of those can send the same email again; a prompt
injection in the ticket can ask for another recipient. Behind the guard:

* the recipient must be the ticket's customer **on record** (the evidence), not whatever address
  the model was given;
* one email per (ticket, template): the action key names it, and the policy's ``once_per`` checks
  the same verified values, also after a restart (the store is a SQLite file);
* every attempt sends the mail provider the same idempotency key, and a timeout is settled by one
  read-only status lookup, never by sending again.

Everything is local: the support system is a dict, the mail provider is a fake that keeps an
outbox in memory, and the store lives in a temporary directory. No email is sent.
"""

from __future__ import annotations

import secrets
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from aobs import safety

POLICY = Path(__file__).resolve().parent / "send_email_policy.toml"

TICKETS = {"T-1001": {"customer_id": "C-7"}}  # the support system (trusted)
CUSTOMERS = {"C-7": {"name": "Jane Doe", "email": "jane.doe@example.com"}}
TEMPLATES = {"refund_confirmation": "Your refund was issued", "shipping_update": "Your order has shipped"}


class MailTimeout(TimeoutError):
    """The mail API did not answer in time; the message may or may not have been accepted."""


@dataclass
class FakeMailProvider:
    """Stands in for a mail API that honours idempotency keys (a repeated key is not sent again)
    and can look a message up by its key. Nothing leaves the process."""

    outbox: dict[str, dict[str, str]] = field(default_factory=dict)  # idempotency key -> message
    send_calls: int = 0
    status_calls: int = 0
    lose_next_response: bool = False  # accept the next message, then time out

    def send(self, to: str, template: str, idempotency_key: str) -> dict[str, str]:
        self.send_calls += 1
        message = self.outbox.setdefault(
            idempotency_key, {"to": to, "template": template, "message_id": f"msg-{len(self.outbox) + 1}"}
        )
        if self.lose_next_response:
            self.lose_next_response = False
            raise MailTimeout("mail API did not respond within 5s")
        return {"message_id": message["message_id"]}

    def status(self, idempotency_key: str) -> str:
        """Read-only. NOT_FOUND proves nothing (it may only mean "not yet"), so it stays unknown."""
        self.status_calls += 1
        return "SUCCEEDED" if idempotency_key in self.outbox else "NOT_FOUND"


def evidence_for(ticket_id: str, template: str) -> dict[str, Any]:
    """From the support system and the template registry, never from the model: the ticket's
    customer address, and the template only if it exists."""
    ticket = TICKETS.get(ticket_id)
    customer = CUSTOMERS.get(ticket["customer_id"]) if ticket else None
    return {
        "ticket_id": ticket_id if ticket else None,
        "to": customer["email"] if customer else None,
        "template": template if template in TEMPLATES else None,
    }


def for_model(result: safety.ActionResult) -> dict[str, Any]:
    """What the model is told: the truth, e.g. "already sent", never "not allowed" for a repeat."""
    if result.outcome == "SUCCEEDED":
        return {"status": "sent", "message_id": result.value["message_id"]}
    if result.final_outcome == "SUCCEEDED":
        return {"status": "sent", "note": "confirmed by a status lookup after a timeout"}
    if result.previous_outcome == "SUCCEEDED":
        return {"status": "already_sent"}
    if result.final_outcome == "UNKNOWN" or result.previous_outcome in ("RESERVED", "EXECUTING", "UNKNOWN"):
        return {"status": "outcome_unknown", "note": "escalated to a person; do not retry"}
    return {"status": "not_sent", "reasons": list(result.decision.reason_codes)}


def email_tool(
    guard: safety.ActionGuard, mail: FakeMailProvider, ticket_id: str, results: list[safety.ActionResult]
) -> Callable[..., dict[str, Any]]:
    """The agent's ``send_email`` tool for one ticket. The model chooses ``to`` and ``template``;
    the ticket (and so the customer) comes from the support system."""

    def send_email(to: str, template: str) -> dict[str, Any]:
        result = guard.run(
            "send_email",
            {"ticket_id": ticket_id, "to": to, "template": template},
            evidence_for(ticket_id, template),
            lambda ctx: mail.send(ctx.request["to"], ctx.request["template"], ctx.idempotency_key),
            # One logical email per (ticket, template). An unknown template is denied by the policy
            # (missing evidence) before the key is used.
            action_key=f"email:{ticket_id}:{template}",
            verify=lambda ctx: mail.status(ctx.idempotency_key),  # read-only: never sends
        )
        results.append(result)
        return for_model(result)

    return send_email


class Step(NamedTuple):
    process: str  # "first" or "restarted" (a new guard on the same store file)
    note: str  # what the agent is doing
    to: str
    template: str
    mail_times_out: bool = False


JANE = "jane.doe@example.com"
STEPS = [
    Step("first", "the agent sends the refund confirmation", JANE, "refund_confirmation"),
    Step("first", "it loops and sends it again", JANE, "refund_confirmation"),
    Step("first", "the ticket text told it to use another address", "refunds@attacker.example", "refund_confirmation"),
    Step("first", "it sends a template that does not exist", JANE, "password_reset"),
    Step("first", "the shipping update: the mail API times out", JANE, "shipping_update", mail_times_out=True),
    Step("first", "it retries after the timeout", JANE, "shipping_update"),
    Step("restarted", "after a restart, the refund confirmation again", JANE, "refund_confirmation"),
]


def explain(result: safety.ActionResult) -> str:
    """One line: what happened, and the reason codes that decided it."""
    d = result.decision
    codes = ", ".join(d.reason_codes)
    if result.final_outcome == "SUCCEEDED":
        lookup = f"; {result.error_type}, status lookup: {result.verified}" if result.outcome == "UNKNOWN" else ""
        return f"SENT     {d.verdict} ({codes}{lookup})"
    if result.previous_outcome is not None:
        return f"BLOCKED  {codes} [{d.rule}]: earlier attempt {result.previous_outcome}, not repeated"
    if result.outcome == "NOT_EXECUTED":
        return f"BLOCKED  {d.verdict} {codes} [{d.rule}]"
    return f"{result.final_outcome}  {d.verdict} ({codes}), next: {result.recovery}"


@dataclass
class Run:
    mail: FakeMailProvider = field(default_factory=FakeMailProvider)
    results: list[safety.ActionResult] = field(default_factory=list)
    replies: list[dict[str, Any]] = field(default_factory=list)  # what the model was told
    stored_state: str | None = None


def main() -> Run:
    run = Run()
    policy = safety.load_policy(POLICY)
    # In production: two stable random secrets (32+ bytes each) from a secret manager, the same in
    # every process and after every restart. Generated here because the store is temporary.
    store_secret = secrets.token_bytes(32)
    idempotency = safety.IdempotencyKeys(namespace="support-agent/recipes", secret=secrets.token_bytes(32))
    print("send_email: one email per (ticket, template), only to the customer on record")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "actions.db"
        guard: safety.ActionGuard | None = None
        for number, step in enumerate(STEPS, 1):
            if guard is None or step.process == "restarted":  # a new process: only the store file remains
                guard = safety.ActionGuard(
                    policy, store=safety.SqliteActionStore(path, secret=store_secret), idempotency=idempotency
                )
            run.mail.lose_next_response = step.mail_times_out
            tool = email_tool(guard, run.mail, "T-1001", run.results)
            run.replies.append(tool(to=step.to, template=step.template))  # the model's tool call
            print(f"{number:>2}  {step.process} process: {step.note}")
            print(f"    send_email(to={step.to!r}, template={step.template!r})")
            print(f"    -> {explain(run.results[-1])}")
        assert guard is not None
        record = guard.inspect("send_email", "email:T-1001:refund_confirmation")
        run.stored_state = record.state if record else None
    sent = ", ".join(f"{m['template']} to {m['to']}" for m in run.mail.outbox.values())
    print(f"mail provider: {len(run.mail.outbox)} email(s) sent ({sent})")
    print(f"               {run.mail.send_calls} send call(s), {run.mail.status_calls} status lookup(s)")
    return run


if __name__ == "__main__":
    main()
