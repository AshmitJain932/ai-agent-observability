"""aobs.safety: action identity, duplicates that report what happened, executing exactly what was
checked, synchronous-only enforcement, interrupted runs, store failures, reconciling unresolved
actions, and keeping keys out of the trace. (Store mechanics and processes: test_sdk_safety_store.)"""

from __future__ import annotations

import gc
import importlib
import sys
import threading
import time
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import aobs
from aobs import safety
from aobs.export import InMemoryExporter
from sdk_support import REPO_ROOT, flush, traces

EXAMPLE_DIR = REPO_ROOT / "examples" / "support_agent"
RULE = {
    "required_evidence": ["order_id", "amount", "status"],
    "must_match": ["order_id"],
    "amount": "amount",
    "amount_at_most_evidence": True,
    "max_amount": "200.00",
    "approval_above": "100.00",
    "once_per": ["order_id"],
}
POLICY = safety.parse_policy({"version": 1, "actions": {"issue_refund": RULE}})
ORDER = {"order_id": "1042", "amount": 59.90, "status": "delivered"}
BIG_ORDER = {"order_id": "2077", "amount": 150.00, "status": "delivered"}
KEY = "refund:T-1"
SECRET = bytes(range(32))  # a fixed test secret; never printed
IDEM_SECRET = bytes(range(64, 96))  # a fixed test secret for provider idempotency keys; never printed
IDEM = safety.IdempotencyKeys(namespace="tests/local", secret=IDEM_SECRET)


def Guard(*args: Any, **options: Any) -> safety.ActionGuard:  # stands in for the class
    """An ActionGuard with the tests' idempotency configuration, unless one is given."""
    options.setdefault("idempotency", IDEM)
    return safety.ActionGuard(*args, **options)


def refund(amount: float = 59.90, order_id: str = "1042") -> dict[str, Any]:
    return {"order_id": order_id, "amount": amount}


class Spy:
    """An action (or lookup) that counts its calls and behaves as told."""

    def __init__(self, raises: BaseException | None = None, value: Any = "done") -> None:
        self.calls: list[safety.ActionContext] = []
        self.raises = raises
        self.value = value

    def __call__(self, ctx: safety.ActionContext) -> Any:
        self.calls.append(ctx)
        if self.raises is not None:
            raise self.raises
        return self.value


@pytest.fixture
def guard() -> safety.ActionGuard:
    return Guard(POLICY)


@pytest.fixture
def payments() -> Iterator[ModuleType]:
    sys.path.insert(0, str(EXAMPLE_DIR))
    try:
        sys.modules.pop("payments", None)
        yield importlib.import_module("payments")
    finally:
        sys.path.remove(str(EXAMPLE_DIR))


# --------------------------------------------------------------------------- identity


def test_the_idempotency_key_is_the_same_for_every_attempt_at_one_logical_action() -> None:
    first, second = Spy(raises=safety.ActionNotPerformed()), Spy()
    Guard(POLICY).run("issue_refund", refund(), ORDER, first, action_key=KEY)
    Guard(POLICY).run("issue_refund", refund(), ORDER, second, action_key=KEY)  # another process
    key = first.calls[0].idempotency_key
    assert key == second.calls[0].idempotency_key == IDEM.key("issue_refund", KEY)
    assert first.calls[0].action_id != second.calls[0].action_id  # attempts stay distinct for tracing
    assert "T-1" not in key and key != IDEM.key("issue_refund", "refund:T-2")
    unkeyed = [Spy(), Spy()]
    for n, spy in enumerate(unkeyed):
        Guard(POLICY).run("issue_refund", refund(order_id=str(n)), {**ORDER, "order_id": str(n)}, spy)
    assert unkeyed[0].calls[0].idempotency_key != unkeyed[1].calls[0].idempotency_key  # per attempt without a key


def test_repeating_a_completed_action_reports_that_it_was_done(guard: safety.ActionGuard) -> None:
    execute = Spy()
    first = guard.run("issue_refund", refund(), ORDER, execute, action_key=KEY)
    again = guard.run("issue_refund", refund(), ORDER, execute, action_key=KEY)
    assert (again.outcome, again.decision.reason_codes, again.decision.rule) == (
        "NOT_EXECUTED", ("duplicate_action",), "guard.action_key",
    )  # fmt: skip
    assert again.previous_outcome == "SUCCEEDED" and again.duplicate_of == first.action_id
    assert len(execute.calls) == 1


def test_the_same_action_key_with_a_changed_amount_is_refused(guard: safety.ActionGuard) -> None:
    execute = Spy()
    guard.run("issue_refund", refund(), ORDER, execute, action_key=KEY)
    changed = guard.run("issue_refund", refund(10.0), ORDER, execute, action_key=KEY)
    assert changed.decision.reason_codes == ("action_key_conflict",) and changed.previous_outcome == "SUCCEEDED"
    failed = Guard(POLICY)
    failed.run("issue_refund", refund(), ORDER, Spy(raises=safety.ActionNotPerformed()), action_key=KEY)
    after_failure = failed.run("issue_refund", refund(10.0), ORDER, execute, action_key=KEY)
    assert after_failure.decision.reason_codes == ("action_key_conflict",)  # even after FAILED: a new key, please
    assert len(execute.calls) == 1


