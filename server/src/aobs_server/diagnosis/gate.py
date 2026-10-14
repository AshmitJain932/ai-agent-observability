"""Reliability gate: check recorded runs against written requirements and answer PASS or FAIL.

A policy file (TOML) lists requirements: per run (outcome, completeness, duration, call counts,
retries, redundant calls, tokens, cost, required/forbidden tools, per-tool limits), per gate
(minimum number of runs, pass rate) and optionally against a baseline run. Each requirement
becomes a check with result PASS, FAIL or UNKNOWN, an expected and an actual value, and evidence
from the trace (span ids and what they show).

Nothing is inferred and missing data is never read as success:

* a value the trace does not contain (usage not reported, a run that never finished) makes the
  check UNKNOWN; the policy decides whether UNKNOWN fails the run (``on_missing_data``);
* on a run that did not finish, counts and totals are lower bounds: a lower bound above the limit
  is a FAIL, anything else is UNKNOWN.

Loading, the privacy step, trace assembly, run profiles and the diagnosis rules are the ones used
by ``analyze`` and ``compare``; the gate adds the requirements and the verdict. See
docs/reliability-gate.md.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_core import ErrorDetails

from ..model import assemble_events
from ..model.assembly import AssemblyConfig, SpanNode, TraceView
from ..model.versioning import AcceptedEvent
from .compare import SUCCESS, RunProfile, money, profile, select_run
from .loader import InputProblem, Source, load
from .rules import RuleConfig, fields, label, repeated_identical_calls, root_cause_errors, short

Result = Literal["pass", "fail", "unknown", "warn"]
Verdict = Literal["pass", "fail"]
MAX_EVIDENCE = 10
PRIVACY_NOTE = "content is never read or shown; error messages are secret-scrubbed"

Count = Annotated[int, Field(ge=0)]
Amount = Annotated[float, Field(ge=0)]


# --------------------------------------------------------------------------- policy


class PolicyError(ValueError):
    """The policy file cannot be read or is invalid."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class GateSection(_Section):
    task: Annotated[str, Field(min_length=1)] | None = None
    min_runs: Annotated[int, Field(ge=1)] = 1
    min_pass_rate: Annotated[float, Field(ge=0, le=1)] = 1.0
    on_missing_data: Literal["fail", "warn"] = "fail"
    on_input_problems: Literal["fail", "warn"] = "fail"


class ToolLimits(_Section):
    max_calls: Count | None = None
    max_failures: Count | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> ToolLimits:
        if self.max_calls is None and self.max_failures is None:
            raise ValueError("set max_calls and/or max_failures")
        return self


class RunSection(_Section):
    require_success: bool = False
    allow_handled_errors: bool = True
    require_complete: bool = False
    max_duration_ms: Amount | None = None
    max_failed_spans: Count | None = None
    max_tool_calls: Count | None = None
    max_llm_calls: Count | None = None
    max_retries: Count | None = None
    max_redundant_calls: Count | None = None
    max_input_tokens: Count | None = None
    max_output_tokens: Count | None = None
    max_cost: str | None = None
    cost_currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    required_tools: list[Annotated[str, Field(min_length=1)]] = []
    forbidden_tools: list[Annotated[str, Field(min_length=1)]] = []
    tools: dict[str, ToolLimits] = {}

    @field_validator("max_cost")
    @classmethod
    def _decimal(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            amount = Decimal(value)
        except InvalidOperation:
            raise ValueError('must be a decimal number as a string, e.g. "0.05"') from None
        if not amount.is_finite() or amount < 0:
            raise ValueError("must be a finite, non-negative decimal number")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> RunSection:
        if (self.max_cost is None) != (self.cost_currency is None):
            raise ValueError("max_cost and cost_currency must be set together")
        overlap = sorted(set(self.required_tools) & set(self.forbidden_tools))
        if overlap:
            raise ValueError(f"tools both required and forbidden: {', '.join(overlap)}")
        return self

    def has_requirements(self) -> bool:
        limits = [v for k, v in self if k.startswith("max_")]
        return (
            self.require_success
            or self.require_complete
            or any(v is not None for v in limits)
            or bool(self.required_tools or self.forbidden_tools or self.tools)
        )


#: Default ``min_duration_increase_ms``: as ``compare``'s ``min_run_ms``, an increase this small is
#: run-to-run noise, however large it is in percent (a 6 ms baseline that takes 59 ms is +883%).
DEFAULT_MIN_DURATION_INCREASE_MS = 100.0


class BaselineSection(_Section):
    forbid_outcome_regression: bool = False
    forbid_new_error_types: bool = False
    forbid_new_tools: bool = False
    forbid_removed_tools: bool = False
    max_duration_increase_pct: Amount | None = None
    min_duration_increase_ms: Amount | None = None  # modifies max_duration_increase_pct; not a requirement
    max_tool_call_increase: Count | None = None
    max_input_token_increase_pct: Amount | None = None

    @model_validator(mode="after")
    def _modifier_needs_its_requirement(self) -> BaselineSection:
        if self.min_duration_increase_ms is not None and self.max_duration_increase_pct is None:
            raise ValueError("min_duration_increase_ms only applies together with max_duration_increase_pct")
        return self

    @property
    def duration_floor_ms(self) -> float:
        floor = self.min_duration_increase_ms
        return DEFAULT_MIN_DURATION_INCREASE_MS if floor is None else floor

    def has_requirements(self) -> bool:
        return any(
            v is True or (v is not None and not isinstance(v, bool))
            for name, v in self
            if name != "min_duration_increase_ms"
        )


class Policy(_Section):
    version: Literal[1]
    gate: GateSection = GateSection()
    run: RunSection = RunSection()
    baseline: BaselineSection | None = None

    @model_validator(mode="after")
    def _has_requirements(self) -> Policy:
        if self.baseline is not None and not self.baseline.has_requirements():
            raise ValueError("the [baseline] section sets no requirement")
        if not self.run.has_requirements() and self.baseline is None:
            raise ValueError("the policy sets no requirement: add some under [run] or [baseline]")
        return self


def _describe(error: ErrorDetails) -> str:
    where = ".".join(str(part) for part in error["loc"]) or "policy"
    if error["type"] == "extra_forbidden":
        return f"{where}: unknown setting"
    if error["type"] == "missing":
        return f"{where}: required"
    message = str(error["msg"]).removeprefix("Value error, ")
    return f"{where}: {message}"


def parse_policy(data: dict[str, Any]) -> Policy:
    try:
        return Policy.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(_describe(e) for e in exc.errors(include_url=False)[:10])
        raise PolicyError(f"invalid policy: {problems}") from None


def load_policy(path: Path) -> Policy:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PolicyError(f"cannot read policy file {path}: {exc.strerror or exc}") from None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PolicyError(f"policy file {path} is not valid TOML: {exc}") from None
    return parse_policy(data)


# --------------------------------------------------------------------------- results


@dataclass(frozen=True)
class Check:
    id: str
    result: Result
    expected: str
    actual: str
    message: str
    evidence: tuple[str, ...] = ()
    span_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "result": self.result,
            "expected": self.expected,
            "actual": self.actual,
            "message": self.message,
            "evidence": list(self.evidence),
            "span_ids": list(self.span_ids),
        }


