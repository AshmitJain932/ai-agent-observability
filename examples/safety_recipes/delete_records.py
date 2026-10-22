"""Recipe: an agent's ``delete_records`` tool that deletes only the requesting account's own,
existing records, asks a person for larger deletions and refuses bulk ones.

    uv run python examples/safety_recipes/delete_records.py

A support agent handles data-deletion requests from signed-in customers. The model names the
records to delete; everything else comes from trusted systems. Behind the guard:

* every record must exist and belong to the account that asked (the signed-in session's account,
  compared with the owner the CRM reports), so the model cannot delete another customer's data;
* more than 25 records need a person (the privacy officer's callback must return ``True``), and
  more than 100 are refused (they go through the bulk-erasure process);
* each deletion request runs once: the action key names the request, so it cannot start again
  while it runs or after it succeeded, and a repeat after it is denied anyway because the
  records no longer exist.

Everything is local: the CRM is a fake in memory and the store lives in a temporary directory.
No data is deleted except from the fake.
"""

from __future__ import annotations

import secrets
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from aobs import safety

POLICY = Path(__file__).resolve().parent / "delete_records_policy.toml"


@dataclass
class FakeCRM:
    """Stands in for the system of record: record id -> owning account."""

    owners: dict[str, str] = field(
        default_factory=lambda: {
            **{f"acme-{n:03}": "acme" for n in range(1, 201)},
            **{f"globex-{n:03}": "globex" for n in range(1, 6)},
        }
    )
    delete_calls: int = 0
    deleted: list[str] = field(default_factory=list)

    def owner_of(self, record_id: str) -> str | None:
        return self.owners.get(record_id)

    def delete(self, account_id: str, record_ids: list[str]) -> dict[str, int]:
        """All or nothing, checked again at the system of record: if any record is not (or no
        longer) the account's, nothing is deleted and the guard is told so (a definite failure)."""
        self.delete_calls += 1
        if any(self.owners.get(r) != account_id for r in record_ids):
            raise safety.ActionNotPerformed("records changed since the check")
        for r in record_ids:
            del self.owners[r]
        self.deleted.extend(record_ids)
        return {"deleted": len(record_ids)}


@dataclass
class PrivacyOfficer:
    """The approver: answers in order (a person, in a real deployment). Sees only the checked request."""

    answers: list[bool] = field(default_factory=list)
    asked: list[int] = field(default_factory=list)  # how many records each request was for

    def approve(self, ctx: safety.ActionContext) -> bool:
        self.asked.append(ctx.request["count"])
        return self.answers.pop(0) if self.answers else False  # nobody answered: not approved


def evidence_for(crm: FakeCRM, record_ids: list[str]) -> dict[str, Any]:
    """From the CRM: which account owns the named records, and how many of them exist. The model
    only names the records; the facts about them come from the CRM."""
    owners = {crm.owner_of(r) for r in record_ids} - {None}
    if len(owners) > 1:
        owner: str | None = "(several accounts)"
    else:
        owner = owners.pop() if owners else None
    return {"account_id": owner, "count": sum(1 for r in record_ids if crm.owner_of(r) is not None)}


def delete_tool(
    guard: safety.ActionGuard,
    crm: FakeCRM,
    officer: PrivacyOfficer,
    session_account: str,
    request_id: str,
    results: list[safety.ActionResult],
) -> Callable[..., dict[str, Any]]:
    """The agent's ``delete_records`` tool for one deletion request by a signed-in customer.
    ``session_account`` comes from the authenticated session, never from the model."""

    def delete_records(record_ids: list[str]) -> dict[str, Any]:
        if not isinstance(record_ids, list) or not all(isinstance(r, str) for r in record_ids):
            return {"status": "invalid_arguments"}
        ids = sorted(set(record_ids))
        result = guard.run(
            "delete_customer_records",
            {"account_id": session_account, "record_ids": ids, "count": len(ids)},
            evidence_for(crm, ids),
            lambda ctx: crm.delete(ctx.request["account_id"], list(ctx.request["record_ids"])),
            action_key=f"delete:{request_id}",  # the deletion request, from the support system
            approver=officer.approve,
        )
        results.append(result)
        if result.final_outcome == "SUCCEEDED":
            return {"status": "deleted", "count": result.value["deleted"]}
        if result.previous_outcome == "SUCCEEDED":
            return {"status": "already_deleted"}
        if result.decision.verdict == "REQUIRE_APPROVAL":
            return {"status": "not_approved"}
        if result.outcome == "NOT_EXECUTED":
            return {"status": "not_allowed", "reasons": list(result.decision.reason_codes)}
        if result.final_outcome == "FAILED":
            return {"status": "not_deleted"}
        return {"status": "outcome_unknown", "note": "escalated to a person; do not retry"}

    return delete_records


