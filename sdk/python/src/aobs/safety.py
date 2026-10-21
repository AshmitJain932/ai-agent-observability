"""Action safety: decide before a side effect, record it before it happens, never blindly retry.

    from aobs import safety

    guard = safety.ActionGuard(                                # one per process, same settings in every worker
        safety.load_policy("safety_policy.toml"),
        store=safety.SqliteActionStore("actions.db", secret=store_secret),
        idempotency=safety.IdempotencyKeys(namespace="support-agent/production", secret=idempotency_secret),
    )
    refund = guard.protect(
        send_refund, action="issue_refund",
        evidence=lambda: orders.lookup(ticket.order_id),       # trusted: never the model's arguments
        action_key=f"refund:{ticket.id}",                      # the identity of this logical refund
        idempotency_param="idempotency_key",                   # send_refund(..., idempotency_key=...)
        verify=lambda ctx: payments.status(ctx.idempotency_key))  # read-only lookup
    try:
        reply = refund(order_id=args["order_id"], amount=args["amount"])  # the model's arguments
    except safety.ActionBlocked as blocked:
        reply = describe(blocked.result)  # denied, a duplicate, failed, or outcome unknown

``guard.run(action, request, evidence, execute, ...)`` is the same without the wrapper.

**Policy** (TOML, strictly validated; anything not listed is denied)::

    version = 1
    [actions.issue_refund]
    required_evidence = ["order_id", "amount", "status"]  # must be present in the verified evidence
    must_match = ["order_id"]               # request value == evidence value (compared as text)
    amount = "amount"                       # the amount field, in the request and the evidence
    amount_at_most_evidence = true          # the request amount may not exceed the evidence amount
    max_amount = "100.00"                   # DENY above this
    approval_above = "50.00"                # REQUIRE_APPROVAL above this
    once_per = ["order_id"]                 # one action per value unless FAILED (fields from must_match)

**Decisions** (:func:`evaluate`, deterministic, no I/O): ``ALLOW``, ``DENY`` or
``REQUIRE_APPROVAL``, with stable reason codes and the rule that decided. Missing, empty or
invalid evidence or request fields deny; they never approve.

**Identity.** ``action_key`` (from the caller, e.g. the support ticket: never from the model) names
one *logical* action. The same key always means the same request (a different request under it is
denied, ``action_key_conflict``). ``ctx.action_id`` is per attempt and is what traces record. A
legitimate second refund is a new logical action: a new action key, and a policy whose ``once_per``
includes a trusted per-case field.

**Provider idempotency keys** (:class:`IdempotencyKeys`, required whenever an ``action_key`` is
used). Every attempt at one logical action gets the same ``ctx.idempotency_key``, an HMAC of the
action and action key under a long-lived random secret and a service/environment ``namespace``; it
does not depend on the store's secret. Pass it to the provider. The provider's role is essential:
only a provider that honours idempotency keys turns a repeated request (a retry, a second process,
or a paused process that resumes) into a no-op. The store only decides which attempt may *start*.
Without provider idempotency nothing here can prevent a duplicate in those cases.

**Store.** Before ``execute`` is called, the action is reserved and then marked ``EXECUTING`` (with
its start time) in the guard's :class:`ActionStore`, atomically and committed. :class:`MemoryActionStore`
(the default) is for one process and is lost on restart: nothing survives a crash.
:class:`SqliteActionStore` persists records and coordinates processes on one host (local file system
with working locks), and needs a stable random secret that keys every stored digest; another
database can implement :class:`ActionStore`. A durable store requires an ``action_key`` on every
action. All guards sharing a store must use the same ``once_per`` rule for an action (otherwise
``policy_mismatch``). If the store cannot be read or written, nothing runs (``store_unavailable``).

**Outcomes** (:meth:`ActionGuard.run`; ``execute`` is called at most once per call):

* ``NOT_EXECUTED`` — denied, approval not granted, a duplicate, or the store was unavailable.
* ``SUCCEEDED`` — ``execute`` returned (adapters must raise when the action did not succeed).
* ``FAILED`` — ``execute`` raised :class:`ActionNotPerformed`: the adapter states the action
  definitely did not happen (e.g. the provider refused the request before acting on it).
* ``UNKNOWN`` — anything else, timeouts included: the action may have happened. It is **never
  retried**. ``verify`` (if given) is called once and must only read the provider's state. It must
  return ``"SUCCEEDED"`` only when the action is confirmed, and ``"FAILED"`` only when the provider
  guarantees the request can no longer complete; an early "not found" is not that, and anything
  but those two strings leaves the outcome ``UNKNOWN``.

A repeated request is a duplicate (``duplicate_action``) while an earlier one with the same action
key or ``once_per`` values is reserved, executing, succeeded or unknown; the result carries the
earlier state in ``previous_outcome`` (e.g. ``SUCCEEDED``: it was already done). Once ``FAILED`` is
recorded (by the adapter, a lookup, or a person) a new attempt may run. The guard never retries on
its own. A process that crashes after marking an action ``EXECUTING`` leaves it blocking.
:meth:`ActionGuard.reconcile` makes one read-only lookup: a confirmed success is recorded at any
time, but an execution younger than ``stale_after_s`` counts as still running and nothing else
changes it (a "not found" may only mean "not yet"). After that it may be declared stale
(``UNKNOWN``). Staleness is a timeout on the wall clock, not proof: the original process may only be
paused, and may send its request when it resumes. :meth:`ActionGuard.resolve` records a person's
decision; resolving a stale action as ``FAILED`` additionally requires the person to state that
the original process has stopped (``original_stopped=True``), which the guard records but cannot
verify. An attempt that finishes after its record was changed cannot overwrite the later state (a
version check fences it); the result then reports the stored outcome (``stored_outcome``,
``final_outcome``), and a success it observed is kept and marked ``conflict``.
:meth:`ActionGuard.inspect` shows the stored state.

**Synchronous only.** ``execute``, ``verify``, ``approver`` and protected functions must be
synchronous: coroutine and generator functions raise ``TypeError`` before anything runs. If
``execute`` returns an awaitable or generator anyway, its work did not (all) run inside the guard:
the outcome is ``UNKNOWN`` and an unstarted coroutine or generator is closed so it cannot run later.

**Enforcement does not depend on telemetry.** The decision is made and enforced before anything is
recorded, and recording errors are ignored. When ``aobs`` is initialized, each run is a span of
kind ``guardrail`` (``action <action>``) whose child is the tool span of the execution, with
``aobs.safety.*`` attributes and span events (decision, approval, outcome, verification,
recovery). Evidence values, action keys, idempotency keys and secrets are never recorded: only field
names, reason codes, and a keyed fingerprint of the identifying values (``input_fingerprint``).

**What is and is not guaranteed.** A policy decision says what the application's rules allow; it
does not make the external action itself safe. Nothing here gives exactly-once execution. With a
shared durable store, at most one attempt per action key is *started while the store considers an
earlier one running or settled as succeeded or unknown*; a further attempt starts only after
``FAILED`` is recorded, and an earlier attempt that was only paused can still reach the provider
after that. Whether that becomes a second side effect depends on the provider honouring the
idempotency key. Without provider idempotency or an authoritative status lookup, ``UNKNOWN``
outcomes are left to a person. The memory store gives none of this across processes or restarts.
Calls that bypass the guard (e.g. calling the original function directly) are not prevented.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import math
import re
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, NoReturn, TypeVar

from . import _api
from ._ids import uuid7
from ._safety_store import (
    ActionRecord,
    ActionStore,
    MemoryActionStore,
    PolicyMismatch,
    Resolution,
    SqliteActionStore,
    State,
    StoreConfigurationError,
    UnsafePermissionsWarning,
    _hmac,
    check_secret,
    reservation_conflict,
)

__all__ = [
    "ActionBlocked",
    "ActionContext",
    "ActionGuard",
    "ActionNotPerformed",
    "ActionRecord",
    "ActionResult",
    "ActionRule",
    "ActionStore",
    "Decision",
    "IdempotencyConfigMismatch",
    "IdempotencyKeys",
    "MemoryActionStore",
    "PolicyError",
    "PolicyMismatch",
    "SafetyPolicy",
    "SqliteActionStore",
    "StoreConfigurationError",
    "UnsafePermissionsWarning",
    "evaluate",
    "load_policy",
    "parse_policy",
    "reservation_conflict",
]

Verdict = Literal["ALLOW", "DENY", "REQUIRE_APPROVAL"]
Outcome = Literal["SUCCEEDED", "FAILED", "UNKNOWN", "NOT_EXECUTED"]
Approval = Literal["not_required", "granted", "denied", "unavailable"]
Recovery = Literal["none", "retry_allowed", "verified_succeeded", "verified_failed", "escalate"]
R = TypeVar("R")

POLICY_VERSION = 1
_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}\Z")
_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")
_RULE_KEYS = frozenset(
    {"required_evidence", "must_match", "amount", "amount_at_most_evidence", "max_amount", "approval_above", "once_per"}
)


class PolicyError(ValueError):
    """The safety policy is invalid; nothing is allowed under an invalid policy."""


class ActionNotPerformed(Exception):
    """Raised by an action adapter when the action **definitely did not happen** (e.g. the request
    was refused before it was sent). Any other exception means the outcome is unknown."""


# --------------------------------------------------------------------------- policy


@dataclass(frozen=True)
class ActionRule:
    action: str
    required_evidence: tuple[str, ...] = ()
    must_match: tuple[str, ...] = ()
    amount: str | None = None
    amount_at_most_evidence: bool = False
    max_amount: Decimal | None = None
    approval_above: Decimal | None = None
    once_per: tuple[str, ...] = ()


@dataclass(frozen=True)
class SafetyPolicy:
    actions: Mapping[str, ActionRule]
    version: int = POLICY_VERSION


def _fields(action: str, key: str, value: Any) -> tuple[str, ...]:
    where = f"actions.{action}.{key}"
    if not isinstance(value, list) or not all(isinstance(v, str) and _FIELD.match(v) for v in value):
        raise PolicyError(f"{where} must be a list of field names")
    if len(set(value)) != len(value):
        raise PolicyError(f"{where} lists a field twice")
    return tuple(value)


def _money(action: str, key: str, value: Any) -> Decimal:
    where = f"actions.{action}.{key}"
    if not isinstance(value, str):
        raise PolicyError(f'{where} must be a decimal number written as a string, e.g. "100.00"')
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise PolicyError(f"{where} is not a decimal number: {value!r}") from None
    if not amount.is_finite() or amount < 0:
        raise PolicyError(f"{where} must be a finite, non-negative amount")
    return amount


def _rule(action: str, data: Any) -> ActionRule:
    if not _NAME.match(action):
        raise PolicyError(f"action name {action!r} must match {_NAME.pattern}")
    if not isinstance(data, dict):
        raise PolicyError(f"actions.{action} must be a table")
    unknown = sorted(set(data) - _RULE_KEYS)
    if unknown:
        raise PolicyError(f"actions.{action}: unknown key(s) {', '.join(unknown)}")
    if not data:
        raise PolicyError(f"actions.{action} sets no rule; list required_evidence or limits")
    required = _fields(action, "required_evidence", data.get("required_evidence", []))
    match = _fields(action, "must_match", data.get("must_match", []))
    once_per = _fields(action, "once_per", data.get("once_per", []))
    if set(match) - set(required):
        raise PolicyError(f"actions.{action}.must_match fields must also be in required_evidence")
    if set(once_per) - set(match):
        raise PolicyError(
            f"actions.{action}.once_per fields must also be in must_match: the duplicate check may only use "
            "values verified against the evidence"
        )
    amount = data.get("amount")
    if amount is not None and not (isinstance(amount, str) and _FIELD.match(amount)):
        raise PolicyError(f"actions.{action}.amount must be a field name")
    at_most = data.get("amount_at_most_evidence", False)
    if not isinstance(at_most, bool):
        raise PolicyError(f"actions.{action}.amount_at_most_evidence must be true or false")
    max_amount = _money(action, "max_amount", data["max_amount"]) if "max_amount" in data else None
    approval = _money(action, "approval_above", data["approval_above"]) if "approval_above" in data else None
    if amount is None and (at_most or max_amount is not None or approval is not None):
        raise PolicyError(f"actions.{action}: amount limits need the amount field name (amount = ...)")
    if at_most and amount not in required:
        raise PolicyError(f"actions.{action}: amount_at_most_evidence needs {amount!r} in required_evidence")
    if max_amount is not None and approval is not None and approval > max_amount:
        raise PolicyError(f"actions.{action}: approval_above is larger than max_amount, so it can never apply")
    return ActionRule(action, required, match, amount, at_most, max_amount, approval, once_per)


def parse_policy(data: Any) -> SafetyPolicy:
    if not isinstance(data, dict):
        raise PolicyError("the policy must be a table")
    unknown = sorted(set(data) - {"version", "actions"})
    if unknown:
        raise PolicyError(f"unknown key(s) {', '.join(unknown)}")
    if data.get("version") != POLICY_VERSION or isinstance(data.get("version"), bool):
        raise PolicyError(f"version = {POLICY_VERSION} is required")
    actions = data.get("actions")
    if not isinstance(actions, dict) or not actions:
        raise PolicyError("[actions.<name>] must define at least one action")
    return SafetyPolicy({name: _rule(name, rule) for name, rule in actions.items()})


def load_policy(path: str | Path) -> SafetyPolicy:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except OSError as exc:
        raise PolicyError(f"cannot read policy {path}: {exc.strerror or exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"policy {path} is not valid TOML: {exc}") from None
    return parse_policy(data)


# --------------------------------------------------------------------------- decisions


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason_codes: tuple[str, ...]  # every failed check, in rule order; ("all_checks_passed",) when allowed
    rule: str  # the policy rule that decided, e.g. "actions.issue_refund.max_amount"
    action: str
    evidence_checked: tuple[str, ...] = ()  # field names only, never values
    evidence_missing: tuple[str, ...] = ()


def _absent(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _amount(value: Any) -> Decimal | None:
    """A finite amount, or None for anything else (booleans, NaN, text that is not a number)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    return amount if amount.is_finite() else None