@dataclass(frozen=True)
class RunResult:
    run: RunProfile
    checks: tuple[Check, ...]
    verdict: Verdict

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.run.source,
            "trace_id": self.run.trace_id,
            "run": self.run.run_name,
            "status": self.run.trace.status,
            "verdict": self.verdict,
            "checks": [c.to_dict() for c in self.checks],
        }


@dataclass(frozen=True)
class GateReport:
    policy_path: str
    policy: Policy
    verdict: Verdict
    gate_checks: tuple[Check, ...]
    runs: tuple[RunResult, ...]
    excluded_runs: int
    problems: tuple[InputProblem, ...]
    baseline: RunProfile | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "policy": self.policy_path,
            "settings": {
                "on_missing_data": self.policy.gate.on_missing_data,
                "on_input_problems": self.policy.gate.on_input_problems,
                "task": self.policy.gate.task,
            },
            "privacy": PRIVACY_NOTE,
            "baseline": None
            if self.baseline is None
            else {
                "source": self.baseline.source,
                "trace_id": self.baseline.trace_id,
                "run": self.baseline.run_name,
                "status": self.baseline.trace.status,
            },
            "input_problems": [str(p) for p in self.problems],
            "gate_checks": [c.to_dict() for c in self.gate_checks],
            "runs_checked": len(self.runs),
            "runs_passed": sum(r.verdict == "pass" for r in self.runs),
            "runs_excluded": self.excluded_runs,
            "runs": [r.to_dict() for r in self.runs],
        }


# --------------------------------------------------------------------------- helpers


def _pass(check_id: str, expected: str, actual: str, message: str, **kw: Any) -> Check:
    return Check(check_id, "pass", expected, actual, message, **kw)


def _fail(check_id: str, expected: str, actual: str, message: str, **kw: Any) -> Check:
    return Check(check_id, "fail", expected, actual, message, **kw)


def _unknown(check_id: str, expected: str, actual: str, message: str, **kw: Any) -> Check:
    return Check(check_id, "unknown", expected, actual, message, **kw)


def _num(value: float | int) -> str:
    return f"{value:,}" if isinstance(value, int) else f"{value:,.1f}"


def _span(node: SpanNode) -> str:
    return f"span {short(node.span_id)} ({label(node)})"


def _nodes(trace: TraceView, span_ids: Iterable[str]) -> list[SpanNode]:
    nodes = [trace.spans[s] for s in span_ids if s in trace.spans]
    return sorted(nodes, key=lambda n: (n.start_time, n.span_id))


def _limited(lines: Sequence[str]) -> tuple[str, ...]:
    if len(lines) <= MAX_EVIDENCE:
        return tuple(lines)
    return (*lines[:MAX_EVIDENCE], f"... and {len(lines) - MAX_EVIDENCE} more")