def test_a_legitimate_second_refund_is_a_new_trusted_case() -> None:
    per_case = {**RULE, "required_evidence": [*RULE["required_evidence"], "case_id"],
                "must_match": ["order_id", "case_id"], "once_per": ["order_id", "case_id"]}  # fmt: skip
    guard = Guard(safety.parse_policy({"version": 1, "actions": {"issue_refund": per_case}}))
    execute = Spy()
    for case in ("C-1", "C-2"):  # the case id comes from the ticket system, and the model's request must match it
        evidence = {**ORDER, "amount": 30.0, "case_id": case}
        result = guard.run(
            "issue_refund", {**refund(25.0), "case_id": case}, evidence, execute, action_key=f"refund:{case}"
        )
        assert result.outcome == "SUCCEEDED"
    repeat = guard.run("issue_refund", {**refund(25.0), "case_id": "C-1"}, {**ORDER, "case_id": "C-1"}, execute,
                       action_key="refund:C-1")  # fmt: skip
    assert repeat.previous_outcome == "SUCCEEDED"
    other_key = guard.run("issue_refund", {**refund(25.0), "case_id": "C-1"}, {**ORDER, "case_id": "C-1"}, execute,
                          action_key="refund:C-1-again")  # fmt: skip
    assert other_key.decision.rule == "actions.issue_refund.once_per"  # a new key alone is not a new case
    forged = guard.run("issue_refund", {**refund(25.0), "case_id": "C-3"}, {**ORDER, "case_id": "C-1"}, execute,
                       action_key="refund:C-3")  # fmt: skip
    assert forged.decision.reason_codes == ("evidence_mismatch",)  # the model cannot invent a case
    assert len(execute.calls) == 2


def test_once_per_may_only_use_verified_fields() -> None:
    with pytest.raises(safety.PolicyError, match="once_per fields must also be in must_match"):
        safety.parse_policy({"version": 1, "actions": {"a": {"required_evidence": ["x", "y"], "must_match": ["x"],
                                                             "once_per": ["y"]}}})  # fmt: skip


@pytest.mark.parametrize("key", ["", " refund:T-1", "refund:T-1 ", "refund\nT-1", "x" * 257, 1042, b"refund"])
def test_invalid_action_keys_are_denied(guard: safety.ActionGuard, key: Any) -> None:
    execute = Spy()
    result = guard.run("issue_refund", refund(), ORDER, execute, action_key=key)
    assert result.decision.reason_codes == ("action_key_invalid",) and execute.calls == []


def test_a_durable_store_requires_an_action_key(tmp_path: Path) -> None:
    guard = Guard(POLICY, store=safety.SqliteActionStore(tmp_path / "actions.db", secret=SECRET))
    execute = Spy()
    result = guard.run("issue_refund", refund(), ORDER, execute)
    assert result.decision.reason_codes == ("action_key_required",) and execute.calls == []


# --------------------------------------------------------------------------- what "unknown" means


def test_an_early_not_found_is_not_a_failure_and_the_late_refund_is_not_repeated(payments: ModuleType) -> None:
    provider = payments.MockPayments("timeout_pending")
    guard = Guard(POLICY)

    def run() -> safety.ActionResult:
        return guard.run("issue_refund", refund(), ORDER,
                         lambda ctx: provider.refund("1042", 59.90, ctx.idempotency_key),
                         action_key=KEY, verify=lambda ctx: provider.status(ctx.idempotency_key))  # fmt: skip

    first = run()
    assert (first.outcome, first.verified, first.recovery) == ("UNKNOWN", "UNKNOWN", "escalate")  # PENDING
    assert run().previous_outcome == "UNKNOWN"  # the agent asks again: not repeated
    provider.complete_pending()  # the provider finishes the original request
    assert provider.refund_calls == 1 and provider.refunds_for("1042") == 1
    settled = guard.reconcile("issue_refund", KEY, verify=lambda ctx: provider.status(ctx.idempotency_key))
    assert settled is not None and (settled.state, settled.resolution) == ("SUCCEEDED", "verified")


@pytest.mark.parametrize("reported", ["NOT_FOUND", "PENDING", "failed", None, ["FAILED"]])
def test_only_an_explicit_failed_from_the_lookup_counts_as_a_failure(guard: safety.ActionGuard, reported: Any) -> None:
    result = guard.run("issue_refund", refund(), ORDER, Spy(raises=TimeoutError()), action_key=KEY,
                       verify=lambda ctx: reported)  # fmt: skip
    assert (result.final_outcome, result.recovery) == ("UNKNOWN", "escalate")
    assert guard.inspect("issue_refund", KEY).state == "UNKNOWN"  # type: ignore[union-attr]


def test_approval_is_not_evidence_of_success(guard: safety.ActionGuard) -> None:
    result = guard.run("issue_refund", refund(150.0, "2077"), BIG_ORDER, Spy(raises=TimeoutError()),
                       action_key=KEY, approver=lambda ctx: True)  # fmt: skip
    assert (result.approval, result.final_outcome) == ("granted", "UNKNOWN")


# --------------------------------------------------------------------------- exactly what was checked