def evaluate(policy: SafetyPolicy, action: str, request: Any, evidence: Any) -> Decision:
    """Decide whether ``action`` with ``request`` may run, given the verified ``evidence``. Pure and
    deterministic. Every check runs; the first failing one names the deciding rule."""
    rule = policy.actions.get(action)
    if rule is None:
        return Decision("DENY", ("action_not_permitted",), "actions", action)
    if not isinstance(request, Mapping) or not isinstance(evidence, Mapping):
        return Decision("DENY", ("invalid_input",), f"actions.{action}", action)
    base = f"actions.{action}"
    denials: list[tuple[str, str]] = []  # (reason code, rule)
    missing = tuple(f for f in rule.required_evidence if _absent(evidence.get(f)))
    if missing:
        denials.append(("evidence_missing", f"{base}.required_evidence"))
    for name in rule.must_match:
        if name in missing:
            continue
        if _absent(request.get(name)):
            denials.append(("request_missing", f"{base}.must_match"))
        elif str(request[name]).strip() != str(evidence[name]).strip():
            denials.append(("evidence_mismatch", f"{base}.must_match"))
    if any(_absent(request.get(name)) for name in rule.once_per):
        denials.append(("request_missing", f"{base}.once_per"))
    needs_approval = False
    if rule.amount is not None:
        requested = _amount(request.get(rule.amount))
        if requested is None or requested <= 0:
            denials.append(("amount_invalid", f"{base}.amount"))
        else:
            if rule.amount_at_most_evidence and rule.amount not in missing:
                verified = _amount(evidence.get(rule.amount))
                if verified is None:
                    denials.append(("evidence_invalid", f"{base}.amount_at_most_evidence"))
                elif requested > verified:
                    denials.append(("amount_exceeds_evidence", f"{base}.amount_at_most_evidence"))
            if rule.max_amount is not None and requested > rule.max_amount:
                denials.append(("amount_exceeds_limit", f"{base}.max_amount"))
            needs_approval = rule.approval_above is not None and requested > rule.approval_above
    checked = tuple(dict.fromkeys((*rule.required_evidence, *rule.must_match)))
    if denials:
        codes = tuple(dict.fromkeys(code for code, _ in denials))
        return Decision("DENY", codes, denials[0][1], action, checked, missing)
    if needs_approval:
        return Decision("REQUIRE_APPROVAL", ("approval_required",), f"{base}.approval_above", action, checked)
    return Decision("ALLOW", ("all_checks_passed",), base, action, checked)