def acme(first: int, last: int) -> list[str]:
    return [f"acme-{n:03}" for n in range(first, last + 1)]


class Step(NamedTuple):
    request: str  # the deletion request (ticket) id
    note: str
    record_ids: list[str]
    approval: bool | None = None  # the privacy officer's answer, if asked


SESSION_ACCOUNT = "acme"  # the signed-in customer who asked
STEPS = [
    Step("DSR-1", "the agent deletes three of the customer's records", acme(1, 3)),
    Step("DSR-1", "it loops and asks again: the records are gone", acme(1, 3)),
    Step("DSR-2", "one of the records belongs to another account", ["acme-010", "globex-002"]),
    Step("DSR-2", "one of the records does not exist (already deleted)", ["acme-003", "acme-010"]),
    Step("DSR-3", "40 records; the privacy officer declines", acme(4, 43), approval=False),
    Step("DSR-3", "40 records again; this time approved", acme(4, 43), approval=True),
    Step("DSR-4", "120 records: above the limit", acme(51, 170)),
]


def show(ids: list[str]) -> str:
    return repr(ids) if len(ids) <= 3 else f"['{ids[0]}', ..., '{ids[-1]}'] ({len(ids)} ids)"


def explain(result: safety.ActionResult) -> str:
    """One line: what happened, and the reason codes that decided it."""
    d = result.decision
    codes = ", ".join(d.reason_codes)
    if result.final_outcome == "SUCCEEDED":
        approved = ", approved" if result.approval == "granted" else ""
        return f"DELETED  {d.verdict} ({codes}{approved})"
    if result.previous_outcome is not None:
        return f"BLOCKED  {codes} [{d.rule}]: earlier attempt {result.previous_outcome}, not repeated"
    if d.verdict == "REQUIRE_APPROVAL":
        return f"BLOCKED  {d.verdict} ({codes}), approval {result.approval}"
    if result.outcome == "NOT_EXECUTED":
        return f"BLOCKED  {d.verdict} {codes} [{d.rule}]"
    return f"{result.final_outcome}  {d.verdict} ({codes}), next: {result.recovery}"


@dataclass
class Run:
    crm: FakeCRM = field(default_factory=FakeCRM)
    officer: PrivacyOfficer = field(default_factory=PrivacyOfficer)
    results: list[safety.ActionResult] = field(default_factory=list)
    replies: list[dict[str, Any]] = field(default_factory=list)  # what the model was told


def main() -> Run:
    run = Run()
    policy = safety.load_policy(POLICY)
    # In production: two stable random secrets (32+ bytes each) from a secret manager, the same in
    # every process and after every restart. Generated here because the store is temporary.
    store_secret = secrets.token_bytes(32)
    idempotency = safety.IdempotencyKeys(namespace="support-agent/recipes", secret=secrets.token_bytes(32))
    print(f"delete_records: only {SESSION_ACCOUNT}'s own, existing records; approval above 25, refused above 100")
    with tempfile.TemporaryDirectory() as tmp:
        store = safety.SqliteActionStore(Path(tmp) / "actions.db", secret=store_secret)
        guard = safety.ActionGuard(policy, store=store, idempotency=idempotency)
        for number, step in enumerate(STEPS, 1):
            run.officer.answers = [] if step.approval is None else [step.approval]
            tool = delete_tool(guard, run.crm, run.officer, SESSION_ACCOUNT, step.request, run.results)
            run.replies.append(tool(record_ids=step.record_ids))  # the model's tool call
            print(f"{number:>2}  {step.request}: {step.note}")
            print(f"    delete_records(record_ids={show(step.record_ids)})")
            print(f"    -> {explain(run.results[-1])}")
    print(f"CRM: {len(run.crm.deleted)} record(s) deleted, all {SESSION_ACCOUNT}'s, in {run.crm.delete_calls} call(s)")
    print(f"privacy officer: asked {len(run.officer.asked)} time(s)")
    return run


if __name__ == "__main__":
    main()