def test_the_action_receives_a_read_only_copy_of_the_checked_request() -> None:
    seen: list[Any] = []

    def execute(ctx: safety.ActionContext) -> str:
        seen.append(dict(ctx.request))
        with pytest.raises(TypeError):
            ctx.request["amount"] = 5000  # type: ignore[index]
        return "done"

    big = refund(150.0, "2077")
    result = Guard(POLICY).run(
        "issue_refund", big, BIG_ORDER, execute, approver=lambda ctx: big.update(amount=5000.0) or True
    )  # the caller's dict changes after the check: that is not what runs
    assert result.outcome == "SUCCEEDED" and seen == [refund(150.0, "2077")]


def test_a_nested_value_changed_after_the_check_is_not_executed(guard: safety.ActionGuard) -> None:
    execute = Spy()

    def approver(ctx: safety.ActionContext) -> bool:
        ctx.request["items"].append("tv")  # nested values are copies, but still mutable
        return True

    result = guard.run("issue_refund", {**refund(150.0, "2077"), "items": ["cable"]}, BIG_ORDER, execute,
                       action_key=KEY, approver=approver)  # fmt: skip
    assert result.decision.reason_codes == ("request_changed",) and execute.calls == []
    assert guard.inspect("issue_refund", KEY) is None  # released: it never ran
    unkeyed = Guard(safety.parse_policy({"version": 1, "actions": {"issue_refund": {
        **RULE, "once_per": []}}}))  # fmt: skip
    result = unkeyed.run("issue_refund", {**refund(150.0, "2077"), "items": ["cable"]}, BIG_ORDER, execute,
                         approver=approver)  # fmt: skip
    assert result.decision.reason_codes == ("request_changed",) and execute.calls == []  # with no store record too


@pytest.mark.parametrize("value", [object(), {1: "x"}, {"x": {1, 2}}, Path("/tmp")])
def test_requests_that_are_not_plain_data_are_denied(guard: safety.ActionGuard, value: Any) -> None:
    execute = Spy()
    result = guard.run("issue_refund", {**refund(), "note": value}, ORDER, execute)
    assert result.decision.reason_codes == ("invalid_input",) and execute.calls == []


# --------------------------------------------------------------------------- synchronous only


async def _async_refund(ctx: safety.ActionContext) -> str:
    return "done"


def _generator_refund(ctx: safety.ActionContext) -> Iterator[str]:
    yield "done"


class _AsyncCallable:
    async def __call__(self, ctx: safety.ActionContext) -> str:
        return "done"


@pytest.mark.parametrize("fn", [_async_refund, _generator_refund, _AsyncCallable()])
@pytest.mark.parametrize("role", ["execute", "verify", "approver"])
def test_async_and_generator_functions_are_rejected_before_anything_runs(fn: Any, role: str) -> None:
    guard, execute = Guard(POLICY), Spy()
    kwargs = {role: fn} if role != "execute" else {}
    with pytest.raises(TypeError, match="must be a synchronous function"):
        guard.run("issue_refund", refund(), ORDER, fn if role == "execute" else execute, action_key=KEY, **kwargs)
    assert execute.calls == [] and guard.inspect("issue_refund", KEY) is None
    with pytest.raises(TypeError, match="must be a synchronous function"):
        guard.protect(fn, action="issue_refund", evidence=lambda: ORDER)


class _Awaitable:
    def __await__(self) -> Iterator[None]:
        yield None


def test_an_awaitable_returned_by_execute_never_runs_and_is_not_a_success(guard: safety.ActionGuard) -> None:
    ran: list[str] = []

    async def refund_later() -> str:
        ran.append("refund")
        return "done"

    def gen() -> Iterator[str]:
        ran.append("refund")
        yield "done"

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # a coroutine left unawaited would warn when collected
        cases = [(refund_later, "AsyncActionNotSupported"), (gen, "GeneratorActionNotSupported"),
                 (_Awaitable, "AsyncActionNotSupported")]  # fmt: skip
        for n, (make, error) in enumerate(cases):
            result = guard.run("issue_refund", refund(order_id=str(n)), {**ORDER, "order_id": str(n)},
                               lambda ctx, make=make: make(), action_key=f"refund:{n}")  # fmt: skip
            assert (result.outcome, result.value, result.error_type) == ("UNKNOWN", None, error)
            assert result.recovery == "escalate"  # the function itself may have acted before returning it
        gc.collect()
    assert ran == []


# --------------------------------------------------------------------------- interruptions and store failures


def test_an_interrupted_execution_is_recorded_as_unknown(guard: safety.ActionGuard) -> None:
    with pytest.raises(KeyboardInterrupt):
        guard.run("issue_refund", refund(), ORDER, Spy(raises=KeyboardInterrupt()), action_key=KEY)
    record = guard.inspect("issue_refund", KEY)
    assert record is not None and (record.state, record.error_type) == ("UNKNOWN", "KeyboardInterrupt")
    execute = Spy()
    assert guard.run("issue_refund", refund(), ORDER, execute, action_key=KEY).previous_outcome == "UNKNOWN"
    assert execute.calls == []


def test_an_interrupted_approval_releases_the_reservation(guard: safety.ActionGuard) -> None:
    def interrupted(ctx: safety.ActionContext) -> bool:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        guard.run("issue_refund", refund(150.0, "2077"), BIG_ORDER, Spy(), action_key=KEY, approver=interrupted)
    assert guard.inspect("issue_refund", KEY) is None  # it never ran, so nothing blocks a new request
    retry = guard.run("issue_refund", refund(150.0, "2077"), BIG_ORDER, Spy(), action_key=KEY, approver=lambda c: True)
    assert retry.outcome == "SUCCEEDED"