# --------------------------------------------------------------------------- identity


_KEY_MAX = 256
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_MAX_DEPTH = 16


_NAMESPACE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")


class IdempotencyConfigMismatch(ValueError):
    """An action's provider key came from a different :class:`IdempotencyKeys` configuration."""


class IdempotencyKeys:
    """How provider idempotency keys are made: ``HMAC-SHA256(secret, namespace, action, action_key)``.

    * ``namespace`` names the service and environment (e.g. ``"support-agent/production"``), so that
      deployments sharing one provider account never send the same key for different actions.
    * ``secret``: at least 32 bytes from a cryptographic random source, separate from the store's
      secret, long-lived, and the same in every worker and after every restart. Without it, keys of
      low-entropy action keys (ticket numbers) could be guessed by whoever sees them.

    Keys are stable for the same configuration and change with either value. Each stored action
    records which configuration (``key_id``, a fingerprint; never the secret) made its key, and the
    guard refuses to continue an action under another one (``idempotency_config_changed``; a new
    key would let the provider repeat it). To rotate, start new actions with the new configuration
    and keep the old one until every action made under it is settled (``SUCCEEDED`` or ``FAILED``
    and not to be retried); a ``FAILED`` action that should run again needs a new action key.
    Some providers also remember a *failed* response for a key and return it again: reusing the key
    after ``FAILED`` may not let the same operation succeed later.
    """

    def __init__(self, *, namespace: str, secret: bytes) -> None:
        if not isinstance(namespace, str) or not _NAMESPACE.match(namespace):
            raise ValueError("namespace must be 1-128 characters: letters, digits and . _ : / - (e.g. 'service/env')")
        self.namespace = namespace
        self.__secret = check_secret(secret, "idempotency secret", ValueError)
        self.key_id = _hmac(self.__secret, "idempotency-id", (namespace,))[:16]

    def key(self, action: str, action_key: str) -> str:
        """The provider idempotency key of one logical action."""
        return "aobs-" + _hmac(self.__secret, "idempotency", (self.namespace, action, action_key))[:40]

    def __repr__(self) -> str:
        return f"IdempotencyKeys(namespace={self.namespace!r}, key_id={self.key_id!r})"

    def __reduce__(self) -> NoReturn:
        raise TypeError("idempotency keys cannot be pickled")


def _valid_key(action_key: Any) -> bool:
    return (
        isinstance(action_key, str)
        and 0 < len(action_key) <= _KEY_MAX
        and action_key == action_key.strip()
        and not _CONTROL.search(action_key)
    )


def _plain(value: Any, depth: int = 0) -> Any:
    """A private deep copy of a request value. Only plain data (what a tool call's arguments are):
    anything else is refused, because what is checked must be exactly what is executed."""
    if value is None or isinstance(value, (bool, int, float, str, Decimal)):
        return value
    if depth >= _MAX_DEPTH:
        raise ValueError("request nested too deeply")
    if isinstance(value, list):
        return [_plain(v, depth + 1) for v in value]
    if isinstance(value, tuple):
        return tuple(_plain(v, depth + 1) for v in value)
    if isinstance(value, Mapping) and all(isinstance(k, str) for k in value):
        return {k: _plain(v, depth + 1) for k, v in value.items()}
    raise TypeError(f"{type(value).__name__} is not plain data")