def _open_nodes(trace: TraceView) -> list[SpanNode]:
    return sorted(
        (n for n in trace.spans.values() if n.lifecycle != "complete"), key=lambda n: (n.start_time, n.span_id)
    )


def _unfinished_note(run: RunProfile) -> str:
    return f"the run did not finish ({len(_open_nodes(run.trace))} span(s) without an end), so this is a lower bound"


def _count_check(
    check_id: str, run: RunProfile, limit: int, span_ids: list[str], what: str, describe: Any = None
) -> Check:
    """At most ``limit`` of something counted from spans. On an unfinished run the count is a lower
    bound: above the limit is a FAIL, otherwise UNKNOWN."""
    nodes = _nodes(run.trace, span_ids)
    count = len(span_ids)
    evidence = _limited([describe(n) if describe else _span(n) for n in nodes])
    expected, actual = f"at most {limit}", str(count)
    ids = tuple(n.span_id for n in nodes)
    if count > limit:
        bound = "at least " if not run.finished else ""
        return _fail(
            check_id, expected, actual, f"{bound}{count} {what}, limit {limit}", evidence=evidence, span_ids=ids
        )
    if not run.finished:
        return _unknown(
            check_id,
            expected,
            actual,
            f"{count} {what} so far; {_unfinished_note(run)}",
            evidence=evidence,
            span_ids=ids,
        )
    return _pass(check_id, expected, actual, f"{count} {what}", evidence=evidence, span_ids=ids)


def _ops(run: RunProfile, kind: str) -> list[Any]:
    return sorted((op for (k, _), op in run.ops.items() if k == kind), key=lambda op: op.name)


def _spans_of(run: RunProfile, kind: str) -> list[str]:
    return [s for op in _ops(run, kind) for s in op.span_ids]


# --------------------------------------------------------------------------- run checks


def _require_success(run: RunProfile, allow_handled: bool) -> Check:
    check_id = "run.require_success"
    accepted = ("ok", "ok_with_errors") if allow_handled else ("ok",)
    expected = " or ".join(accepted)
    status = run.trace.status
    if status in ("in_progress", "incomplete"):
        open_nodes = _open_nodes(run.trace)
        return _unknown(
            check_id,
            expected,
            status,
            "the run never finished, so no final status was recorded",
            evidence=_limited([f"{_span(n)} has no end" for n in open_nodes]),
            span_ids=tuple(n.span_id for n in open_nodes),
        )
    if status in accepted:
        return _pass(check_id, expected, status, f"the run ended with status {status}")
    findings = root_cause_errors(run.trace, RuleConfig())
    evidence = [f"root cause: {f.title}; {f.evidence[0]}" for f in findings]
    span_ids = tuple(s for f in findings for s in f.span_ids)
    if status == "ok_with_errors":
        message = "the run recovered, but a span inside it failed (allow_handled_errors = false)"
    else:
        message = f"the run ended with status {status}"
    return _fail(check_id, expected, status, message, evidence=_limited(evidence), span_ids=span_ids)


def _require_complete(run: RunProfile) -> Check:
    check_id = "run.require_complete"
    trace = run.trace
    open_nodes = _open_nodes(trace)
    orphans = [(trace.spans[s], reason) for s, reason in trace.orphans]
    if not open_nodes and not orphans:
        return _pass(
            check_id, "complete", "complete", f"all {trace.span_count} span(s) ended; no missing or cyclic parents"
        )
    evidence = [f"{_span(n)} started {n.start_time.isoformat()} and has no end" for n in open_nodes]
    evidence += [
        f"{_span(n)} has parent {short(n.parent_span_id or '')}, which is not in the trace"
        if reason == "missing_parent"
        else f"{_span(n)} is part of a parent cycle"
        for n, reason in orphans
    ]
    parts = []
    if open_nodes:
        parts.append(f"{len(open_nodes)} span(s) without an end")
    if orphans:
        parts.append(f"{len(orphans)} span(s) with a missing or cyclic parent")
    span_ids = tuple(n.span_id for n in open_nodes) + tuple(n.span_id for n, _ in orphans)
    return _fail(check_id, "complete", "incomplete", ", ".join(parts), evidence=_limited(evidence), span_ids=span_ids)


def _max_duration(run: RunProfile, limit: float) -> Check:
    check_id = "run.max_duration_ms"
    expected = f"at most {_num(limit)} ms"
    if not run.finished:
        return _unknown(check_id, expected, "unknown", "the run did not finish, so it has no duration")
    if run.root is None:
        return _unknown(
            check_id,
            expected,
            "unknown",
            f"the trace has {len(run.trace.roots)} root spans, so there is no single run duration",
        )
    duration = run.duration_ms
    if duration is None:  # pragma: no cover - a finished single root always has a duration
        return _unknown(check_id, expected, "unknown", "the root span has no duration")
    evidence = (f"{_span(run.root)} took {_num(duration)} ms",)
    actual = f"{_num(duration)} ms"
    ids = (run.root.span_id,)
    if duration > limit:
        return _fail(
            check_id,
            expected,
            actual,
            f"the run took {_num(duration - limit)} ms longer than allowed",
            evidence=evidence,
            span_ids=ids,
        )
    return _pass(check_id, expected, actual, "within the limit", evidence=evidence, span_ids=ids)