class FlakyStore(safety.MemoryActionStore):
    """A memory store whose operations fail on demand."""

    def __init__(self) -> None:
        super().__init__()
        self.fail: set[str] = set()

    def reserve(self, claim: safety.ActionRecord, now: float, **kwargs: Any) -> safety.ActionRecord | None:
        if "reserve" in self.fail:
            raise OSError("database is locked")
        return super().reserve(claim, now, **kwargs)

    def update(self, action: str, key: str, attempt_id: str, **kwargs: Any) -> Any:
        if kwargs["state"] in self.fail:
            raise OSError("disk I/O error")
        return super().update(action, key, attempt_id, **kwargs)


@pytest.mark.parametrize("failing", ["reserve", "EXECUTING"])
def test_when_the_store_is_unavailable_nothing_runs(failing: str) -> None:
    store = FlakyStore()
    store.fail.add(failing)
    guard, execute = Guard(POLICY, store=store), Spy()
    result = guard.run("issue_refund", refund(), ORDER, execute, action_key=KEY)
    assert (result.outcome, result.decision.reason_codes, result.store_error) == (
        "NOT_EXECUTED", ("store_unavailable",), "OSError",
    )  # fmt: skip
    assert execute.calls == [] and guard.inspect("issue_refund", KEY) is None  # the reservation was released


def test_a_lost_outcome_record_keeps_blocking_until_reconciled() -> None:
    store = FlakyStore()
    store.fail.add("SUCCEEDED")
    guard, execute = Guard(POLICY, store=store), Spy()
    result = guard.run("issue_refund", refund(), ORDER, execute, action_key=KEY)
    assert (result.outcome, result.store_error) == ("SUCCEEDED", "OSError")  # it happened; the record did not
    assert guard.run("issue_refund", refund(), ORDER, execute, action_key=KEY).previous_outcome == "EXECUTING"
    store.fail.clear()
    settled = guard.reconcile("issue_refund", KEY, verify=lambda ctx: "SUCCEEDED")
    assert settled is not None and settled.state == "SUCCEEDED" and len(execute.calls) == 1


def test_an_unavailable_sqlite_file_fails_closed(tmp_path: Path) -> None:
    directory = tmp_path / "gone"
    directory.mkdir()
    guard = Guard(POLICY, store=safety.SqliteActionStore(directory / "actions.db", secret=SECRET))
    for f in directory.iterdir():
        f.unlink()
    directory.rmdir()
    execute = Spy()
    result = guard.run("issue_refund", refund(), ORDER, execute, action_key=KEY)
    assert result.decision.reason_codes == ("store_unavailable",) and execute.calls == []


# --------------------------------------------------------------------------- leases


def test_an_abandoned_reservation_expires_but_an_executing_action_never_does() -> None:
    store, execute = safety.MemoryActionStore(), Spy()
    slow, fast = Guard(POLICY, store=store, lease_s=0.05), Guard(POLICY, store=store)
    others: list[safety.ActionResult] = []

    def slow_approver(ctx: safety.ActionContext) -> bool:  # a person who answers after the lease ran out
        time.sleep(0.1)
        others.append(fast.run("issue_refund", refund(150.0, "2077"), BIG_ORDER, execute, action_key=KEY,
                               approver=lambda c: True))  # fmt: skip
        return True

    late = slow.run("issue_refund", refund(150.0, "2077"), BIG_ORDER, execute, action_key=KEY, approver=slow_approver)
    assert others[0].outcome == "SUCCEEDED"  # the expired reservation never ran, so it could be replaced
    assert (late.outcome, late.previous_outcome) == ("NOT_EXECUTED", "SUCCEEDED") and len(execute.calls) == 1

    blocked: list[safety.ActionResult] = []

    def long_running(ctx: safety.ActionContext) -> str:
        time.sleep(0.1)  # longer than the lease: executing is never treated as abandoned
        blocked.append(fast.run("issue_refund", refund(), ORDER, Spy(), action_key="refund:T-2"))
        return "done"

    assert slow.run("issue_refund", refund(), ORDER, long_running, action_key="refund:T-2").outcome == "SUCCEEDED"
    assert blocked[0].previous_outcome == "EXECUTING"


# --------------------------------------------------------------------------- reconcile and resolve


def test_reconcile_settles_an_unknown_action_with_one_read_only_lookup(guard: safety.ActionGuard) -> None:
    execute, lookup = Spy(raises=TimeoutError()), Spy(value="SUCCEEDED")
    first = guard.run("issue_refund", refund(), ORDER, execute, action_key=KEY)
    settled = guard.reconcile("issue_refund", KEY, verify=lookup)
    assert settled is not None and (settled.state, settled.resolution, settled.attempt_id) == (
        "SUCCEEDED", "verified", first.action_id,
    )  # fmt: skip
    assert len(lookup.calls) == 1 and len(execute.calls) == 1
    assert lookup.calls[0].idempotency_key == IDEM.key("issue_refund", KEY)
    assert lookup.calls[0].request == {}
    assert guard.run("issue_refund", refund(), ORDER, execute, action_key=KEY).previous_outcome == "SUCCEEDED"
    assert guard.reconcile("issue_refund", KEY, verify=lookup) == settled and len(lookup.calls) == 1  # settled
    assert guard.reconcile("issue_refund", "refund:none", verify=lookup) is None