def _stable(value: Any) -> str:
    """A text form of plain data that is equal exactly when the data is (types included)."""
    if isinstance(value, Mapping):
        return "{" + ",".join(f"{k!r}:{_stable(v)}" for k, v in sorted(value.items())) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_stable(v) for v in value) + "]"
    if isinstance(value, tuple):
        return "(" + ",".join(_stable(v) for v in value) + ")"
    return f"{type(value).__name__}:{value!r}"


# --------------------------------------------------------------------------- the guard


@dataclass(frozen=True)
class ActionContext:
    """What ``execute``, ``verify`` and ``approver`` receive. ``request`` is a read-only copy of
    the checked request: execute exactly these values. ``idempotency_key`` is the provider key of
    the logical action (see :class:`IdempotencyKeys`); without an ``action_key`` it is unique to
    this attempt and gives no protection across attempts."""

    action_id: str
    action: str
    request: Mapping[str, Any]
    decision: Decision
    action_key: str | None = None
    idempotency_key: str = ""


@dataclass(frozen=True)
class ActionResult:
    action_id: str
    decision: Decision
    approval: Approval
    executed: bool  # execute() was called (its effect may still be unknown)
    outcome: Outcome  # what execute() did in this attempt
    recovery: Recovery  # what may happen next; the guard itself never retries
    value: Any = None  # execute()'s return value when SUCCEEDED
    error_type: str | None = None  # exception type of execute() or verify(), never its message
    verified: Outcome | None = None  # what verify() reported, if it was called
    duplicate_of: str | None = None  # the earlier attempt that blocked this one
    previous_outcome: State | None = None  # that attempt's stored state, e.g. "SUCCEEDED": already done
    store_error: str | None = None  # the store could not be used (type only)
    superseded: bool = False  # the record was changed while this attempt ran (see ActionGuard.reconcile)
    stored_outcome: State | None = None  # the store's state of the action after this attempt, if recorded

    @property
    def attempt_outcome(self) -> Outcome:
        """What this attempt observed itself (``execute``, then its own ``verify``)."""
        return self.verified if self.verified in ("SUCCEEDED", "FAILED") else self.outcome

    @property
    def final_outcome(self) -> Outcome:
        """The outcome to act on. Normally the attempt's own; when it was superseded, the stored,
        authoritative one: ``SUCCEEDED`` if this attempt or the store saw success (it happened),
        ``FAILED`` if the store settled it so, otherwise ``UNKNOWN``. ``outcome``, ``verified`` and
        ``stored_outcome`` keep the details of a conflict."""
        own = self.attempt_outcome
        if not self.superseded:
            return own
        if own == "SUCCEEDED" or self.stored_outcome == "SUCCEEDED":
            return "SUCCEEDED"
        if self.stored_outcome == "FAILED":
            return "FAILED"
        return "UNKNOWN"


class ActionBlocked(Exception):
    """Raised by a :meth:`ActionGuard.protect` wrapper whenever it cannot return the function's own
    return value: denied, not approved, a duplicate, a confirmed failure, or an outcome that is
    unknown or was only established by a lookup. ``result`` tells which; the message has no values."""

    def __init__(self, result: ActionResult) -> None:
        self.result = result
        previous = f"; earlier attempt {result.previous_outcome}" if result.previous_outcome else ""
        if result.superseded:
            previous += f"; superseded: this attempt {result.attempt_outcome}, stored {result.stored_outcome}"
        super().__init__(
            f"{result.decision.action}: {result.final_outcome} ({', '.join(result.decision.reason_codes)}{previous})"
        )


def _require_sync(fn: Any, what: str) -> None:
    for candidate in (fn, getattr(fn, "__call__", None)):  # noqa: B004 - callable objects too
        if candidate is not None and (
            inspect.iscoroutinefunction(candidate)
            or inspect.isasyncgenfunction(candidate)
            or inspect.isgeneratorfunction(candidate)
        ):
            raise TypeError(
                f"{what} must be a synchronous function: aobs.safety does not support async or generator "
                "functions (the action would run outside the guard)"
            )


def _discard(value: Any) -> None:
    """Close an unstarted coroutine or generator so that its body can never run."""
    unstarted_coroutine = inspect.iscoroutine(value) and inspect.getcoroutinestate(value) == inspect.CORO_CREATED
    unstarted_generator = inspect.isgenerator(value) and inspect.getgeneratorstate(value) == inspect.GEN_CREATED
    if unstarted_coroutine or unstarted_generator:
        value.close()


def _lazy(value: Any) -> str | None:
    """The error type for a result whose work has not (all) run inside the guard, else None."""
    if inspect.iscoroutine(value) or inspect.isawaitable(value) or inspect.isasyncgen(value):
        _discard(value)
        return "AsyncActionNotSupported"
    if inspect.isgenerator(value):
        _discard(value)
        return "GeneratorActionNotSupported"
    return None