def _failed_span_text(node: SpanNode) -> str:
    return f"{_span(node)} ended with status error ({fields(node).get('error', {}).get('type', 'no error type')})"


def _redundant_calls(run: RunProfile, limit: int) -> Check:
    check_id = "run.max_redundant_calls"
    expected = f"at most {limit}"
    findings = repeated_identical_calls(run.trace, RuleConfig())
    redundant = sum(len(f.span_ids) - 1 for f in findings)
    evidence = _limited([f"{f.title}; {f.evidence[0]}" for f in findings])
    ids = tuple(s for f in findings for s in f.span_ids)
    calls = [n for n in run.trace.spans.values() if n.kind in ("tool", "llm") and n.status == "ok"]
    unfingerprinted = _nodes(run.trace, (n.span_id for n in calls if not fields(n).get("input_fingerprint")))
    if redundant > limit:
        return _fail(
            check_id,
            expected,
            str(redundant),
            f"{redundant} successful call(s) repeated an earlier call with identical input",
            evidence=evidence,
            span_ids=ids,
        )
    if unfingerprinted:
        return _unknown(
            check_id,
            expected,
            str(redundant),
            f"{len(unfingerprinted)} of {len(calls)} successful call(s) have no input fingerprint, so repeats "
            "among them cannot be detected",
            evidence=_limited([f"{_span(n)} has no input fingerprint" for n in unfingerprinted]),
            span_ids=tuple(n.span_id for n in unfingerprinted),
        )
    if not run.finished:
        return _unknown(
            check_id,
            expected,
            str(redundant),
            f"{redundant} so far; {_unfinished_note(run)}",
            evidence=evidence,
            span_ids=ids,
        )
    return _pass(
        check_id,
        expected,
        str(redundant),
        f"{redundant} successful call(s) repeated an earlier call with identical input",
        evidence=evidence,
        span_ids=ids,
    )


def _tokens(run: RunProfile, name: str, limit: int) -> Check:
    check_id = f"run.max_{name}"
    expected = f"at most {limit:,}"
    total = run.trace.tokens[name]
    llm_nodes = sorted(
        (n for n in run.trace.spans.values() if n.kind in ("llm", "embedding")), key=lambda n: (n.start_time, n.span_id)
    )
    if total.expected_spans == 0:
        if not run.finished:
            return _unknown(check_id, expected, "0", f"no model calls so far; {_unfinished_note(run)}")
        return _pass(check_id, expected, "0", "the run made no model calls")
    missing = [n for n in llm_nodes if n.lifecycle != "complete" or name not in (fields(n).get("usage") or {})]
    coverage = f"reported by {total.reported_spans} of {total.expected_spans} model call(s)"
    if not missing:
        assert total.value is not None
        actual = f"{total.value:,}"
        if total.value > limit:
            return _fail(check_id, expected, actual, f"{total.value:,} {name.replace('_', ' ')}, {coverage}")
        if not run.finished:
            return _unknown(check_id, expected, actual, f"{coverage}; {_unfinished_note(run)}")
        return _pass(check_id, expected, actual, coverage)
    evidence = _limited([f"{_span(n)} did not report {name}" for n in missing])
    ids = tuple(n.span_id for n in missing)
    if total.value is not None and total.value > limit:
        return _fail(
            check_id,
            expected,
            f">= {total.value:,}",
            f"the reported part alone exceeds the limit ({coverage})",
            evidence=evidence,
            span_ids=ids,
        )
    actual = "unknown" if total.value is None else f">= {total.value:,}"
    return _unknown(check_id, expected, actual, f"usage incomplete: {coverage}", evidence=evidence, span_ids=ids)


def _cost(run: RunProfile, limit: str, currency: str) -> Check:
    check_id = "run.max_cost"
    expected = f"at most {limit} {currency}"
    cost = run.cost()
    if cost.reason == "no model calls":
        if not run.finished:
            return _unknown(check_id, expected, f"0 {currency}", f"no model calls so far; {_unfinished_note(run)}")
        return _pass(check_id, expected, f"0 {currency}", "the run made no model calls")
    if cost.amount is None or cost.currency is None:
        return _unknown(check_id, expected, "unknown", f"no comparable cost: {cost.reason}")
    actual = f"{money(cost.amount)} {cost.currency}"
    if cost.currency != currency:
        return _unknown(check_id, expected, actual, f"the run's cost is in {cost.currency}, the limit in {currency}")
    if cost.amount > Decimal(limit):
        return _fail(check_id, expected, actual, "provider-reported cost above the limit")
    if not run.finished:
        return _unknown(check_id, expected, actual, f"within the limit so far; {_unfinished_note(run)}")
    return _pass(check_id, expected, actual, "provider-reported cost within the limit")


def _tool_names(run: RunProfile) -> dict[str, list[str]]:
    return {op.name: op.span_ids for op in _ops(run, "tool")}