def test_an_inconclusive_unknown_needs_a_person_and_failed_allows_a_new_attempt(guard: safety.ActionGuard) -> None:
    guard.run("issue_refund", refund(), ORDER, Spy(raises=TimeoutError()), action_key=KEY)
    assert guard.reconcile("issue_refund", KEY, verify=Spy(raises=ConnectionError())).state == "UNKNOWN"  # type: ignore[union-attr]
    resolved = guard.resolve("issue_refund", KEY, "FAILED")  # checked with the provider by other means
    assert (resolved.state, resolved.resolution) == ("FAILED", "manual")
    retry = Spy()
    assert guard.run("issue_refund", refund(), ORDER, retry, action_key=KEY).outcome == "SUCCEEDED"
    assert retry.calls[0].idempotency_key == IDEM.key("issue_refund", KEY)  # the same key again


def test_resolve_only_settles_unknown_actions(guard: safety.ActionGuard) -> None:
    with pytest.raises(LookupError):
        guard.resolve("issue_refund", KEY, "FAILED")
    guard.run("issue_refund", refund(), ORDER, Spy(), action_key=KEY)
    with pytest.raises(ValueError, match="this one is SUCCEEDED"):
        guard.resolve("issue_refund", KEY, "FAILED")
    with pytest.raises(ValueError, match="SUCCEEDED"):
        guard.resolve("issue_refund", KEY, "maybe")  # type: ignore[arg-type]
    store = FlakyStore()
    store.fail.add("SUCCEEDED")  # leaves the record EXECUTING
    stuck = Guard(POLICY, store=store)
    stuck.run("issue_refund", refund(), ORDER, Spy(), action_key=KEY)
    with pytest.raises(ValueError, match="still considered running"):
        stuck.resolve("issue_refund", KEY, "SUCCEEDED")
    later = Guard(POLICY, store=store, clock=lambda: time.time() + 3600)
    with pytest.raises(ValueError, match="reconcile"):
        later.resolve("issue_refund", KEY, "SUCCEEDED")  # stale, but still EXECUTING: reconcile first


# --------------------------------------------------------------------------- protect()


def test_protect_wraps_a_tool_without_changing_how_it_is_called() -> None:
    calls: list[dict[str, Any]] = []

    def issue_refund(order_id: str, amount: float, idempotency_key: str = "", note: str = "none") -> dict[str, Any]:
        """Refund an order."""
        calls.append({"order_id": order_id, "amount": amount, "idempotency_key": idempotency_key, "note": note})
        return {"reference": f"rf_{order_id}"}

    guard = Guard(POLICY)
    refund_tool = guard.protect(issue_refund, action="issue_refund", evidence=lambda: ORDER, action_key=KEY,
                                idempotency_param="idempotency_key")  # fmt: skip
    assert refund_tool.__name__ == "issue_refund" and refund_tool.__doc__ == "Refund an order."
    import inspect

    assert list(inspect.signature(refund_tool).parameters) == ["order_id", "amount", "note"]
    assert refund_tool("1042", amount=59.90) == {"reference": "rf_1042"}
    assert calls == [{"order_id": "1042", "amount": 59.90, "note": "none",
                      "idempotency_key": IDEM.key("issue_refund", KEY)}]  # fmt: skip
    with pytest.raises(TypeError):
        refund_tool("1042", 59.90, idempotency_key="chosen-by-the-model")
    with pytest.raises(safety.ActionBlocked) as blocked:
        refund_tool("1042", 59.90)
    assert blocked.value.result.previous_outcome == "SUCCEEDED" and len(calls) == 1
    assert "1042" not in str(blocked.value) and "59.9" not in str(blocked.value)
    assert str(blocked.value) == "issue_refund: NOT_EXECUTED (duplicate_action; earlier attempt SUCCEEDED)"


@pytest.mark.parametrize(
    ("execute", "final"),
    [
        (safety.ActionNotPerformed("declined"), "FAILED"),
        (TimeoutError("no response"), "UNKNOWN"),
    ],
)
def test_protect_never_turns_a_failure_or_an_unknown_outcome_into_a_tool_result(execute: Exception, final: str) -> None:
    def issue_refund(order_id: str, amount: float) -> str:
        raise execute

    refund_tool = Guard(POLICY).protect(issue_refund, action="issue_refund", evidence=lambda: ORDER)
    with pytest.raises(safety.ActionBlocked) as blocked:
        refund_tool("1042", 59.90)
    assert blocked.value.result.final_outcome == final


def test_protect_blocks_denied_calls_and_fails_closed_on_bad_context() -> None:
    called: list[Any] = []

    def issue_refund(order_id: str, amount: float) -> str:
        called.append(order_id)
        return "done"

    guard = Guard(POLICY)
    tool = guard.protect(issue_refund, action="issue_refund", evidence=lambda: ORDER)
    with pytest.raises(safety.ActionBlocked) as denied:
        tool("2077", 59.90)
    assert denied.value.result.decision.reason_codes == ("evidence_mismatch",)

    def no_evidence() -> dict[str, Any]:
        raise ConnectionError("order system down")

    with pytest.raises(safety.ActionBlocked) as unavailable:
        guard.protect(issue_refund, action="issue_refund", evidence=no_evidence)("1042", 59.90)
    assert unavailable.value.result.decision.reason_codes == ("invalid_input",)

    def no_key() -> str:
        raise KeyError("ticket")

    with pytest.raises(safety.ActionBlocked) as keyless:
        guard.protect(issue_refund, action="issue_refund", evidence=lambda: ORDER, action_key=no_key)("1042", 59.90)
    assert keyless.value.result.decision.reason_codes == ("action_key_invalid",)
    assert called == []