@dataclass
class ActionGuard:
    """Runs side-effecting actions under a :class:`SafetyPolicy`. Thread-safe. Create one per
    process (or share a durable ``store``): a guard per request with the default memory store
    has no memory of earlier requests. See the module docstring for what is and is not guaranteed."""

    policy: SafetyPolicy
    store: ActionStore = field(default_factory=MemoryActionStore)
    lease_s: float = 300.0  # how long an unexecuted reservation (e.g. awaiting approval) blocks others
    stale_after_s: float = 900.0  # an execution younger than this counts as still running: longer than any action
    clock: Callable[[], float] = field(default=time.time)  # Unix time; injectable for tests
    idempotency: IdempotencyKeys | None = None  # required for actions with an action_key

    def __post_init__(self) -> None:
        if not isinstance(self.policy, SafetyPolicy):
            raise TypeError("policy must be a SafetyPolicy (see load_policy)")
        for name in ("lease_s", "stale_after_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
                raise ValueError(f"{name} must be a positive number of seconds")
        if not callable(self.clock):
            raise TypeError("clock must be a function returning Unix time")
        if self.idempotency is not None and not isinstance(self.idempotency, IdempotencyKeys):
            raise TypeError("idempotency must be an IdempotencyKeys")

    def _key(self, action: str, action_key: str) -> str:
        return self.store.digest("key", action, action_key)

    def _request_digest(self, action: str, request: Mapping[str, Any]) -> str:
        return self.store.digest("request", action, _stable(request))

    def _once_key(self, action: str, rule: ActionRule | None, evidence: Mapping[str, Any]) -> str | None:
        """Digest of the ``once_per`` values, from the verified evidence (they equal the request's)."""
        if rule is None or not rule.once_per:
            return None
        return self.store.digest("once", action, _stable(tuple(str(evidence[n]).strip() for n in rule.once_per)))

    def is_live(self, record: ActionRecord) -> bool:
        """Whether an ``EXECUTING`` record is younger than ``stale_after_s`` on this guard's wall
        clock: then its attempt may still be running and nothing but a confirmed success may change
        it. False does not prove that the attempt has stopped (it may be paused, or the clock may
        have jumped forward); a clock that moves backwards keeps it live longer."""
        if record.state != "EXECUTING":
            return False
        return record.started_at is None or self.clock() - record.started_at < self.stale_after_s

    # ----------------------------------------------------------------- running an action

    def run(
        self,
        action: str,
        request: Mapping[str, Any],
        evidence: Mapping[str, Any],
        execute: Callable[[ActionContext], Any],
        *,
        action_key: str | None = None,
        verify: Callable[[ActionContext], str] | None = None,
        approver: Callable[[ActionContext], bool] | None = None,
    ) -> ActionResult:
        _require_sync(execute, "execute")
        if verify is not None:
            _require_sync(verify, "verify")
        if approver is not None:
            _require_sync(approver, "approver")
        attempt = uuid7()
        rule = self.policy.actions.get(action)
        checked: dict[str, Any] | None = None
        digest = ""
        claim: ActionRecord | None = None
        try:
            if isinstance(request, Mapping):
                checked = _plain(dict(request))
                digest = self._request_digest(action, checked)
            decision = evaluate(self.policy, action, checked if checked is not None else request, evidence)
            if decision.verdict != "DENY" and checked is not None:
                decision, claim = self._claim(decision, attempt, rule, digest, evidence, action_key)
        except Exception:  # e.g. a value that is not plain data, or whose str() raises: fail closed
            decision, claim = Decision("DENY", ("invalid_input",), f"actions.{action}", action), None
        frozen: Mapping[str, Any] = MappingProxyType(checked if checked is not None else {})
        stable = action_key is not None and _valid_key(action_key) and self.idempotency is not None
        context = ActionContext(
            attempt,
            action,
            frozen,
            decision,
            action_key if stable else None,
            self.idempotency.key(action, action_key)  # type: ignore[union-attr,arg-type]
            if stable
            else "aobs-attempt-" + attempt.replace("-", ""),
        )
        subject = {name: frozen.get(name) for name in sorted({*rule.once_per, *rule.must_match})} if rule else {}
        if action_key is not None:
            subject["aobs.action_key"] = action_key  # fingerprinted with the rest, never recorded
        with _Trace(action, attempt, subject, self.store.kind, keyed=action_key is not None) as trace:
            trace.decision(decision)
            if decision.verdict == "DENY":
                return trace.finish(ActionResult(attempt, decision, "not_required", False, "NOT_EXECUTED", "none"))
            if claim is not None:
                try:
                    earlier = self.store.reserve(claim, self.clock(), once_per=rule.once_per if rule else ())
                except PolicyMismatch:  # another guard on this store enforces a different rule
                    return self._refuse(trace, attempt, decision, "policy_mismatch", "guard.store",
                                        store_error="PolicyMismatch")  # fmt: skip
                except Exception as exc:  # the store is unavailable: nothing runs
                    return self._refuse(trace, attempt, decision, "store_unavailable", "guard.store",
                                        store_error=type(exc).__name__)  # fmt: skip
                if earlier is not None:
                    return self._refuse(trace, attempt, decision, *_why(earlier, claim), previous=earlier)
            return self._attempt(trace, context, digest, claim, execute, verify, approver)

    def _claim(
        self,
        decision: Decision,
        attempt: str,
        rule: ActionRule | None,
        digest: str,
        evidence: Mapping[str, Any],
        action_key: str | None,
    ) -> tuple[Decision, ActionRecord | None]:
        """The store reservation for an allowed request, or a denial for a missing or bad key."""
        action = decision.action
        if action_key is None and self.store.durable:
            return Decision(
                "DENY", ("action_key_required",), "guard.action_key", action, decision.evidence_checked
            ), None
        if action_key is not None and not _valid_key(action_key):
            return Decision(
                "DENY", ("action_key_invalid",), "guard.action_key", action, decision.evidence_checked
            ), None
        if action_key is not None and self.idempotency is None:  # no stable provider key: fail closed
            return Decision(
                "DENY", ("idempotency_unconfigured",), "guard.idempotency", action, decision.evidence_checked
            ), None
        once = self._once_key(action, rule, evidence)
        if action_key is None and once is None:
            return decision, None  # nothing identifies this action: no duplicate check (in memory, as configured)
        key = self._key(action, action_key) if action_key is not None else self.store.digest("attempt", attempt)
        lease = self.clock() + self.lease_s
        idempotency_id = self.idempotency.key_id if action_key is not None and self.idempotency else None
        record = ActionRecord(action, key, attempt, "RESERVED", digest, once, lease, idempotency_id=idempotency_id)
        return decision, record

    def _attempt(
        self,
        trace: _Trace,
        context: ActionContext,
        digest: str,
        claim: ActionRecord | None,
        execute: Callable[[ActionContext], Any],
        verify: Callable[[ActionContext], str] | None,
        approver: Callable[[ActionContext], bool] | None,
    ) -> ActionResult:
        attempt, decision = context.action_id, context.decision
        phase = "reserved"
        fence: int | None = None  # the record version this attempt wrote when it began executing
        try:
            approval: Approval = "not_required"
            if decision.verdict == "REQUIRE_APPROVAL":
                approval = _ask(approver, context)
                trace.approval(approval)
                if approval != "granted":
                    self._release(claim)
                    return trace.finish(ActionResult(attempt, decision, approval, False, "NOT_EXECUTED", "none"))
            if self._request_digest(context.action, context.request) != digest:
                self._release(claim)  # a nested value changed after the check (e.g. by the approver)
                return self._refuse(trace, attempt, decision, "request_changed", "guard.request", approval=approval)
            if claim is not None:
                try:
                    now = self.clock()
                    began, current = self.store.update(claim.action, claim.key, attempt, expect=("RESERVED",),
                                                       state="EXECUTING", now=now, started_at=now)  # fmt: skip
                except Exception as exc:
                    self._release(claim)
                    return self._refuse(trace, attempt, decision, "store_unavailable", "guard.store",
                                        approval=approval, store_error=type(exc).__name__)  # fmt: skip
                if not began or current is None:  # the reservation expired and another attempt took it over
                    return self._refuse(trace, attempt, decision, "duplicate_action", "guard.action_key",
                                        approval=approval, previous=current)  # fmt: skip
                fence = current.version
            phase = "executing"  # committed: from here a crash leaves EXECUTING, never a retry
            outcome, value, error_type = _execute(execute, context)
            trace.outcome(outcome, error_type)
            verified: Outcome | None = None
            if outcome == "SUCCEEDED":
                recovery: Recovery = "none"
            elif outcome == "FAILED":
                recovery = "retry_allowed"  # it definitely did not happen; running again is the caller's choice
            elif verify is None:
                recovery = "escalate"
            else:
                verified, verify_error = _verify(verify, context)
                trace.verification(verified, verify_error)
                error_type = error_type or verify_error
                recovery = _RECOVERY_AFTER_VERIFY[verified]
            final: Outcome = verified if verified in ("SUCCEEDED", "FAILED") else outcome
            store_error, superseded, stored = self._finish(
                claim, fence, final, error_type, verified=verified is not None and final != "UNKNOWN"
            )
            phase = "done"
            trace.recovery(recovery)
            result = ActionResult(
                action_id=attempt,
                decision=decision,
                approval=approval,
                executed=True,
                outcome=outcome,
                recovery=recovery,
                value=value if outcome == "SUCCEEDED" else None,
                error_type=error_type,
                verified=verified,
                store_error=store_error,
                superseded=superseded,
                stored_outcome=stored,
            )
            return trace.finish(result)
        except BaseException as exc:  # interrupted (KeyboardInterrupt, SystemExit, ...) or a bug here
            if phase == "executing":
                self._finish(claim, fence, "UNKNOWN", type(exc).__name__)  # it may have happened
                trace.outcome("UNKNOWN", type(exc).__name__)
            elif phase == "reserved":
                self._release(claim)  # it did not run
            raise

    def _refuse(
        self,
        trace: _Trace,
        attempt: str,
        decision: Decision,
        code: str,
        rule: str,
        *,
        approval: Approval = "not_required",
        previous: ActionRecord | None = None,
        store_error: str | None = None,
    ) -> ActionResult:
        denied = Decision("DENY", (code,), rule, decision.action, decision.evidence_checked)
        trace.decision(denied)
        return trace.finish(
            ActionResult(
                attempt,
                denied,
                approval,
                False,
                "NOT_EXECUTED",
                "none",
                duplicate_of=previous.attempt_id if previous else None,
                previous_outcome=previous.state if previous else None,
                store_error=store_error,
            )
        )

    def _release(self, claim: ActionRecord | None) -> None:
        """Free a reservation that never executed. If this fails, its lease expires instead."""
        if claim is None:
            return
        with contextlib.suppress(Exception):
            self.store.release(claim.action, claim.key, claim.attempt_id)

    def _finish(
        self,
        claim: ActionRecord | None,
        fence: int | None,
        final: Outcome,
        error_type: str | None,
        *,
        verified: bool = False,
    ) -> tuple[str | None, bool, State | None]:
        """Record this attempt's outcome, only over the record exactly as this attempt left it
        (state EXECUTING at version ``fence``). Returns (store error type, superseded, the stored
        state afterwards). If the store fails, the record stays EXECUTING and keeps blocking its
        action, which is safe."""
        if claim is None:
            return None, False, None
        state: State = final if final in ("SUCCEEDED", "FAILED") else "UNKNOWN"
        now = self.clock()
        try:
            moved, current = self.store.update(claim.action, claim.key, claim.attempt_id, expect=("EXECUTING",),
                                               expect_version=fence, state=state, now=now,
                                               resolution="verified" if verified else None,
                                               error_type=error_type)  # fmt: skip
            if moved:
                return None, False, state
            superseded, stored = self._settle_late(claim, current, state, error_type, now)
            return None, superseded, stored
        except Exception as exc:
            return type(exc).__name__, False, None

    def _settle_late(
        self, claim: ActionRecord, current: ActionRecord | None, state: State, error_type: str | None, now: float
    ) -> tuple[bool, State | None]:
        """The record changed while this attempt ran (reconcile or resolve). Its outcome may
        replace only what holds no information; a later settled state is never overwritten, except
        that an observed success is kept, marked ``conflict``. Returns (superseded, stored state)."""
        if current is None or current.attempt_id != claim.attempt_id:
            return True, current.state if current is not None else None  # another attempt holds it now
        if current.state == "UNKNOWN" and current.resolution in ("none", "stale"):
            # declared stale while it ran, or unknown: the attempt's own outcome is better, and it has ended
            if state == "UNKNOWN" and current.resolution == "none":
                return False, state
            moved, after = self.store.update(claim.action, claim.key, claim.attempt_id, expect=("UNKNOWN",),
                                             expect_version=current.version, state=state, now=now,
                                             resolution="none", error_type=error_type)  # fmt: skip
            return not moved, after.state if after is not None else None
        if current.state == state:
            return False, state  # the same outcome was already established
        if state == "SUCCEEDED" and current.state == "FAILED":  # it did happen: keep that, so nothing retries
            _, after = self.store.update(claim.action, claim.key, claim.attempt_id, expect=("FAILED",),
                                         expect_version=current.version, state="SUCCEEDED", now=now,
                                         resolution="conflict", error_type=error_type)  # fmt: skip
            return True, after.state if after is not None else None
        return True, current.state

    # ----------------------------------------------------------------- wrapping a tool

    def protect(
        self,
        fn: Callable[..., R],
        *,
        action: str,
        evidence: Callable[[], Mapping[str, Any]],
        action_key: str | Callable[[], str] | None = None,
        verify: Callable[[ActionContext], str] | None = None,
        approver: Callable[[ActionContext], bool] | None = None,
        idempotency_param: str | None = None,
    ) -> Callable[..., R]:
        """Wrap an existing synchronous tool function. The wrapper has the same parameters (minus
        ``idempotency_param``, which the guard fills in) and returns the function's own return value
        when the action succeeds; otherwise it raises :class:`ActionBlocked`. The function is called
        with exactly the checked arguments, only after the decision allows it.

        ``evidence`` and ``action_key`` (a string or a function returning one) are called on each
        call and must come from trusted context, not from the arguments. Mistakes in the
        configuration raise here, before any call."""
        if action not in self.policy.actions:
            raise PolicyError(f"action {action!r} is not in the safety policy")
        _require_sync(fn, "the protected function")
        for what, callback in (("verify", verify), ("approver", approver), ("evidence", evidence)):
            if callback is not None:
                _require_sync(callback, what)
        if not callable(evidence):
            raise TypeError("evidence must be a function that returns the verified evidence")
        if action_key is None and self.store.durable:
            raise TypeError(f"a {self.store.kind} store needs action_key= (the identity of each logical action)")
        if action_key is not None and self.idempotency is None:
            raise TypeError("action_key= needs the guard's idempotency= (IdempotencyKeys) for stable provider keys")
        if isinstance(action_key, str) and not _valid_key(action_key):
            raise ValueError("action_key must be 1-256 characters, without control characters or outer spaces")
        signature = inspect.signature(fn)
        for p in signature.parameters.values():
            if p.kind in (p.POSITIONAL_ONLY, p.VAR_POSITIONAL, p.VAR_KEYWORD):
                raise TypeError(f"protect() needs named parameters; {p.name!r} is {p.kind.description}")
        if idempotency_param is not None and idempotency_param not in signature.parameters:
            raise TypeError(f"{fn.__name__}() has no parameter {idempotency_param!r}")
        exposed = signature.replace(
            parameters=[p for p in signature.parameters.values() if p.name != idempotency_param]
        )

        def call(ctx: ActionContext) -> R:
            arguments = dict(ctx.request)
            if idempotency_param is not None:
                arguments[idempotency_param] = ctx.idempotency_key
            return fn(**arguments)

        @functools.wraps(fn)
        def guarded(*args: Any, **kwargs: Any) -> R:
            bound = exposed.bind(*args, **kwargs)  # wrong arguments raise TypeError, as they would unwrapped
            bound.apply_defaults()
            try:
                trusted: Any = evidence()
            except Exception:
                trusted = None  # no evidence: denied (invalid_input)
            try:
                key = action_key() if callable(action_key) else action_key
            except Exception:
                key = ""  # denied (action_key_invalid)
            result = self.run(action, bound.arguments, trusted, call, action_key=key, verify=verify, approver=approver)
            if result.outcome == "SUCCEEDED":
                return result.value  # type: ignore[no-any-return]
            raise ActionBlocked(result)

        guarded.__signature__ = exposed  # type: ignore[attr-defined]
        return guarded

    # ----------------------------------------------------------------- unresolved actions

    def inspect(self, action: str, action_key: str) -> ActionRecord | None:
        """The stored state of the logical action, or None. Store errors are raised."""
        return self.store.get(action, self._key(action, action_key))

    def reconcile(
        self, action: str, action_key: str, verify: Callable[[ActionContext], str] | None = None
    ) -> ActionRecord | None:
        """Settle an action left ``EXECUTING`` (e.g. its process crashed) or ``UNKNOWN`` without
        repeating it: one read-only ``verify`` lookup (see the module docstring for its contract).

        * A confirmed ``SUCCEEDED`` is recorded (verified) in any case.
        * While the execution is live (:meth:`is_live`: younger than ``stale_after_s``) nothing else
          changes it: "not found", or even "failed", may only mean the request was not sent *yet*.
        * After that, ``FAILED`` is recorded (verified); anything else marks an ``EXECUTING`` record
          ``UNKNOWN`` with resolution ``stale``, for :meth:`resolve`. Stale is not proof that the
          original process stopped.

        Other states are returned unchanged. The action is never executed here, and the record is
        changed only if it is still exactly as read (version check). During reconciliation
        ``ctx.request`` is empty: look the action up by ``ctx.idempotency_key``."""
        if verify is not None:
            _require_sync(verify, "verify")
        key = self._key(action, action_key)
        record = self.store.get(action, key)
        if record is None or record.state not in ("EXECUTING", "UNKNOWN"):
            return record
        live = self.is_live(record)
        if verify is not None and (self.idempotency is None or record.idempotency_id != self.idempotency.key_id):
            raise IdempotencyConfigMismatch(  # the lookup must use the key the action was sent with
                "this action's provider key was made by another idempotency configuration (or none is set): "
                "reconcile it with that configuration, or settle it with resolve()"
            )
        context = ActionContext(
            record.attempt_id,
            action,
            MappingProxyType({}),
            Decision("ALLOW", ("previously_allowed",), "guard.reconcile", action),
            action_key,
            self.idempotency.key(action, action_key) if self.idempotency is not None else "",
        )
        with _Trace(action, uuid7(), {"aobs.action_key": action_key}, self.store.kind, operation="reconcile") as trace:
            verified, error_type = _verify(verify, context) if verify is not None else ("UNKNOWN", None)
            trace.verification(verified, error_type)
            change: tuple[State, Resolution] | None = None
            if verified == "SUCCEEDED":
                change = ("SUCCEEDED", "verified")
            elif live:
                change = None  # it may still be running
            elif verified == "FAILED":
                change = ("FAILED", "verified")
            elif record.state == "EXECUTING":
                change = ("UNKNOWN", "stale")
            current: ActionRecord | None = record
            if change is not None:
                _, current = self.store.update(action, key, record.attempt_id, expect=(record.state,),
                                               expect_version=record.version, state=change[0],
                                               now=self.clock(), resolution=change[1])  # fmt: skip
            trace.settled(record, current, live=live)
            return current

    def resolve(
        self,
        action: str,
        action_key: str,
        outcome: Literal["SUCCEEDED", "FAILED"],
        *,
        original_stopped: bool = False,
    ) -> ActionRecord:
        """Record a person's decision about an ``UNKNOWN`` action, after checking the provider by
        other means. ``FAILED`` lets a new attempt run with the same idempotency key.

        If the action was declared ``stale`` (its process may only be paused), ``FAILED`` also
        requires ``original_stopped=True``: the person's statement that the original process has
        stopped (or that the provider honours the idempotency key, so a late request is a no-op).
        The guard records that statement (resolution ``manual_stopped``) but cannot verify it;
        without it the action stays ``UNKNOWN``. An ``EXECUTING`` action is refused (reconcile it
        once it is stale); settled actions cannot be changed."""
        if outcome not in ("SUCCEEDED", "FAILED"):
            raise ValueError('outcome must be "SUCCEEDED" or "FAILED"')
        key = self._key(action, action_key)
        record = self.store.get(action, key)
        if record is None:
            raise LookupError(f"no {action} action with this action key")
        if record.state != "UNKNOWN":
            if self.is_live(record):
                hint = f"; it is still considered running (stale after {self.stale_after_s:g}s)"
            elif record.state == "EXECUTING":
                hint = "; reconcile() it first"
            else:
                hint = ""
            raise ValueError(f"only an UNKNOWN action can be resolved by hand; this one is {record.state}{hint}")
        stale = record.resolution == "stale"
        if outcome == "FAILED" and stale and original_stopped is not True:
            raise ValueError(
                "this action was declared stale while its process may still be running, and FAILED lets a new "
                "attempt run: confirm that the original process has stopped (or that the provider honours the "
                "idempotency key) and pass original_stopped=True. The guard cannot verify this."
            )
        resolution: Resolution = "manual_stopped" if stale and original_stopped is True else "manual"
        with _Trace(action, uuid7(), {"aobs.action_key": action_key}, self.store.kind, operation="resolve") as trace:
            applied, current = self.store.update(action, key, record.attempt_id, expect=("UNKNOWN",),
                                                 expect_version=record.version, state=outcome,
                                                 now=self.clock(), resolution=resolution)  # fmt: skip
            trace.settled(record, current)
        if not applied or current is None:
            raise ValueError("the action changed while it was being resolved; inspect it again")
        return current


def _why(earlier: ActionRecord, claim: ActionRecord) -> tuple[str, str]:
    if earlier.key != claim.key:
        return "duplicate_action", f"actions.{claim.action}.once_per"
    if earlier.request_digest != claim.request_digest:
        return "action_key_conflict", "guard.action_key"
    if earlier.idempotency_id != claim.idempotency_id:
        return "idempotency_config_changed", "guard.idempotency"
    return "duplicate_action", "guard.action_key"


_RECOVERY_AFTER_VERIFY: dict[Outcome, Recovery] = {
    "SUCCEEDED": "verified_succeeded",
    "FAILED": "verified_failed",
    "UNKNOWN": "escalate",
    "NOT_EXECUTED": "escalate",
}


def _ask(approver: Callable[[ActionContext], bool] | None, context: ActionContext) -> Approval:
    if approver is None:
        return "unavailable"
    try:
        answer = approver(context)
    except Exception:
        return "denied"
    _discard(answer)
    return "granted" if answer is True else "denied"  # only an explicit True approves


def _execute(execute: Callable[[ActionContext], Any], context: ActionContext) -> tuple[Outcome, Any, str | None]:
    try:
        value = execute(context)
    except ActionNotPerformed as exc:
        return "FAILED", None, type(exc).__name__
    except Exception as exc:  # a timeout or anything else: it may have happened
        return "UNKNOWN", None, type(exc).__name__
    lazy = _lazy(value)
    if lazy is not None:  # an awaitable or generator: its work did not (all) run here
        return "UNKNOWN", None, lazy
    return "SUCCEEDED", value, None


def _verify(verify: Callable[[ActionContext], str], context: ActionContext) -> tuple[Outcome, str | None]:
    """One read-only status lookup. Only an explicit "SUCCEEDED" or "FAILED" resolves the outcome."""
    try:
        reported = verify(context)
    except Exception as exc:
        return "UNKNOWN", type(exc).__name__
    _discard(reported)
    if reported == "SUCCEEDED":
        return "SUCCEEDED", None
    if reported == "FAILED":
        return "FAILED", None
    return "UNKNOWN", None


# --------------------------------------------------------------------------- telemetry


class _Trace:
    """Records one guarded run (or a reconciliation) as a ``guardrail`` span. Every method
    swallows its own errors: telemetry must never change what the guard decides or does."""

    def __init__(
        self,
        action: str,
        action_id: str,
        subject: Mapping[str, Any],
        store: str,
        *,
        keyed: bool = True,
        operation: str = "run",
    ) -> None:
        self.span: Any = None
        try:
            attributes: dict[str, Any] = {
                "aobs.safety.action": action,
                "aobs.safety.action_id": action_id,
                "aobs.safety.policy_version": POLICY_VERSION,
                "aobs.safety.store": store,
                "aobs.safety.keyed": keyed,
            }
            if operation != "run":
                attributes["aobs.safety.operation"] = operation
            name = f"action {action}" if operation == "run" else f"action {action} {operation}"
            self.span = _api.span(name, kind="guardrail", attributes=attributes)
        except Exception:
            self.span = None
            return
        try:  # separately: once the span exists it must still be ended, whatever fails here
            if subject:  # a keyed hash of the identifying values; never the values themselves
                self.span.fingerprint_input({name: str(value) for name, value in subject.items()})
        except Exception:
            pass

    def __enter__(self) -> _Trace:
        try:
            if self.span is not None:
                self.span.__enter__()
        except Exception:
            self.span = None
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if self.span is not None:
                self.span.__exit__(*exc)
        except Exception:
            pass

    def _record(self, event: str | None, attributes: Mapping[str, Any]) -> None:
        """A span event (if named) and the same values as span attributes (the latest wins)."""
        try:
            if self.span is not None:
                if event is not None:
                    self.span.event(event, attributes)
                self.span.set_attributes(attributes)
        except Exception:
            pass

    def decision(self, d: Decision) -> None:
        self._record(
            "aobs.safety.decision",
            {
                "aobs.safety.decision": d.verdict,
                "aobs.safety.reason_codes": list(d.reason_codes),
                "aobs.safety.rule": d.rule,
                "aobs.safety.evidence_checked": list(d.evidence_checked),
                "aobs.safety.evidence_missing": list(d.evidence_missing),
            },
        )

    def approval(self, approval: Approval) -> None:
        self._record("aobs.safety.approval", {"aobs.safety.approval": approval})

    def outcome(self, outcome: Outcome, error_type: str | None) -> None:
        attributes: dict[str, Any] = {"aobs.safety.outcome": outcome}
        if error_type:
            attributes["aobs.safety.error_type"] = error_type
        self._record("aobs.safety.outcome", attributes)

    def verification(self, verified: Outcome, error_type: str | None) -> None:
        attributes: dict[str, Any] = {"aobs.safety.verified_outcome": verified}
        if error_type:
            attributes["aobs.safety.verify_error_type"] = error_type
        self._record("aobs.safety.verification", attributes)

    def recovery(self, recovery: Recovery) -> None:
        self._record("aobs.safety.recovery", {"aobs.safety.recovery": recovery, "aobs.safety.auto_retry": False})

    def _status(self, final: str) -> None:
        try:
            if self.span is not None:
                if final == "NOT_EXECUTED":
                    self.span.set_status("cancelled")
                elif final in ("UNKNOWN", "EXECUTING"):
                    self.span.set_error("ActionOutcomeUnknown", origin="tool", retryable=False)
                elif final == "FAILED":
                    self.span.set_error("ActionNotPerformed", origin="tool")
        except Exception:
            pass

    def finish(self, result: ActionResult) -> ActionResult:
        attributes: dict[str, Any] = {"aobs.safety.final_outcome": result.final_outcome}
        if result.duplicate_of:
            attributes["aobs.safety.duplicate_of"] = result.duplicate_of
        if result.previous_outcome:
            attributes["aobs.safety.previous_outcome"] = result.previous_outcome
        if result.store_error:
            attributes["aobs.safety.store_error"] = result.store_error
        if result.superseded:
            attributes["aobs.safety.superseded"] = True
            attributes["aobs.safety.attempt_outcome"] = result.attempt_outcome
        if result.stored_outcome:
            attributes["aobs.safety.stored_outcome"] = result.stored_outcome
        self._record(None, attributes)
        self._status(result.final_outcome)
        return result

    def settled(self, before: ActionRecord, after: ActionRecord | None, *, live: bool = False) -> None:
        """A reconciliation or resolution: which attempt, and its state before and after."""
        state = after.state if after is not None else "UNKNOWN"
        self._record(
            "aobs.safety.resolution",
            {
                "aobs.safety.reconciled_action_id": before.attempt_id,
                "aobs.safety.previous_outcome": before.state,
                "aobs.safety.final_outcome": state,
                "aobs.safety.resolution": after.resolution if after is not None else "none",
                "aobs.safety.live": live,
            },
        )
        if state != "SUCCEEDED":
            self._status(state)