def _required_tool(run: RunProfile, tool: str) -> Check:
    check_id = f"run.required_tools.{tool}"
    spans = _tool_names(run).get(tool, [])
    if spans:
        return _pass(check_id, "called", f"{len(spans)} call(s)", f"tool '{tool}' was called", span_ids=tuple(spans))
    if not run.finished:
        return _unknown(check_id, "called", "not called", f"not called so far; {_unfinished_note(run)}")
    called = ", ".join(sorted(_tool_names(run))) or "none"
    return _fail(
        check_id,
        "called",
        "not called",
        f"tool '{tool}' was never called",
        evidence=(f"tools the run called: {called}",),
    )


def _forbidden_tool(run: RunProfile, tool: str) -> Check:
    check_id = f"run.forbidden_tools.{tool}"
    spans = _tool_names(run).get(tool, [])
    if spans:
        nodes = _nodes(run.trace, spans)
        return _fail(
            check_id,
            "not called",
            f"{len(spans)} call(s)",
            f"forbidden tool '{tool}' was called",
            evidence=_limited([_span(n) for n in nodes]),
            span_ids=tuple(spans),
        )
    if not run.finished:
        return _unknown(check_id, "not called", "not called", f"not called so far; {_unfinished_note(run)}")
    return _pass(check_id, "not called", "not called", f"tool '{tool}' was not called")


def run_checks(run: RunProfile, policy: RunSection) -> list[Check]:
    checks: list[Check] = []
    add = checks.append
    if policy.require_success:
        add(_require_success(run, policy.allow_handled_errors))
    if policy.require_complete:
        add(_require_complete(run))
    if policy.max_duration_ms is not None:
        add(_max_duration(run, policy.max_duration_ms))
    if policy.max_failed_spans is not None:
        failed = [n.span_id for n in run.trace.spans.values() if n.status == "error"]
        add(
            _count_check(
                "run.max_failed_spans", run, policy.max_failed_spans, failed, "failed span(s)", _failed_span_text
            )
        )
    if policy.max_tool_calls is not None:
        add(_count_check("run.max_tool_calls", run, policy.max_tool_calls, _spans_of(run, "tool"), "tool call(s)"))
    if policy.max_llm_calls is not None:
        add(_count_check("run.max_llm_calls", run, policy.max_llm_calls, _spans_of(run, "llm"), "model call(s)"))
    if policy.max_retries is not None:
        add(_count_check("run.max_retries", run, policy.max_retries, run.retries(), "retry attempt(s)"))
    if policy.max_redundant_calls is not None:
        add(_redundant_calls(run, policy.max_redundant_calls))
    if policy.max_input_tokens is not None:
        add(_tokens(run, "input_tokens", policy.max_input_tokens))
    if policy.max_output_tokens is not None:
        add(_tokens(run, "output_tokens", policy.max_output_tokens))
    if policy.max_cost is not None and policy.cost_currency is not None:
        add(_cost(run, policy.max_cost, policy.cost_currency))
    for tool in policy.required_tools:
        add(_required_tool(run, tool))
    for tool in policy.forbidden_tools:
        add(_forbidden_tool(run, tool))
    for tool, limits in policy.tools.items():
        op = run.ops.get(("tool", tool))
        if limits.max_calls is not None:
            add(
                _count_check(
                    f"run.tools.{tool}.max_calls",
                    run,
                    limits.max_calls,
                    op.span_ids if op else [],
                    f"call(s) of '{tool}'",
                )
            )
        if limits.max_failures is not None:
            add(
                _count_check(
                    f"run.tools.{tool}.max_failures",
                    run,
                    limits.max_failures,
                    op.failed_span_ids if op else [],
                    f"failure(s) of '{tool}'",
                    _failed_span_text,
                )
            )
    return checks


# --------------------------------------------------------------------------- baseline checks


def _baseline_unfinished(check_id: str, expected: str) -> Check:
    return _unknown(check_id, expected, "unknown", "the baseline run did not finish, so it cannot serve as a reference")


def _new_error_types(base: RunProfile, cur: RunProfile) -> list[tuple[str, str, list[str]]]:
    found = []
    for key in sorted(cur.ops):
        old = base.ops.get(key)
        for error_type, spans in sorted(cur.ops[key].errors.items()):
            if old is None or error_type not in old.errors:
                found.append((cur.ops[key].label, error_type, spans))
    return found


