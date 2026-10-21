"""aobs.safety: the refund policy, the guard's enforcement (which must not depend on telemetry),
safe handling of ambiguous outcomes, duplicate prevention, and what reaches the trace."""

from __future__ import annotations

import importlib
import sys
import threading
import time
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
POLICY_TEXT = """
version = 1
[actions.issue_refund]
required_evidence = ["order_id", "amount", "status"]
must_match = ["order_id"]
amount = "amount"
amount_at_most_evidence = true
max_amount = "200.00"
approval_above = "100.00"
once_per = ["order_id"]
"""
ORDER = {"order_id": "1042", "amount": 59.90, "status": "delivered"}
BIG_ORDER = {"order_id": "2077", "amount": 150.00, "status": "delivered"}


@pytest.fixture
def policy(tmp_path: Path) -> safety.SafetyPolicy:
    path = tmp_path / "policy.toml"
    path.write_text(POLICY_TEXT)
    return safety.load_policy(path)


class Spy:
    """An action that counts its calls and behaves as told."""

    def __init__(self, raises: BaseException | None = None, value: Any = "done") -> None:
        self.calls: list[safety.ActionContext] = []
        self.raises = raises
        self.value = value

    def __call__(self, ctx: safety.ActionContext) -> Any:
        self.calls.append(ctx)
        if self.raises is not None:
            raise self.raises
        return self.value


def refund(amount: float = 59.90, order_id: str = "1042") -> dict[str, Any]:
    return {"order_id": order_id, "amount": amount}


# --------------------------------------------------------------------------- policy configuration