def test_protect_reports_configuration_mistakes_before_any_call(tmp_path: Path) -> None:
    def tool(order_id: str, amount: float) -> str:
        return "done"

    def star(*args: Any) -> str:
        return "done"

    guard = Guard(POLICY)
    with pytest.raises(safety.PolicyError, match="not in the safety policy"):
        guard.protect(tool, action="issue_refnd", evidence=lambda: ORDER)
    with pytest.raises(TypeError, match="no parameter 'key'"):
        guard.protect(tool, action="issue_refund", evidence=lambda: ORDER, idempotency_param="key")
    with pytest.raises(TypeError, match="named parameters"):
        guard.protect(star, action="issue_refund", evidence=lambda: ORDER)
    with pytest.raises(ValueError, match="action_key"):
        guard.protect(tool, action="issue_refund", evidence=lambda: ORDER, action_key=" spaced ")
    durable = Guard(POLICY, store=safety.SqliteActionStore(tmp_path / "a.db", secret=SECRET))
    with pytest.raises(TypeError, match="needs action_key"):
        durable.protect(tool, action="issue_refund", evidence=lambda: ORDER)
    with pytest.raises(TypeError, match="SafetyPolicy"):
        Guard({"version": 1})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="lease_s"):
        Guard(POLICY, lease_s=0)


# --------------------------------------------------------------------------- the trace


def test_action_keys_and_idempotency_keys_never_reach_the_trace(sdk: InMemoryExporter, tmp_path: Path) -> None:
    secret_key = "refund:T-SECRET-7731"
    guard = Guard(POLICY, store=safety.SqliteActionStore(tmp_path / "actions.db", secret=SECRET))
    with aobs.run("answer_ticket"):
        first = guard.run("issue_refund", refund(), ORDER, Spy(raises=TimeoutError()), action_key=secret_key)
        guard.run("issue_refund", refund(), ORDER, Spy(), action_key=secret_key)
        guard.reconcile("issue_refund", secret_key, verify=lambda ctx: "PENDING")
        guard.resolve("issue_refund", secret_key, "SUCCEEDED")
    flush()
    text = "".join(body.decode() for body in sdk.bodies)
    record = guard.inspect("issue_refund", secret_key)
    assert record is not None
    for secret in ("T-SECRET-7731", IDEM.key("issue_refund", secret_key), record.key,
                   record.request_digest, str(record.once_key), "59.9"):  # fmt: skip
        assert secret not in text, secret
    traces(sdk)  # schema-valid
    spans = [e for e in sdk.events() if e["type"] == "span_end" and e["kind"] == "guardrail"]
    by_name = {s["name"]: s["attributes"] for s in spans}
    assert set(by_name) == {"action issue_refund", "action issue_refund reconcile", "action issue_refund resolve"}
    assert by_name["action issue_refund resolve"]["aobs.safety.reconciled_action_id"] == first.action_id
    assert by_name["action issue_refund resolve"]["aobs.safety.resolution"] == "manual"
    run_attributes = [s["attributes"] for s in spans if s["name"] == "action issue_refund"]
    assert {a["aobs.safety.store"] for a in run_attributes} == {"sqlite"} and all(
        a["aobs.safety.keyed"] for a in run_attributes
    )
    assert sorted(a.get("aobs.safety.previous_outcome", "-") for a in run_attributes) == ["-", "UNKNOWN"]


@pytest.mark.parametrize("method", ["event", "set_attributes", "set_status", "set_error", "fingerprint_input"])
def test_failing_telemetry_does_not_weaken_durable_enforcement(
    sdk: InMemoryExporter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, method: str
) -> None:
    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("telemetry is broken")

    monkeypatch.setattr(aobs.Span, method, explode)
    guard, execute = (
        Guard(POLICY, store=safety.SqliteActionStore(tmp_path / "a.db", secret=SECRET)),
        Spy(raises=TimeoutError()),
    )
    assert guard.run("issue_refund", refund(), ORDER, execute).decision.reason_codes == ("action_key_required",)
    assert guard.run("issue_refund", refund(), ORDER, execute, action_key=KEY).outcome == "UNKNOWN"
    assert guard.run("issue_refund", refund(), ORDER, execute, action_key=KEY).previous_outcome == "UNKNOWN"
    assert guard.reconcile("issue_refund", KEY, verify=lambda ctx: "SUCCEEDED").state == "SUCCEEDED"  # type: ignore[union-attr]
    assert len(execute.calls) == 1


def test_the_guard_is_thread_safe_with_one_shared_key() -> None:
    guard, barrier, execute_calls = Guard(POLICY), threading.Barrier(8), []

    def execute(ctx: safety.ActionContext) -> str:
        execute_calls.append(ctx.action_id)
        time.sleep(0.05)
        return "done"

    results: list[safety.ActionResult] = []

    def worker() -> None:
        barrier.wait()
        results.append(guard.run("issue_refund", refund(), ORDER, execute, action_key=KEY))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(execute_calls) == 1 and sorted(r.outcome for r in results).count("SUCCEEDED") == 1