def baseline_checks(base: RunProfile, cur: RunProfile, policy: BaselineSection) -> list[Check]:
    checks: list[Check] = []
    add = checks.append
    base_tools, cur_tools = _tool_names(base), _tool_names(cur)

    if policy.forbid_outcome_regression:
        check_id, expected = "baseline.forbid_outcome_regression", "no success -> failure"
        if not base.finished:
            add(_baseline_unfinished(check_id, expected))
        elif base.trace.status not in SUCCESS:
            add(
                _pass(
                    check_id,
                    expected,
                    f"{base.trace.status} -> {cur.trace.status}",
                    f"the baseline itself ended with {base.trace.status}, so there is no success to lose",
                )
            )
        elif not cur.finished:
            add(
                _unknown(
                    check_id,
                    expected,
                    f"{base.trace.status} -> {cur.trace.status}",
                    "the run did not finish, so its outcome is unknown",
                )
            )
        elif cur.trace.status not in SUCCESS:
            causes = [f"root cause: {f.title}" for f in root_cause_errors(cur.trace, RuleConfig())]
            add(
                _fail(
                    check_id,
                    expected,
                    f"{base.trace.status} -> {cur.trace.status}",
                    "the baseline succeeded and this run did not",
                    evidence=_limited(causes),
                )
            )
        else:
            add(_pass(check_id, expected, f"{base.trace.status} -> {cur.trace.status}", "both runs succeeded"))

    if policy.forbid_new_error_types:
        check_id, expected = "baseline.forbid_new_error_types", "none"
        new = _new_error_types(base, cur)
        if not base.finished:
            add(_baseline_unfinished(check_id, expected))
        elif new:
            error_lines = [
                f"{op} failed with {etype} on spans {', '.join(short(s) for s in spans)}" for op, etype, spans in new
            ]
            add(
                _fail(
                    check_id,
                    expected,
                    f"{len(new)} new error type(s)",
                    "errors the baseline run did not have",
                    evidence=_limited(error_lines),
                    span_ids=tuple(s for _, _, spans in new for s in spans),
                )
            )
        elif not cur.finished:
            add(_unknown(check_id, expected, "none so far", _unfinished_note(cur)))
        else:
            add(_pass(check_id, expected, "none", "no error type that the baseline did not have"))

    if policy.forbid_new_tools:
        check_id, expected = "baseline.forbid_new_tools", "none"
        added = sorted(set(cur_tools) - set(base_tools))
        if not base.finished:
            add(_baseline_unfinished(check_id, expected))
        elif added:
            add(
                _fail(
                    check_id,
                    expected,
                    ", ".join(added),
                    "tools the baseline run never called",
                    evidence=_limited(
                        [f"tool '{t}': spans {', '.join(short(s) for s in cur_tools[t])}" for t in added]
                    ),
                    span_ids=tuple(s for t in added for s in cur_tools[t]),
                )
            )
        elif not cur.finished:
            add(_unknown(check_id, expected, "none so far", _unfinished_note(cur)))
        else:
            add(_pass(check_id, expected, "none", "only tools the baseline also called"))

    if policy.forbid_removed_tools:
        check_id, expected = "baseline.forbid_removed_tools", "none"
        removed = sorted(set(base_tools) - set(cur_tools))
        if not base.finished:
            add(_baseline_unfinished(check_id, expected))
        elif removed and not cur.finished:
            add(_unknown(check_id, expected, ", ".join(removed), f"not called so far; {_unfinished_note(cur)}"))
        elif removed:
            add(
                _fail(
                    check_id,
                    expected,
                    ", ".join(removed),
                    "tools the baseline called and this run did not",
                    evidence=_limited(
                        [
                            f"baseline called '{t}' on spans {', '.join(short(s) for s in base_tools[t])}"
                            for t in removed
                        ]
                    ),
                )
            )
        else:
            add(_pass(check_id, expected, "none", "every tool the baseline called was called"))

    if policy.max_duration_increase_pct is not None:
        add(_duration_increase(base, cur, policy.max_duration_increase_pct, policy.duration_floor_ms))

    if policy.max_tool_call_increase is not None:
        limit_n = policy.max_tool_call_increase
        check_id, expected = "baseline.max_tool_call_increase", f"at most +{limit_n}"
        b, c = sum(len(v) for v in base_tools.values()), sum(len(v) for v in cur_tools.values())
        actual, evidence = f"{c - b:+d}", (f"baseline {b} tool call(s) -> run {c}",)
        if not base.finished:
            add(_baseline_unfinished(check_id, expected))
        elif c - b > limit_n:
            add(_fail(check_id, expected, actual, "more tool calls than the baseline allows", evidence=evidence))
        elif not cur.finished:
            add(_unknown(check_id, expected, actual, _unfinished_note(cur), evidence=evidence))
        else:
            add(_pass(check_id, expected, actual, "within the allowed increase", evidence=evidence))

    if policy.max_input_token_increase_pct is not None:
        add(_token_increase(base, cur, policy.max_input_token_increase_pct))
    return checks