def test_the_example_policy_loads() -> None:
    policy = safety.load_policy(EXAMPLE_DIR / "safety_policy.toml")
    rule = policy.actions["issue_refund"]
    assert rule.must_match == ("order_id",) and rule.once_per == ("order_id",)
    assert str(rule.max_amount) == "200.00" and str(rule.approval_above) == "100.00"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[actions.issue_refund]\nrequired_evidence = ['order_id']", "version = 1 is required"),
        ("version = 2\n[actions.a]\nrequired_evidence = ['x']", "version = 1 is required"),
        ("version = true\n[actions.a]\nrequired_evidence = ['x']", "version = 1 is required"),
        ("version = 1", "must define at least one action"),
        ("version = 1\nextra = 1\n[actions.a]\nrequired_evidence = ['x']", "unknown key"),
        ("version = 1\n[actions.a]\nmax_amount_typo = '1'", "unknown key"),
        ("version = 1\n[actions.a]", "sets no rule"),
        ("version = 1\n[actions.Bad]\nrequired_evidence = ['x']", "action name"),
        ("version = 1\n[actions.a]\nrequired_evidence = 'x'", "list of field names"),
        ("version = 1\n[actions.a]\nrequired_evidence = ['x', 'x']", "lists a field twice"),
        ("version = 1\n[actions.a]\nrequired_evidence = ['x']\nmust_match = ['y']", "must also be in required"),
        ("version = 1\n[actions.a]\namount = 'amount'\nmax_amount = 100", "written as a string"),
        ("version = 1\n[actions.a]\namount = 'amount'\nmax_amount = 'lots'", "not a decimal number"),
        ("version = 1\n[actions.a]\namount = 'amount'\nmax_amount = '-1'", "non-negative"),
        ("version = 1\n[actions.a]\namount = 'amount'\nmax_amount = 'NaN'", "finite"),
        ("version = 1\n[actions.a]\nmax_amount = '10'", "need the amount field name"),
        ("version = 1\n[actions.a]\namount = 'amount'\namount_at_most_evidence = true", "in required_evidence"),
        ("version = 1\n[actions.a]\namount = 'amount'\namount_at_most_evidence = 'yes'", "true or false"),
        ("version = 1\n[actions.a]\namount = 'a'\nmax_amount = '10'\napproval_above = '20'", "can never apply"),
        ("version = 1\n[actions.a\n", "not valid TOML"),
        (
            "version = 1\n[actions.a]\nrequired_evidence = ['x', 'y']\nmust_match = ['x']\nonce_per = ['y']",
            "once_per fields must also be in must_match",
        ),
    ],
)
def test_invalid_policies_are_rejected(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "policy.toml"
    path.write_text(text)
    with pytest.raises(safety.PolicyError, match=message):
        safety.load_policy(path)


def test_a_missing_policy_file_is_a_policy_error(tmp_path: Path) -> None:
    with pytest.raises(safety.PolicyError, match="cannot read policy"):
        safety.load_policy(tmp_path / "absent.toml")


# --------------------------------------------------------------------------- decisions


def test_a_valid_refund_is_allowed(policy: safety.SafetyPolicy) -> None:
    decision = safety.evaluate(policy, "issue_refund", refund(), ORDER)
    assert decision.verdict == "ALLOW" and decision.reason_codes == ("all_checks_passed",)
    assert decision.evidence_checked == ("order_id", "amount", "status") and decision.evidence_missing == ()


@pytest.mark.parametrize(
    ("request_", "evidence", "codes", "rule"),
    [
        (refund(250.0, "2077"), {**BIG_ORDER, "amount": 300.0}, ("amount_exceeds_limit",), "max_amount"),
        (refund(80.0), ORDER, ("amount_exceeds_evidence",), "amount_at_most_evidence"),
        (refund(order_id="2077"), ORDER, ("evidence_mismatch",), "must_match"),
        (refund(), {**ORDER, "status": None}, ("evidence_missing",), "required_evidence"),
        (refund(), {"order_id": "1042", "amount": 59.90}, ("evidence_missing",), "required_evidence"),
        (refund(), {"error": "order 1042 not found"}, ("evidence_missing",), "required_evidence"),
        (refund(), {**ORDER, "order_id": "  "}, ("evidence_missing",), "required_evidence"),
        (refund(), {**ORDER, "amount": "a lot"}, ("evidence_invalid",), "amount_at_most_evidence"),
        ({"amount": 10}, ORDER, ("request_missing",), "must_match"),
        (refund(0), ORDER, ("amount_invalid",), "amount"),
        (refund(-5), ORDER, ("amount_invalid",), "amount"),
        ({"order_id": "1042", "amount": True}, ORDER, ("amount_invalid",), "amount"),
        ({"order_id": "1042", "amount": float("nan")}, ORDER, ("amount_invalid",), "amount"),
        ({"order_id": "1042", "amount": float("inf")}, ORDER, ("amount_invalid",), "amount"),
        ({"order_id": "1042", "amount": "ten"}, ORDER, ("amount_invalid",), "amount"),
        ({"order_id": "1042"}, ORDER, ("amount_invalid",), "amount"),
    ],
)
def test_unsafe_or_unverifiable_refunds_are_denied(
    policy: safety.SafetyPolicy, request_: dict[str, Any], evidence: dict[str, Any], codes: tuple[str, ...], rule: str
) -> None:
    decision = safety.evaluate(policy, "issue_refund", request_, evidence)
    assert decision.verdict == "DENY"
    assert decision.reason_codes == codes and decision.rule == f"actions.issue_refund.{rule}"


def test_every_failed_check_is_reported_and_the_first_names_the_rule(policy: safety.SafetyPolicy) -> None:
    decision = safety.evaluate(policy, "issue_refund", refund(250.0, "2077"), {**ORDER, "status": ""})
    assert decision.verdict == "DENY"
    assert decision.reason_codes == ("evidence_missing", "evidence_mismatch", "amount_exceeds_evidence",
                                     "amount_exceeds_limit")  # fmt: skip
    assert decision.rule == "actions.issue_refund.required_evidence" and decision.evidence_missing == ("status",)


def test_a_large_refund_needs_approval(policy: safety.SafetyPolicy) -> None:
    decision = safety.evaluate(policy, "issue_refund", refund(150.0, "2077"), BIG_ORDER)
    assert decision.verdict == "REQUIRE_APPROVAL" and decision.reason_codes == ("approval_required",)
    assert decision.rule == "actions.issue_refund.approval_above"


@pytest.mark.parametrize(("request_", "evidence"), [(refund(), ["not", "a", "mapping"]), (None, ORDER)])
def test_inputs_that_are_not_mappings_are_denied(policy: safety.SafetyPolicy, request_: Any, evidence: Any) -> None:
    assert safety.evaluate(policy, "issue_refund", request_, evidence).reason_codes == ("invalid_input",)


def test_unknown_actions_are_denied_and_decisions_are_deterministic(policy: safety.SafetyPolicy) -> None:
    assert safety.evaluate(policy, "delete_account", {}, {}).reason_codes == ("action_not_permitted",)
    cases = [(refund(), ORDER), (refund(150.0, "2077"), BIG_ORDER), (refund(80.0), {**ORDER, "status": None})]
    for request_, evidence in cases:
        assert len({safety.evaluate(policy, "issue_refund", request_, evidence) for _ in range(5)}) == 1


# --------------------------------------------------------------------------- the guard


def test_an_allowed_action_runs_once_and_returns_its_value(policy: safety.SafetyPolicy) -> None:
    execute = Spy(value={"reference": "rf_1042"})
    result = safety.ActionGuard(policy).run("issue_refund", refund(), ORDER, execute)
    assert (result.outcome, result.recovery, result.executed) == ("SUCCEEDED", "none", True)
    assert result.value == {"reference": "rf_1042"} and len(execute.calls) == 1
    assert execute.calls[0].action_id == result.action_id  # per attempt; idempotency_key is per logical action


@pytest.mark.parametrize(
    ("request_", "evidence"),
    [(refund(250.0, "2077"), {**BIG_ORDER, "amount": 300.0}), (refund(order_id="2077"), ORDER), (refund(), {})],
)
def test_a_denied_action_never_runs(policy: safety.SafetyPolicy, request_: Any, evidence: Any) -> None:
    execute = Spy()
    result = safety.ActionGuard(policy).run("issue_refund", request_, evidence, execute)
    assert result.decision.verdict == "DENY" and result.outcome == "NOT_EXECUTED" and not result.executed
    assert execute.calls == []


class Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("no text form")


def test_values_that_break_evaluation_deny_rather_than_raise(policy: safety.SafetyPolicy) -> None:
    execute = Spy()
    result = safety.ActionGuard(policy).run("issue_refund", {"order_id": Unprintable(), "amount": 5}, ORDER, execute)
    assert result.decision.reason_codes == ("invalid_input",) and execute.calls == []


def _raise(ctx: safety.ActionContext) -> bool:
    raise RuntimeError("approval service down")


@pytest.mark.parametrize(
    ("approver", "approval"),
    [
        (None, "unavailable"),
        (lambda ctx: False, "denied"),
        (lambda ctx: None, "denied"),
        (lambda ctx: "yes", "denied"),  # only an explicit True approves
        (lambda ctx: 1, "denied"),
        (_raise, "denied"),
    ],
)
def test_approval_cannot_be_bypassed(
    policy: safety.SafetyPolicy, approver: Callable[[safety.ActionContext], Any] | None, approval: str
) -> None:
    execute = Spy()
    result = safety.ActionGuard(policy).run("issue_refund", refund(150.0, "2077"), BIG_ORDER, execute,
                                            approver=approver)  # fmt: skip
    assert (result.approval, result.outcome, result.executed) == (approval, "NOT_EXECUTED", False)
    assert execute.calls == []


def test_a_granted_approval_runs_the_action_and_a_refused_one_does_not_block_a_later_request(
    policy: safety.SafetyPolicy,
) -> None:
    guard, execute = safety.ActionGuard(policy), Spy()
    refused = guard.run("issue_refund", refund(150.0, "2077"), BIG_ORDER, execute, approver=lambda ctx: False)
    assert refused.outcome == "NOT_EXECUTED"
    asked: list[safety.ActionContext] = []
    granted = guard.run("issue_refund", refund(150.0, "2077"), BIG_ORDER, execute,
                        approver=lambda ctx: asked.append(ctx) or True)  # fmt: skip
    assert granted.approval == "granted" and granted.outcome == "SUCCEEDED" and len(execute.calls) == 1
    assert asked[0].decision.verdict == "REQUIRE_APPROVAL" and asked[0].request == refund(150.0, "2077")


def test_a_definite_failure_is_failed_and_may_be_run_again(policy: safety.SafetyPolicy) -> None:
    guard = safety.ActionGuard(policy)
    first = guard.run("issue_refund", refund(), ORDER, Spy(raises=safety.ActionNotPerformed("declined")))
    assert (first.outcome, first.recovery, first.error_type) == ("FAILED", "retry_allowed", "ActionNotPerformed")
    second = guard.run("issue_refund", refund(), ORDER, Spy())
    assert second.outcome == "SUCCEEDED"  # a new, explicit request; nothing happened the first time


@pytest.mark.parametrize("error", [TimeoutError("no response"), ConnectionResetError("reset"), ValueError("bad")])
def test_an_ambiguous_failure_is_unknown_and_never_retried(policy: safety.SafetyPolicy, error: Exception) -> None:
    guard, execute = safety.ActionGuard(policy), Spy(raises=error)
    result = guard.run("issue_refund", refund(), ORDER, execute)
    assert (result.outcome, result.recovery, result.verified) == ("UNKNOWN", "escalate", None)
    assert result.error_type == type(error).__name__ and len(execute.calls) == 1
    again = guard.run("issue_refund", refund(), ORDER, execute)  # the agent asks again
    assert again.decision.reason_codes == ("duplicate_action",) and again.duplicate_of == result.action_id
    assert len(execute.calls) == 1


@pytest.mark.parametrize(
    ("reported", "verified", "recovery"),
    [
        ("SUCCEEDED", "SUCCEEDED", "verified_succeeded"),
        ("FAILED", "FAILED", "verified_failed"),
        ("PENDING", "UNKNOWN", "escalate"),
        (True, "UNKNOWN", "escalate"),
    ],
)
def test_one_read_only_lookup_may_resolve_an_unknown_outcome(
    policy: safety.SafetyPolicy, reported: Any, verified: str, recovery: str
) -> None:
    execute, lookups = Spy(raises=TimeoutError()), Spy(value=reported)
    result = safety.ActionGuard(policy).run("issue_refund", refund(), ORDER, execute, verify=lookups)
    assert (result.outcome, result.verified, result.recovery) == ("UNKNOWN", verified, recovery)
    assert len(execute.calls) == 1 and len(lookups.calls) == 1
    assert lookups.calls[0].action_id == execute.calls[0].action_id


def test_a_failing_lookup_leaves_the_outcome_unknown(policy: safety.SafetyPolicy) -> None:
    execute = Spy(raises=TimeoutError())
    result = safety.ActionGuard(policy).run("issue_refund", refund(), ORDER, execute,
                                            verify=Spy(raises=ConnectionError("no lookup")))  # fmt: skip
    assert (result.verified, result.recovery, result.error_type) == ("UNKNOWN", "escalate", "TimeoutError")
    assert len(execute.calls) == 1


def test_after_a_verified_failure_a_new_request_may_run(policy: safety.SafetyPolicy) -> None:
    guard = safety.ActionGuard(policy)
    guard.run("issue_refund", refund(), ORDER, Spy(raises=TimeoutError()), verify=lambda ctx: "FAILED")
    assert guard.run("issue_refund", refund(), ORDER, Spy()).outcome == "SUCCEEDED"


def test_a_second_refund_for_the_same_order_is_a_duplicate(policy: safety.SafetyPolicy) -> None:
    guard, execute = safety.ActionGuard(policy), Spy()
    first = guard.run("issue_refund", refund(), ORDER, execute)
    second = guard.run("issue_refund", refund(10.0), ORDER, execute)  # a different amount, the same order
    assert second.outcome == "NOT_EXECUTED" and second.decision.rule == "actions.issue_refund.once_per"
    assert second.duplicate_of == first.action_id and len(execute.calls) == 1
    other = guard.run("issue_refund", refund(150.0, "2077"), BIG_ORDER, execute, approver=lambda ctx: True)
    assert other.outcome == "SUCCEEDED"  # another order is not a duplicate


def test_concurrent_requests_for_the_same_order_run_once(policy: safety.SafetyPolicy) -> None:
    guard, started = safety.ActionGuard(policy), threading.Event()

    def slow(ctx: safety.ActionContext) -> str:
        started.set()
        time.sleep(0.2)
        return "done"

    results: list[safety.ActionResult] = []
    first = threading.Thread(target=lambda: results.append(guard.run("issue_refund", refund(), ORDER, slow)))
    first.start()
    started.wait(5)
    results.append(guard.run("issue_refund", refund(), ORDER, slow))  # while the first one runs
    first.join(5)
    assert sorted(r.outcome for r in results) == ["NOT_EXECUTED", "SUCCEEDED"]


# --------------------------------------------------------------------------- enforcement without telemetry


def guardrails(exporter: InMemoryExporter) -> list[dict[str, Any]]:
    return [e for e in exporter.events() if e["type"] == "span_end" and e["kind"] == "guardrail"]


def test_enforcement_does_not_need_telemetry(policy: safety.SafetyPolicy) -> None:
    aobs.shutdown(timeout_s=1)  # the SDK is not running
    guard, execute = safety.ActionGuard(policy), Spy(raises=TimeoutError())
    assert guard.run("issue_refund", refund(250.0, "2077"), BIG_ORDER, execute).outcome == "NOT_EXECUTED"
    assert guard.run("issue_refund", refund(), ORDER, execute).outcome == "UNKNOWN"
    assert guard.run("issue_refund", refund(), ORDER, execute).decision.reason_codes == ("duplicate_action",)
    assert len(execute.calls) == 1


@pytest.mark.parametrize("method", ["event", "set_attributes", "set_status", "set_error", "fingerprint_input"])
def test_failing_telemetry_does_not_weaken_enforcement(
    policy: safety.SafetyPolicy, sdk: InMemoryExporter, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("telemetry is broken")

    monkeypatch.setattr(aobs.Span, method, explode)
    guard, execute = safety.ActionGuard(policy), Spy(raises=TimeoutError())
    assert guard.run("issue_refund", refund(order_id="2077"), ORDER, execute).outcome == "NOT_EXECUTED"
    assert guard.run("issue_refund", refund(150.0, "2077"), BIG_ORDER, execute).approval == "unavailable"
    assert guard.run("issue_refund", refund(), ORDER, execute).outcome == "UNKNOWN"
    assert guard.run("issue_refund", refund(), ORDER, execute).outcome == "NOT_EXECUTED"
    assert len(execute.calls) == 1
    flush()
    if method != "set_attributes":  # set at span creation: then no span exists and nothing is recorded
        assert len(guardrails(sdk)) == 4  # the spans existed, so the failing recording path really ran
    started = [e for e in sdk.events() if e["type"] == "span_start" and e["kind"] == "guardrail"]
    assert {e["span_id"] for e in started} <= {e["span_id"] for e in guardrails(sdk)}  # none left unfinished


def test_a_span_that_cannot_be_created_does_not_weaken_enforcement(
    policy: safety.SafetyPolicy, sdk: InMemoryExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("no spans today")

    monkeypatch.setattr(safety._api, "span", explode)
    execute = Spy()
    guard = safety.ActionGuard(policy)
    assert guard.run("issue_refund", refund(order_id="2077"), ORDER, execute).outcome == "NOT_EXECUTED"
    assert guard.run("issue_refund", refund(), ORDER, execute).outcome == "SUCCEEDED" and len(execute.calls) == 1


# --------------------------------------------------------------------------- the trace


def span_events(exporter: InMemoryExporter, span_id: str) -> list[dict[str, Any]]:
    return [e for e in exporter.events() if e["type"] == "span_event" and e["span_id"] == span_id]


def test_the_guardrail_span_is_the_parent_of_the_tool_execution(
    policy: safety.SafetyPolicy, sdk: InMemoryExporter
) -> None:
    @aobs.tool(name="issue_refund")
    def send(order_id: str, amount: float) -> dict[str, str]:
        return {"reference": f"rf_{order_id}"}

    with aobs.run("answer_ticket"):
        result = safety.ActionGuard(policy).run("issue_refund", refund(), ORDER, lambda ctx: send("1042", 59.90))
    flush()
    (trace,) = traces(sdk).values()  # schema-valid and accepted by the collector's model
    (guard_span,) = guardrails(sdk)
    tool = next(n for n in trace.spans.values() if n.kind == "tool")
    root = trace.spans[trace.roots[0]]
    assert tool.parent_span_id == guard_span["span_id"] and guard_span["parent_span_id"] == root.span_id
    attributes = guard_span["attributes"]
    assert guard_span["name"] == "action issue_refund" and guard_span["status"] == "ok"
    assert attributes["aobs.safety.action_id"] == result.action_id and attributes["aobs.safety.decision"] == "ALLOW"
    assert attributes["aobs.safety.final_outcome"] == "SUCCEEDED" and attributes["aobs.safety.auto_retry"] is False
    assert [e["name"] for e in span_events(sdk, guard_span["span_id"])] == [
        "aobs.safety.decision", "aobs.safety.outcome", "aobs.safety.recovery",
    ]  # fmt: skip


def test_a_denied_action_is_recorded_without_a_tool_span(policy: safety.SafetyPolicy, sdk: InMemoryExporter) -> None:
    safety.ActionGuard(policy).run("issue_refund", refund(250.0, "2077"), {**BIG_ORDER, "amount": 300.0}, Spy())
    flush()
    (guard_span,) = guardrails(sdk)
    assert guard_span["status"] == "cancelled"
    assert guard_span["attributes"]["aobs.safety.reason_codes"] == ["amount_exceeds_limit"]
    assert guard_span["attributes"]["aobs.safety.rule"] == "actions.issue_refund.max_amount"
    assert not [e for e in sdk.events() if e["type"] == "span_end" and e["kind"] == "tool"]


def test_an_unknown_outcome_is_an_error_that_must_not_be_retried(
    policy: safety.SafetyPolicy, sdk: InMemoryExporter
) -> None:
    safety.ActionGuard(policy).run("issue_refund", refund(), ORDER, Spy(raises=TimeoutError()), verify=lambda ctx: "?")
    flush()
    (guard_span,) = guardrails(sdk)
    assert guard_span["status"] == "error"
    assert guard_span["error"] == {"type": "ActionOutcomeUnknown", "origin": "tool", "retryable": False}
    assert guard_span["attributes"]["aobs.safety.recovery"] == "escalate"
    assert [e["name"] for e in span_events(sdk, guard_span["span_id"])] == [
        "aobs.safety.decision", "aobs.safety.outcome", "aobs.safety.verification", "aobs.safety.recovery",
    ]  # fmt: skip


def test_an_approval_and_a_duplicate_are_recorded(policy: safety.SafetyPolicy, sdk: InMemoryExporter) -> None:
    guard = safety.ActionGuard(policy)
    first = guard.run("issue_refund", refund(150.0, "2077"), BIG_ORDER, Spy(), approver=lambda ctx: True)
    guard.run("issue_refund", refund(150.0, "2077"), BIG_ORDER, Spy(), approver=lambda ctx: True)
    flush()
    approved, duplicate = guardrails(sdk)
    assert approved["attributes"]["aobs.safety.approval"] == "granted"
    assert duplicate["attributes"]["aobs.safety.reason_codes"] == ["duplicate_action"]
    assert duplicate["attributes"]["aobs.safety.duplicate_of"] == first.action_id
    assert approved["input_fingerprint"] == duplicate["input_fingerprint"]  # the same order, keyed-hashed


def test_evidence_values_and_sensitive_errors_never_reach_the_trace(
    policy: safety.SafetyPolicy, make_sdk: Callable[..., InMemoryExporter]
) -> None:
    exporter = make_sdk(capture="full")  # even when the application captures content
    secret_evidence = {**ORDER, "order_id": "ORD-7731", "customer_email": "jane.doe@example.com"}

    @aobs.tool(name="issue_refund", capture=False)
    def send() -> None:
        raise TimeoutError(
            "refund for card 4111 1111 1111 1111 (jane.doe@example.com) timed out, token sk-abcdefghijklmnopqrstu"
        )

    request_ = {"order_id": "ORD-7731", "amount": 59.90}
    safety.ActionGuard(policy).run("issue_refund", request_, secret_evidence, lambda ctx: send())
    flush()
    text = "".join(body.decode() for body in exporter.bodies)
    for secret in ("ORD-7731", "59.9", "jane.doe@example.com", "4111 1111 1111 1111", "sk-abcdefghijklmnopqrstu"):
        assert secret not in text, secret
    assert "[REDACTED:card_number]" in text  # the tool's error message was kept, scrubbed


# --------------------------------------------------------------------------- the example


@pytest.fixture
def demo() -> Iterator[ModuleType]:
    sys.path.insert(0, str(EXAMPLE_DIR))
    try:
        for name in ("safety_demo", "protect_example", "agent", "mock_llm", "payments"):
            sys.modules.pop(name, None)
        yield importlib.import_module("safety_demo")
    finally:
        sys.path.remove(str(EXAMPLE_DIR))


EXPECTED = {  # scenario -> (decisions, final outcomes, refund calls, status lookups, refunds made)
    "allowed": (["ALLOW"], ["SUCCEEDED"], 1, 0, 1),
    "needs-approval": (["REQUIRE_APPROVAL"], ["NOT_EXECUTED"], 0, 0, 0),
    "approved": (["REQUIRE_APPROVAL"], ["SUCCEEDED"], 1, 0, 1),
    "wrong-order": (["DENY"], ["NOT_EXECUTED"], 0, 0, 0),
    "unverified-order": (["DENY"], ["NOT_EXECUTED"], 0, 0, 0),
    "rejected": (["ALLOW"], ["FAILED"], 1, 0, 0),
    "timeout-not-done": (["ALLOW"], ["FAILED"], 1, 1, 0),
    "timeout-done": (["ALLOW"], ["SUCCEEDED"], 1, 1, 1),
    "timeout-unknown": (["ALLOW"], ["UNKNOWN"], 1, 1, 1),
    "timeout-pending": (["ALLOW"], ["UNKNOWN"], 1, 1, 0),
    "duplicate": (["ALLOW", "DENY"], ["SUCCEEDED", "NOT_EXECUTED"], 1, 0, 1),
    "restart": (["ALLOW", "DENY"], ["SUCCEEDED", "NOT_EXECUTED"], 1, 0, 1),
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_the_demo_scenarios(demo: ModuleType, sdk: InMemoryExporter, name: str) -> None:
    outcome = demo.run_scenario(name, safety.load_policy(EXAMPLE_DIR / "safety_policy.toml"))
    decisions, finals, refund_calls, lookups, made = EXPECTED[name]
    assert [r.decision.verdict for r in outcome.results] == decisions
    assert [r.final_outcome for r in outcome.results] == finals
    if name in ("duplicate", "restart"):  # the agent is told the refund was already made, not "not allowed"
        assert outcome.results[1].previous_outcome == "SUCCEEDED" and "already_refunded" in outcome.replies[1]
    p = outcome.payments
    assert (p.refund_calls, p.status_calls, p.refunds_for("1042") + p.refunds_for("2077")) == (
        refund_calls,
        lookups,
        made,
    )
    flush()
    traces(sdk)  # every batch the demo produced is schema-valid


def test_the_timeout_pending_refund_is_made_once_by_the_provider_and_never_repeated(demo: ModuleType) -> None:
    outcome = demo.run_scenario("timeout-pending", safety.load_policy(EXAMPLE_DIR / "safety_policy.toml"))
    assert outcome.payments.refunds_for("1042") == 0 and outcome.results[0].recovery == "escalate"
    outcome.payments.complete_pending()
    assert outcome.payments.refunds_for("1042") == 1 and outcome.payments.refund_calls == 1


def test_wrapping_the_existing_tool_takes_a_few_lines_and_runs_it_once(
    demo: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    example = importlib.import_module("protect_example")
    agent = importlib.import_module("agent")
    calls: list[tuple[str, float]] = []
    original = agent.issue_refund

    def counted(order_id: str, amount: float) -> Any:
        calls.append((order_id, amount))
        return original(order_id, amount)

    monkeypatch.setattr(agent, "issue_refund", counted)
    replies = example.main()
    assert calls == [("1042", 59.90)]  # three requests, one in a restarted process: one refund
    assert "rf_1042" in replies[0] and replies[1:] == ["Order 1042 is refunded."] * 2  # not "not allowed"
    added = [line for line in (EXAMPLE_DIR / "protect_example.py").read_text().splitlines() if "# safety" in line]
    assert len(added) <= 30
    assert not [line for line in (EXAMPLE_DIR / "agent.py").read_text().splitlines() if "safety" in line]


def test_the_example_instrumentation_limit_still_holds() -> None:
    marked = [
        line
        for f in ("agent.py", "run.py")
        for line in (EXAMPLE_DIR / f).read_text().splitlines()
        if "# aobs" in line and not line.lstrip().startswith(("#", '"', "The ", "``"))
    ]
    assert 1 <= len(marked) <= 10