# --------------------------------------------------------------------------- live executions (stale_after_s)


class Clock:
    """An injectable clock: Unix time that moves only when told to."""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


Pair = Callable[..., tuple[safety.ActionGuard, safety.ActionGuard, Clock]]


@pytest.fixture(params=["memory", "sqlite"])
def pair(request: pytest.FixtureRequest, tmp_path: Path) -> Pair:
    """Two guards on one store, as two processes would be: the executing one, and an operator's.
    With SQLite each guard opens the file with its own store object (its own connections)."""
    clock = Clock()
    shared = safety.MemoryActionStore()

    def make_pair(stale_after_s: float = 900.0) -> tuple[safety.ActionGuard, safety.ActionGuard, Clock]:
        def make() -> safety.ActionGuard:
            store: safety.ActionStore = (
                shared if request.param == "memory" else safety.SqliteActionStore(tmp_path / "a.db", secret=SECRET)
            )
            return Guard(POLICY, store=store, clock=clock, stale_after_s=stale_after_s)

        return make(), make(), clock

    return make_pair


@pytest.mark.parametrize("reported", ["NOT_FOUND", "PENDING", "FAILED", None])
def test_a_live_execution_is_not_released_by_a_lookup(pair: Pair, reported: Any) -> None:
    worker, operator, clock = pair()
    executions: list[str] = []
    seen: dict[str, Any] = {}

    def execute(ctx: safety.ActionContext) -> str:  # still running: the request has not been sent yet
        clock.now += 899.0  # just under stale_after_s
        record = operator.reconcile("issue_refund", KEY, verify=lambda c: reported)
        seen["state"], seen["live"] = record.state, operator.is_live(record)  # type: ignore[union-attr]
        with pytest.raises(ValueError, match="still considered running"):
            operator.resolve("issue_refund", KEY, "FAILED")
        seen["other"] = operator.run("issue_refund", refund(), ORDER, Spy(), action_key=KEY)
        executions.append(ctx.action_id)
        return "done"

    first = worker.run("issue_refund", refund(), ORDER, execute, action_key=KEY)
    assert (seen["state"], seen["live"]) == ("EXECUTING", True)
    assert (seen["other"].outcome, seen["other"].previous_outcome) == ("NOT_EXECUTED", "EXECUTING")
    assert (first.outcome, first.superseded, len(executions)) == ("SUCCEEDED", False, 1)
    assert operator.inspect("issue_refund", KEY).state == "SUCCEEDED"  # type: ignore[union-attr]


def test_staleness_starts_exactly_at_stale_after_s(pair: Pair) -> None:
    worker, operator, clock = pair(stale_after_s=60.0)
    seen: list[Any] = []

    def execute(ctx: safety.ActionContext) -> str:  # the record is EXECUTING, started at clock.now
        clock.now += 59.999
        seen.append(operator.is_live(operator.inspect("issue_refund", KEY)))  # type: ignore[arg-type]
        seen.append(operator.reconcile("issue_refund", KEY, verify=lambda c: "NOT_FOUND").state)  # type: ignore[union-attr]
        clock.now += 0.001
        seen.append(operator.is_live(operator.inspect("issue_refund", KEY)))  # type: ignore[arg-type]
        stale = operator.reconcile("issue_refund", KEY, verify=lambda c: "NOT_FOUND")
        seen.append((stale.state, stale.resolution))  # type: ignore[union-attr]
        return "done"

    worker.run("issue_refund", refund(), ORDER, execute, action_key=KEY)
    assert seen == [True, "EXECUTING", False, ("UNKNOWN", "stale")]  # stale is UNKNOWN, never FAILED


def test_a_confirmed_success_is_recorded_for_a_live_execution_without_allowing_a_duplicate(pair: Pair) -> None:
    worker, operator, _ = pair()
    other: list[safety.ActionResult] = []

    def execute(ctx: safety.ActionContext) -> str:
        settled = operator.reconcile("issue_refund", KEY, verify=lambda c: "SUCCEEDED")
        assert settled is not None and (settled.state, settled.resolution) == ("SUCCEEDED", "verified")
        other.append(operator.run("issue_refund", refund(), ORDER, Spy(), action_key=KEY))
        return "done"

    first = worker.run("issue_refund", refund(), ORDER, execute, action_key=KEY)
    assert (other[0].outcome, other[0].previous_outcome) == ("NOT_EXECUTED", "SUCCEEDED")
    assert (first.outcome, first.superseded) == ("SUCCEEDED", False)  # the same outcome: nothing overwritten


def test_a_stale_execution_is_not_declared_dead_and_its_real_outcome_is_kept(pair: Pair) -> None:
    worker, operator, clock = pair()
    blocked: list[safety.ActionResult] = []

    def paused_then_resumes(ctx: safety.ActionContext) -> str:
        clock.now += 3600  # the process was paused, not dead
        stale = operator.reconcile("issue_refund", KEY, verify=lambda c: "NOT_FOUND")
        assert stale is not None and (stale.state, stale.resolution) == ("UNKNOWN", "stale")
        blocked.append(operator.run("issue_refund", refund(), ORDER, Spy(), action_key=KEY))
        return "done"  # it resumes and completes

    first = worker.run("issue_refund", refund(), ORDER, paused_then_resumes, action_key=KEY)
    assert blocked[0].previous_outcome == "UNKNOWN"  # stale still blocks: only a person may release it
    record = operator.inspect("issue_refund", KEY)
    assert record is not None and (record.state, record.resolution) == ("SUCCEEDED", "none")
    assert (first.outcome, first.superseded) == ("SUCCEEDED", False)