def _duration_increase(base: RunProfile, cur: RunProfile, limit_pct: float, floor_ms: float) -> Check:
    """FAIL only if the run is slower than the baseline by more than ``limit_pct`` percent **and**
    by more than ``floor_ms`` milliseconds (both limits inclusive)."""
    check_id = "baseline.max_duration_increase_pct"
    expected = f"at most +{_num(limit_pct)}%, or at most +{_num(floor_ms)} ms"
    if not base.finished:
        return _baseline_unfinished(check_id, expected)
    if base.duration_ms is None:
        return _unknown(check_id, expected, "unknown", "the baseline has no single run duration")
    if not cur.finished or cur.duration_ms is None:
        return _unknown(check_id, expected, "unknown", "the run has no duration (unfinished or several roots)")
    delta = cur.duration_ms - base.duration_ms
    evidence = (f"baseline {_num(base.duration_ms)} ms -> run {_num(cur.duration_ms)} ms ({delta:+,.1f} ms)",)
    if delta <= floor_ms:
        actual = f"{delta:+,.1f} ms"
        message = (
            f"the run is at most {_num(floor_ms)} ms slower than the baseline (min_duration_increase_ms): "
            "a difference this small is treated as timing noise, whatever its percentage"
        )
        return _pass(check_id, expected, actual, message, evidence=evidence)
    if base.duration_ms == 0:
        return _unknown(
            check_id,
            expected,
            f"{delta:+,.1f} ms",
            "the baseline duration is 0 ms, so a percentage is undefined",
            evidence=evidence,
        )
    pct = delta / base.duration_ms * 100
    actual = f"{pct:+.1f}% ({delta:+,.1f} ms)"
    if pct > limit_pct:
        return _fail(
            check_id,
            expected,
            actual,
            "the run is slower than the baseline by more than both limits allow",
            evidence=evidence,
        )
    return _pass(check_id, expected, actual, "within the allowed slowdown", evidence=evidence)


def _token_increase(base: RunProfile, cur: RunProfile, limit: float) -> Check:
    check_id, expected = "baseline.max_input_token_increase_pct", f"at most +{_num(limit)}%"
    b_value, b_complete, b_how = base.tokens("input_tokens")
    c_value, c_complete, c_how = cur.tokens("input_tokens")
    no_calls = base.trace.tokens["input_tokens"].expected_spans == 0
    if not b_complete and not no_calls:
        return _unknown(check_id, expected, "unknown", f"baseline usage incomplete: {b_how}")
    base_total = 0 if no_calls else (b_value or 0)
    evidence = (f"baseline {base_total:,} -> run {'unknown' if c_value is None else f'{c_value:,}'} ({c_how})",)
    if base_total == 0:
        if cur.trace.tokens["input_tokens"].expected_spans == 0:
            return _pass(check_id, expected, "+0%", "neither run made model calls")
        return _unknown(
            check_id,
            expected,
            "unknown",
            "the baseline used no input tokens, so a percentage is undefined",
            evidence=evidence,
        )
    if c_value is None:
        return _unknown(check_id, expected, "unknown", f"usage incomplete: {c_how}", evidence=evidence)
    pct = (c_value - base_total) / base_total * 100
    actual = f"{pct:+.1f}%" if c_complete else f">= {pct:+.1f}%"
    if pct > limit:
        return _fail(check_id, expected, actual, "more input tokens than the baseline allows", evidence=evidence)
    if not c_complete:
        return _unknown(check_id, expected, actual, f"usage incomplete: {c_how}", evidence=evidence)
    return _pass(check_id, expected, actual, "within the allowed increase", evidence=evidence)


# --------------------------------------------------------------------------- the gate


def _run_verdict(checks: Sequence[Check], on_missing_data: str) -> Verdict:
    if any(c.result == "fail" for c in checks):
        return "fail"
    if on_missing_data == "fail" and any(c.result == "unknown" for c in checks):
        return "fail"
    return "pass"


def _gate_checks(
    policy: Policy, runs: Sequence[RunResult], problems: Sequence[InputProblem], excluded: int
) -> list[Check]:
    gate = policy.gate
    checks: list[Check] = []
    if problems:
        evidence = _limited([str(p) for p in problems])
        message = f"{len(problems)} input problem(s): malformed lines or rejected batches or events"
        result: Result = "fail" if gate.on_input_problems == "fail" else "warn"
        checks.append(Check("gate.input_valid", result, "no input problems", str(len(problems)), message, evidence))
    else:
        checks.append(
            _pass("gate.input_valid", "no input problems", "0", "every line was read and every event accepted")
        )

    checked = len(runs)
    note = f" ({excluded} run(s) with another task were not checked)" if excluded else ""
    expected = f"at least {gate.min_runs}"
    if checked >= gate.min_runs:
        checks.append(_pass("gate.min_runs", expected, str(checked), f"{checked} run(s) checked{note}"))
    else:
        checks.append(_fail("gate.min_runs", expected, str(checked), f"only {checked} run(s) checked{note}"))

    rate = Decimal(str(gate.min_pass_rate))
    expected = f"at least {float(rate) * 100:g}% of runs pass"
    if checked == 0:
        checks.append(_unknown("gate.pass_rate", expected, "no runs", "no run was checked"))
    else:
        passed = sum(r.verdict == "pass" for r in runs)
        actual = f"{passed} of {checked} ({Decimal(passed) / Decimal(checked) * 100:.0f}%)"
        failed = [
            f"run {r.run.trace_id[:12]} '{r.run.run_name}' in {r.run.source}" for r in runs if r.verdict == "fail"
        ]
        if Decimal(passed) >= rate * Decimal(checked):
            checks.append(
                _pass(
                    "gate.pass_rate",
                    expected,
                    actual,
                    f"{passed} of {checked} run(s) passed",
                    evidence=_limited(failed),
                )
            )
        else:
            checks.append(
                _fail(
                    "gate.pass_rate",
                    expected,
                    actual,
                    f"{checked - passed} of {checked} run(s) failed",
                    evidence=_limited(failed),
                )
            )
    return checks