@pytest.mark.parametrize(
    ("resumed", "recorded", "resolution"),
    [
        (Spy(), "SUCCEEDED", "conflict"),  # it did happen: kept, so that nothing retries, and flagged
        (Spy(raises=TimeoutError()), "FAILED", "manual_stopped"),  # unknown never overwrites a decision
        (Spy(raises=safety.ActionNotPerformed()), "FAILED", "manual_stopped"),
    ],
)
def test_a_resumed_execution_cannot_overwrite_a_later_decision(
    pair: Pair, resumed: Spy, recorded: str, resolution: str
) -> None:
    worker, operator, clock = pair()

    def execute(ctx: safety.ActionContext) -> Any:
        clock.now += 3600
        operator.reconcile("issue_refund", KEY, verify=lambda c: "NOT_FOUND")
        # a person decides while the process is paused, wrongly stating that it has stopped
        operator.resolve("issue_refund", KEY, "FAILED", original_stopped=True)
        return resumed(ctx)

    first = worker.run("issue_refund", refund(), ORDER, execute, action_key=KEY)
    record = operator.inspect("issue_refund", KEY)
    assert record is not None and (record.state, record.resolution) == (recorded, resolution)
    assert first.final_outcome == first.stored_outcome == recorded  # the stored decision, consistently
    assert first.superseded is (resolution == "conflict" or first.attempt_outcome != recorded)


@pytest.mark.parametrize("honours_key", [True, False])
def test_after_a_takeover_only_provider_idempotency_stops_a_resumed_duplicate(
    pair: Pair, payments: ModuleType, honours_key: bool
) -> None:
    provider = payments.MockPayments()
    worker, operator, clock = pair()
    second: list[safety.ActionResult] = []

    def send(ctx: safety.ActionContext) -> Any:
        key = ctx.idempotency_key if honours_key else ctx.action_id  # a provider that ignores keys
        return provider.refund("1042", 59.90, key)

    def paused(ctx: safety.ActionContext) -> Any:
        clock.now += 3600
        operator.reconcile("issue_refund", KEY, verify=lambda c: "NOT_FOUND")
        operator.resolve("issue_refund", KEY, "FAILED", original_stopped=True)  # wrongly: it was only paused
        second.append(operator.run("issue_refund", refund(), ORDER, send, action_key=KEY))
        return send(ctx)  # the original resumes and sends its request

    first = worker.run("issue_refund", refund(), ORDER, paused, action_key=KEY)
    assert second[0].outcome == "SUCCEEDED" and first.superseded  # the store cannot fence the provider
    assert (first.attempt_outcome, first.stored_outcome, first.final_outcome) == ("SUCCEEDED", "SUCCEEDED", "SUCCEEDED")
    record = operator.inspect("issue_refund", KEY)
    assert record is not None and record.attempt_id == second[0].action_id  # the takeover's record stands
    assert provider.refund_calls == 2 and provider.refunds_for("1042") == (1 if honours_key else 2)


def test_reconcile_never_overwrites_a_record_that_changed_after_it_was_read(pair: Pair) -> None:
    worker, operator, clock = pair()

    def execute(ctx: safety.ActionContext) -> str:
        clock.now += 3600
        record = operator.inspect("issue_refund", KEY)
        assert record is not None

        def lookup(c: safety.ActionContext) -> str:  # meanwhile the attempt finishes
            operator.store.update("issue_refund", record.key, record.attempt_id, expect=("EXECUTING",),
                                  state="SUCCEEDED", now=clock.now)  # fmt: skip
            return "NOT_FOUND"

        after = operator.reconcile("issue_refund", KEY, verify=lookup)
        assert after is not None and after.state == "SUCCEEDED"  # not turned into UNKNOWN (stale)
        return "done"

    worker.run("issue_refund", refund(), ORDER, execute, action_key=KEY)


def test_the_execution_start_is_recorded_durably(tmp_path: Path) -> None:
    clock = Clock()
    guard = Guard(POLICY, store=safety.SqliteActionStore(tmp_path / "a.db", secret=SECRET), clock=clock)
    starts: list[float | None] = []

    def execute(ctx: safety.ActionContext) -> str:
        restarted = safety.SqliteActionStore(tmp_path / "a.db", secret=SECRET)  # another process reads it
        starts.append(Guard(POLICY, store=restarted).inspect("issue_refund", KEY).started_at)  # type: ignore[union-attr]
        return "done"

    guard.run("issue_refund", refund(), ORDER, execute, action_key=KEY)
    assert starts == [clock.now]


@pytest.mark.parametrize("value", [0, -1, True, "900"])
def test_stale_after_and_clock_are_validated(value: Any) -> None:
    with pytest.raises(ValueError, match="stale_after_s"):
        Guard(POLICY, stale_after_s=value)
    with pytest.raises(TypeError, match="clock"):
        Guard(POLICY, clock=1.0)  # type: ignore[arg-type]