def _load_runs(
    paths: Sequence[Source], now: datetime, error_messages: bool
) -> tuple[list[RunProfile], list[InputProblem]]:
    events: list[AcceptedEvent] = []
    sources: dict[str, str] = {}
    resources: dict[str, dict[str, Any]] = {}
    problems: list[InputProblem] = []
    for path in paths:  # one file at a time, to know where each run came from
        telemetry = load([path], error_messages=error_messages)
        problems.extend(telemetry.problems)
        events.extend(telemetry.events)
        for item in telemetry.events:
            sources.setdefault(item.event.trace_id, str(path))
        for trace_id, resource in telemetry.resources.items():
            resources.setdefault(trace_id, resource)
    traces = assemble_events(events, now, AssemblyConfig())  # together: a run split across files stays one run
    runs = [profile(t, resources.get(tid, {}), sources.get(tid, "?")) for tid, t in traces.items()]
    runs.sort(key=lambda r: (r.source, r.trace.started_at is None, r.trace.started_at, r.trace_id))
    return runs, problems


def load_baseline(
    path: Source, now: datetime, trace_prefix: str | None, error_messages: bool
) -> tuple[RunProfile, list[InputProblem]]:
    """The one baseline run in ``path`` (or the one whose trace id starts with ``trace_prefix``).
    Raises ``NoRunsError`` or ``SelectionError`` from ``compare``."""
    telemetry = load([path], error_messages=error_messages)
    traces = assemble_events(telemetry.events, now, AssemblyConfig())
    trace = select_run(traces, trace_prefix, str(path))
    return profile(trace, telemetry.resources.get(trace.trace_id, {}), str(path)), list(telemetry.problems)


def build_gate_report(
    policy: Policy,
    paths: Sequence[Source],
    now: datetime,
    *,
    policy_path: str = "<policy>",
    baseline: RunProfile | None = None,
    baseline_problems: Sequence[InputProblem] = (),
    error_messages: bool = True,
) -> GateReport:
    if (policy.baseline is None) != (baseline is None):
        raise PolicyError(
            "the policy has a [baseline] section but no baseline run was given (--baseline)"
            if baseline is None
            else "a baseline run was given but the policy has no [baseline] section"
        )
    runs, problems = _load_runs(paths, now, error_messages)
    problems = [*baseline_problems, *problems]
    task = policy.gate.task
    checked = [r for r in runs if task is None or r.run_name == task]
    excluded = len(runs) - len(checked)
    results = []
    for run in checked:
        checks = run_checks(run, policy.run)
        if policy.baseline is not None and baseline is not None:
            checks += baseline_checks(baseline, run, policy.baseline)
        results.append(RunResult(run, tuple(checks), _run_verdict(checks, policy.gate.on_missing_data)))
    gate_checks = _gate_checks(policy, results, problems, excluded)
    verdict: Verdict = "fail" if any(c.result in ("fail", "unknown") for c in gate_checks) else "pass"
    return GateReport(
        policy_path, policy, verdict, tuple(gate_checks), tuple(results), excluded, tuple(problems), baseline
    )


# --------------------------------------------------------------------------- text rendering


def _check_lines(check: Check, show_details: bool) -> list[str]:
    lines = [
        f"  {check.result.upper():<8} {check.id}: {check.message}  [expected {check.expected}, actual {check.actual}]"
    ]
    if show_details:
        lines += [f"             - {item}" for item in check.evidence]
    return lines


def render_gate_text(report: GateReport) -> str:
    lines: list[str] = []
    add = lines.append
    gate = report.policy.gate
    add(f"aobs reliability gate: {report.verdict.upper()}")
    add(
        f"policy: {report.policy_path}  (missing data: {gate.on_missing_data}; input problems: "
        f"{gate.on_input_problems}{'; task: ' + gate.task if gate.task else ''})"
    )
    add(f"privacy: {PRIVACY_NOTE}")
    if report.baseline is not None:
        b = report.baseline
        add(f"baseline: {b.source}  trace {b.trace_id[:12]}  run '{b.run_name}'  status={b.trace.status}")
    add("")
    add("Gate")
    for check in report.gate_checks:
        lines += _check_lines(check, check.result != "pass")
    for result in report.runs:
        run = result.run
        add("")
        add(
            f"Run {run.trace_id[:12]} '{run.run_name}'  status={run.trace.status}  ({run.source}): "
            f"{result.verdict.upper()}"
        )
        for check in result.checks:
            lines += _check_lines(check, check.result != "pass")
    add("")
    passed = sum(r.verdict == "pass" for r in report.runs)
    add(
        f"Result: {report.verdict.upper()} - {passed} of {len(report.runs)} run(s) passed. "
        "Details per span: `aobs-server analyze FILE`; differences to a baseline: `aobs-server compare`."
    )
    add("")
    return "\n".join(lines)
