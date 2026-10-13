"""Cross-run regression detection: compare a baseline run with one or more later runs.

Runs are loaded exactly like ``analyze`` (strict parsing, schema validation, the ingest privacy
step with a metadata-only ceiling, trace assembly). Each run is reduced to a profile, and
operations are matched across runs by a **stable identity** - span kind plus tool name (or span
name) - never by span id, which is random per run.

Findings separate what was measured from what it might mean: every finding carries the baseline
and current values, the measured difference with units, the spans it is based on, why it can
matter, explicitly unverified possible explanations, and a next step. A difference between two
runs does not show what caused it.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..model import assemble_events
from ..model.assembly import AssemblyConfig, SpanNode, TraceView
from .loader import InputProblem, load
from .rules import SEVERITY_ORDER, RuleConfig, Severity, fields, root_cause_errors, short

OpKey = tuple[str, str]  # (kind, operation name)
FINISHED = ("ok", "ok_with_errors", "error", "cancelled")
SUCCESS = ("ok", "ok_with_errors")
VOLUME_RULES = frozenset(
    {
        "regression.more_calls",
        "regression.more_retries",
        "regression.slower_run",
        "regression.slower_operation",
        "regression.more_tokens",
        "regression.higher_cost",
    }
)
DISCLAIMER = (
    "Measured differences are facts read from the two traces; explanations are possibilities to check, "
    "not conclusions. One run per side is a single sample: timings vary between runs."
)


@dataclass(frozen=True)
class CompareConfig:
    min_change_pct: float = 20.0  # a duration or token change must be at least this large ...
    min_run_ms: float = 100.0  # ... and the run's duration must change by at least this much ...
    min_op_ms: float = 50.0  # ... or an operation's median duration by at least this much


class SelectionError(ValueError):
    """The run to compare could not be chosen unambiguously from a file."""


class NoRunsError(SelectionError):
    """A file holds no readable runs at all."""


# --------------------------------------------------------------------------- run profiles


@dataclass
class OpStats:
    kind: str
    name: str
    calls: int = 0
    failed: int = 0
    durations: list[float] = field(default_factory=list)
    span_ids: list[str] = field(default_factory=list)
    failed_span_ids: list[str] = field(default_factory=list)
    retry_span_ids: list[str] = field(default_factory=list)
    models: set[str] = field(default_factory=set)
    errors: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))  # error type -> span ids

    @property
    def label(self) -> str:
        return f"{self.kind} '{self.name}'"

    @property
    def median_ms(self) -> float | None:
        return statistics.median(self.durations) if self.durations else None


@dataclass(frozen=True)
class Cost:
    amount: Decimal | None
    currency: str | None
    reason: str | None  # why there is no comparable amount


@dataclass
class RunProfile:
    source: str
    trace: TraceView
    resource: dict[str, Any]
    ops: dict[OpKey, OpStats]
    root_causes: list[str]

    @property
    def trace_id(self) -> str:
        return self.trace.trace_id

    @property
    def root(self) -> SpanNode | None:
        return self.trace.spans[self.trace.roots[0]] if len(self.trace.roots) == 1 else None

    @property
    def run_name(self) -> str | None:
        return self.root.name if self.root else None

    @property
    def agent(self) -> str | None:
        return fields(self.root).get("agent", {}).get("name") if self.root else None

    @property
    def finished(self) -> bool:
        return self.trace.status in FINISHED and self.trace.open_span_count == 0

    @property
    def duration_ms(self) -> float | None:
        return self.root.duration_ms if self.root and self.finished else None

    def tokens(self, name: str) -> tuple[int | None, bool, str]:
        """(value, complete, description) for a token count summed over the run's model calls."""
        total = self.trace.tokens[name]
        complete = total.expected_spans > 0 and total.reported_spans == total.expected_spans
        return total.value, complete, f"{total.reported_spans} of {total.expected_spans} model call(s) reported it"

    def retries(self) -> list[str]:
        return [s for op in self.ops.values() for s in op.retry_span_ids]

    def cost(self) -> Cost:
        calls = [n for n in self.trace.spans.values() if n.kind in ("llm", "embedding")]
        if not calls:
            return Cost(None, None, "no model calls")
        costs = [fields(n).get("cost") for n in calls]
        if any(c is None for c in costs):
            reported = sum(c is not None for c in costs)
            return Cost(None, None, f"provider-reported cost on {reported} of {len(calls)} model call(s)")
        currencies = {c["currency"] for c in costs if c}
        if len(currencies) != 1:
            return Cost(None, None, f"costs in several currencies ({', '.join(sorted(currencies))})")
        return Cost(sum((Decimal(c["amount"]) for c in costs if c), Decimal(0)), currencies.pop(), None)

    def summary(self) -> dict[str, Any]:
        tokens = {}
        for name in ("input_tokens", "output_tokens"):
            value, complete, how = self.tokens(name)
            tokens[name] = {"value": value, "complete": complete, "coverage": how}
        cost = self.cost()
        return {
            "source": self.source,
            "trace_id": self.trace_id,
            "run": self.run_name,
            "agent": self.agent,
            "status": self.trace.status,
            "duration_ms": self.duration_ms,
            "span_count": self.trace.span_count,
            "error_count": self.trace.error_count,
            "retries": len(self.retries()),
            "tool_calls": {op.name: op.calls for op in sorted(self.ops.values(), key=_op_sort) if op.kind == "tool"},
            "models": sorted({m for op in self.ops.values() for m in op.models}),
            "tokens": tokens,
            "cost": None if cost.amount is None else {"amount": money(cost.amount), "currency": cost.currency},
            "service": self.resource.get("service_name"),
            "environment": self.resource.get("environment"),
        }


def money(amount: Decimal) -> str:
    """Plain decimal notation without exponent or trailing zeros: Decimal('0.00500') -> '0.005'."""
    text = format(amount.normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".") or "0"


def _op_sort(op: OpStats) -> tuple[str, str]:
    return (op.kind, op.name)


def profile(trace: TraceView, resource: dict[str, Any], source: str) -> RunProfile:
    ops: dict[OpKey, OpStats] = {}
    roots = set(trace.roots)
    for node in sorted(trace.spans.values(), key=lambda n: (n.start_time, n.span_id)):
        if node.span_id in roots:
            continue
        f = fields(node)
        name = f.get("tool", {}).get("name") or node.name
        op = ops.setdefault((node.kind, name), OpStats(node.kind, name))
        op.calls += 1
        op.span_ids.append(node.span_id)
        if node.duration_ms is not None:
            op.durations.append(node.duration_ms)
        if node.status == "error":
            op.failed += 1
            op.failed_span_ids.append(node.span_id)
            op.errors[f.get("error", {}).get("type", "unknown error")].append(node.span_id)
        retry = f.get("retry", {})
        links = f.get("links", [])
        if retry.get("attempt", 1) > 1 or any(link.get("relation") == "retry_of" for link in links):
            op.retry_span_ids.append(node.span_id)
        model = f.get("llm", {}).get("response_model") or f.get("llm", {}).get("request_model")
        if model:
            op.models.add(model)
    causes = [finding.title for finding in root_cause_errors(trace, RuleConfig())]
    return RunProfile(source, trace, resource, ops, causes)


# --------------------------------------------------------------------------- findings


@dataclass(frozen=True)
class Change:
    rule: str
    severity: Severity
    title: str
    baseline: str
    current: str
    difference: str
    evidence: tuple[str, ...]
    why: str
    possible_causes: tuple[str, ...]
    next_step: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "title": self.title,
            "baseline": self.baseline,
            "current": self.current,
            "difference": self.difference,
            "evidence": list(self.evidence),
            "why_it_matters": self.why,
            "possible_causes": list(self.possible_causes),
            "next_step": self.next_step,
        }


def _ms(value: float | None) -> str:
    return "unknown" if value is None else f"{value:,.1f} ms"


def _pct(old: float, new: float) -> str:
    return "n/a (baseline was 0)" if old == 0 else f"{(new - old) / old * 100:+.0f}%"


def _ids(ids: list[str], limit: int = 6) -> str:
    shown = ", ".join(short(i) for i in ids[:limit])
    return shown + (f" (+{len(ids) - limit} more)" if len(ids) > limit else "") if ids else "none"


def _significant(old: float, new: float, min_abs: float, min_pct: float) -> bool:
    return new - old >= min_abs and (old == 0 or (new - old) / old * 100 >= min_pct)


def compare_runs(
    base: RunProfile, cur: RunProfile, config: CompareConfig | None = None
) -> tuple[list[Change], list[str]]:
    """Findings (regressions, changes, data gaps) and notes on what could not be compared."""
    cfg = config or CompareConfig()
    changes: list[Change] = []
    notes: list[str] = []
    add = changes.append

    # ---- comparability
    if (base.run_name, base.agent) != (cur.run_name, cur.agent):
        add(
            Change(
                "compare.different_task",
                "warning",
                "the two runs look like different tasks",
                f"run '{base.run_name}', agent '{base.agent}'",
                f"run '{cur.run_name}', agent '{cur.agent}'",
                "run name or agent differs",
                (
                    f"baseline root span {short(base.trace.roots[0]) if base.trace.roots else 'none'}",
                    f"current root span {short(cur.trace.roots[0]) if cur.trace.roots else 'none'}",
                ),
                "Differences between different tasks are expected and say little about a regression.",
                ("the files were recorded from different scenarios or agents",),
                "Compare runs of the same task and agent, e.g. the same test case before and after a change.",
            )
        )
    for side, run in (("baseline", base), ("current", cur)):
        if not run.finished:
            add(
                Change(
                    "data.unfinished_run",
                    "warning",
                    f"the {side} run did not finish, so its duration and outcome are not compared",
                    base.trace.status,
                    cur.trace.status,
                    f"{run.trace.open_span_count} span(s) without an end",
                    tuple(
                        f"open span {short(n.span_id)} ({n.kind} {n.name})"
                        for n in run.trace.spans.values()
                        if n.lifecycle != "complete"
                    )[:6],
                    "A run that never ended has no total duration and no final status to compare.",
                    ("the process crashed or was killed", "the run was still in progress when the file was written"),
                    f"Run `aobs-server analyze {run.source}` to see where it stopped.",
                )
            )

    # ---- outcome
    if base.finished and cur.finished:
        if base.trace.status in SUCCESS and cur.trace.status not in SUCCESS:
            add(
                Change(
                    "regression.run_failed",
                    "error",
                    f"the run succeeded in the baseline and now ends with status {cur.trace.status}",
                    base.trace.status,
                    cur.trace.status,
                    "outcome changed from success to failure",
                    tuple(f"current root cause: {c}" for c in cur.root_causes) or ("no failing child span recorded",),
                    "The same task no longer completes.",
                    (
                        "a dependency used by the failing operation changed or is unavailable",
                        "a code, prompt or model change altered what the agent does",
                    ),
                    f"Run `aobs-server analyze {cur.source}` for the failing span's details, then look at what "
                    "changed in that operation between the two runs.",
                )
            )
        elif base.trace.status not in SUCCESS and cur.trace.status in SUCCESS:
            add(
                Change(
                    "improvement.run_fixed",
                    "info",
                    f"the run failed in the baseline ({base.trace.status}) and now succeeds",
                    base.trace.status,
                    cur.trace.status,
                    "outcome changed from failure to success",
                    tuple(f"baseline root cause: {c}" for c in base.root_causes),
                    "Confirms a fix, if the runs exercise the same task.",
                    (),
                    "Keep this pair as a regression test case.",
                )
            )

    # ---- errors per operation
    for key in sorted(set(base.ops) | set(cur.ops)):
        b = base.ops.get(key) or OpStats(*key)
        c = cur.ops.get(key) or OpStats(*key)
        for error_type in sorted(set(c.errors) - set(b.errors)):
            spans = c.errors[error_type]
            add(
                Change(
                    "regression.new_error",
                    "error",
                    f"new error: {c.label} failed with {error_type}",
                    "no such error",
                    f"{len(spans)} failure(s) with {error_type}",
                    f"+{len(spans)} failure(s)",
                    (
                        f"current spans {_ids(spans)}",
                        f"baseline: {b.failed} failure(s) in {b.calls} call(s)" if b.calls else "baseline: not called",
                    ),
                    "An error type that did not occur before points to a new failure mode.",
                    ("the operation's dependency or input changed", "the code path handling this case changed"),
                    f"Inspect span {short(spans[0])} with `aobs-server analyze {cur.source}`.",
                )
            )
        shared = set(b.errors) & set(c.errors)
        more = [t for t in sorted(shared) if len(c.errors[t]) > len(b.errors[t])]
        for error_type in more:
            add(
                Change(
                    "regression.more_failures",
                    "error",
                    f"{c.label} failed more often with {error_type}",
                    f"{len(b.errors[error_type])} failure(s)",
                    f"{len(c.errors[error_type])} failure(s)",
                    f"+{len(c.errors[error_type]) - len(b.errors[error_type])} failure(s)",
                    (f"baseline spans {_ids(b.errors[error_type])}", f"current spans {_ids(c.errors[error_type])}"),
                    "The same error now happens more often.",
                    ("the dependency became less reliable", "retries now repeat a failing call"),
                    "Check whether the extra failures are retries of one call or separate calls.",
                )
            )

    # ---- tools and call counts
    for key in sorted(set(base.ops) | set(cur.ops)):
        old_op, new_op = base.ops.get(key), cur.ops.get(key)
        what = "tool" if key[0] == "tool" else "operation"
        if old_op is None and new_op is not None:
            add(
                Change(
                    f"change.{what}_added",
                    "info",
                    f"new {what} call: {new_op.label}",
                    "not called",
                    f"{new_op.calls} call(s)",
                    f"+{new_op.calls} call(s)",
                    (f"current spans {_ids(new_op.span_ids)}",),
                    f"A {what} that was not used before changes the run's behaviour, latency and failure surface.",
                    ("the agent's plan or prompt changed", "the input led the agent down a different path"),
                    "Confirm the new call is intended for this task.",
                )
            )
        elif new_op is None and old_op is not None:
            add(
                Change(
                    f"change.{what}_removed",
                    "info",
                    f"{what} no longer called: {old_op.label}",
                    f"{old_op.calls} call(s)",
                    "not called",
                    f"-{old_op.calls} call(s)",
                    (f"baseline spans {_ids(old_op.span_ids)}",),
                    "A step the baseline performed is missing; the result may be incomplete.",
                    ("the agent skipped the step", "the run failed before reaching it"),
                    "Check whether the current run still does everything the task needs.",
                )
            )
        elif old_op is not None and new_op is not None and new_op.calls > old_op.calls:
            add(
                Change(
                    "regression.more_calls",
                    "warning",
                    f"{new_op.label} is called more often",
                    f"{old_op.calls} call(s)",
                    f"{new_op.calls} call(s)",
                    f"+{new_op.calls - old_op.calls} call(s) ({_pct(old_op.calls, new_op.calls)})",
                    (f"baseline spans {_ids(old_op.span_ids)}", f"current spans {_ids(new_op.span_ids)}"),
                    "Extra calls add latency and cost, and can be the start of a loop.",
                    ("retries of a failing call", "the agent repeats a request whose result it lost"),
                    "Look at what precedes each extra call; `aobs-server analyze` flags identical repeated input.",
                )
            )

    # ---- retries
    b_retries, c_retries = base.retries(), cur.retries()
    if len(c_retries) > len(b_retries):
        add(
            Change(
                "regression.more_retries",
                "warning",
                "more retry attempts",
                f"{len(b_retries)} retry attempt(s)",
                f"{len(c_retries)} retry attempt(s)",
                f"+{len(c_retries) - len(b_retries)} attempt(s)",
                (f"current retry spans {_ids(c_retries)}",),
                "Retries hide failures from the final status but cost time and money.",
                ("a dependency became slower or flakier", "a retry policy changed"),
                "Check the first attempt's error in the current run.",
            )
        )

    # ---- durations
    if base.duration_ms is not None and cur.duration_ms is not None:
        old, new = base.duration_ms, cur.duration_ms
        if _significant(old, new, cfg.min_run_ms, cfg.min_change_pct):
            evidence = [
                f"baseline root {short(base.trace.roots[0])}: {_ms(old)}",
                f"current root {short(cur.trace.roots[0])}: {_ms(new)}",
            ]
            # Where the extra time went: change in each operation's total time (nested spans overlap
            # their parents, so these do not add up to the run's change).
            growth = []
            for key in set(base.ops) | set(cur.ops):
                b_total = sum(base.ops[key].durations) if key in base.ops else 0.0
                c_total = sum(cur.ops[key].durations) if key in cur.ops else 0.0
                if c_total - b_total >= max(cfg.min_op_ms, 0.1 * (new - old)):
                    label = (cur.ops.get(key) or base.ops[key]).label
                    growth.append((c_total - b_total, label, b_total, c_total, key in base.ops))
            for delta, label, b_total, c_total, existed in sorted(growth, key=lambda g: (-g[0], g[1]))[:4]:
                where = f"{_ms(b_total)} -> {_ms(c_total)}" if existed else f"new, {_ms(c_total)}"
                evidence.append(f"{label}: total time {where} (+{delta:,.1f} ms)")
            add(
                Change(
                    "regression.slower_run",
                    "warning",
                    "the run is slower",
                    _ms(old),
                    _ms(new),
                    f"+{new - old:,.1f} ms ({_pct(old, new)})",
                    tuple(evidence),
                    "Users wait longer; timeouts become more likely.",
                    ("a slower dependency or model", "additional or repeated calls", "normal run-to-run variation"),
                    "Repeat both runs a few times to confirm, then start with the operations listed above.",
                )
            )
    for key in sorted(set(base.ops) & set(cur.ops)):
        b, c = base.ops[key], cur.ops[key]
        if b.median_ms is None or c.median_ms is None:
            continue
        if _significant(b.median_ms, c.median_ms, cfg.min_op_ms, cfg.min_change_pct):
            add(
                Change(
                    "regression.slower_operation",
                    "warning",
                    f"{c.label} is slower per call",
                    f"median {_ms(b.median_ms)} over {len(b.durations)} call(s)",
                    f"median {_ms(c.median_ms)} over {len(c.durations)} call(s)",
                    f"+{c.median_ms - b.median_ms:,.1f} ms per call ({_pct(b.median_ms, c.median_ms)})",
                    (f"baseline spans {_ids(b.span_ids)}", f"current spans {_ids(c.span_ids)}"),
                    "This operation contributes more time to every run that uses it.",
                    ("the dependency behind it is slower", "larger inputs or outputs", "a different model or settings"),
                    "Compare the operation's inputs and its dependency's health between the two runs.",
                )
            )

    # ---- models and tokens
    for key in sorted(set(base.ops) & set(cur.ops)):
        b, c = base.ops[key], cur.ops[key]
        if b.models and c.models and b.models != c.models:
            add(
                Change(
                    "change.model",
                    "info",
                    f"{c.label} uses a different model",
                    ", ".join(sorted(b.models)),
                    ", ".join(sorted(c.models)),
                    "model changed",
                    (f"baseline spans {_ids(b.span_ids)}", f"current spans {_ids(c.span_ids)}"),
                    "A model change can change quality, latency, token use and cost at the same time.",
                    (),
                    "Treat other differences in this report with the model change in mind.",
                )
            )
    for name, label in (("input_tokens", "input tokens"), ("output_tokens", "output tokens")):
        b_val, b_complete, b_how = base.tokens(name)
        c_val, c_complete, c_how = cur.tokens(name)
        if b_val is None and c_val is None:
            continue
        if not (b_complete and c_complete):
            add(
                Change(
                    "data.incomplete_usage",
                    "info",
                    f"{label} not compared: usage is missing in at least one run",
                    f"{b_val if b_val is not None else 'unknown'} ({b_how})",
                    f"{c_val if c_val is not None else 'unknown'} ({c_how})",
                    "not computed",
                    ("token totals are only compared when every model call in both runs reported them",),
                    "A partial total would make a run look cheaper or more expensive than it was.",
                    (),
                    "Record usage for every model call (for OpenAI-compatible streaming: include_usage).",
                )
            )
            continue
        assert b_val is not None and c_val is not None
        if _significant(b_val, c_val, 1, cfg.min_change_pct):
            add(
                Change(
                    "regression.more_tokens",
                    "warning",
                    f"the run uses more {label}",
                    f"{b_val:,} tokens",
                    f"{c_val:,} tokens",
                    f"+{c_val - b_val:,} tokens ({_pct(b_val, c_val)})",
                    tuple(
                        f"{op.label}: {len(op.span_ids)} call(s), models {', '.join(sorted(op.models)) or 'unknown'}"
                        for op in sorted(cur.ops.values(), key=_op_sort)
                        if op.kind in ("llm", "embedding")
                    ),
                    "More tokens usually means higher cost and latency.",
                    ("longer prompts or more context", "more model calls", "a different model or tokenizer"),
                    "Compare prompt sizes per model call between the two runs.",
                )
            )

    # ---- cost (provider-reported only; never estimated)
    b_cost, c_cost = base.cost(), cur.cost()
    if b_cost.amount is not None and c_cost.amount is not None and b_cost.currency == c_cost.currency:
        if c_cost.amount > b_cost.amount and (
            b_cost.amount == 0 or (c_cost.amount - b_cost.amount) / b_cost.amount * 100 >= cfg.min_change_pct
        ):
            increase = c_cost.amount - b_cost.amount
            pct = "" if b_cost.amount == 0 else f" ({float(increase / b_cost.amount) * 100:+.0f}%)"
            add(
                Change(
                    "regression.higher_cost",
                    "warning",
                    "provider-reported cost increased",
                    f"{money(b_cost.amount)} {b_cost.currency}",
                    f"{money(c_cost.amount)} {c_cost.currency}",
                    f"+{money(increase)} {c_cost.currency}{pct}",
                    ("sum of provider-reported cost on every model call in each run",),
                    "Per-run cost multiplies across every run in production.",
                    ("more tokens", "a more expensive model", "more model calls"),
                    "See the token and model findings in this report.",
                )
            )
    elif not (b_cost.reason == c_cost.reason == "no model calls"):
        reasons = [f"{side}: {x.reason}" for side, x in (("baseline", b_cost), ("current", c_cost)) if x.reason]
        if b_cost.currency and c_cost.currency and b_cost.currency != c_cost.currency:
            reasons.append(f"different currencies ({b_cost.currency} vs {c_cost.currency})")
        notes.append(
            "cost not compared: "
            + "; ".join(reasons)
            + ". aobs never estimates prices from usage (no price table yet)."
        )
    notes.append(
        "operations are matched by kind and tool/span name; several calls with the same name are compared as a group "
        "(count, failures, median duration), regardless of where they appear in the tree"
    )

    if base.finished and cur.finished and base.trace.status not in SUCCESS and cur.trace.status in SUCCESS:
        # The baseline gave up early, so "more work" in the current run is expected, not a regression.
        early = "the baseline run failed and stopped early, so it did less work"
        changes = [
            replace(c, severity="info", possible_causes=(early, *c.possible_causes)) if c.rule in VOLUME_RULES else c
            for c in changes
        ]
        notes.insert(0, "the baseline failed, so increases in time, calls, tokens and cost are reported as info only")

    def order(change: Change) -> tuple[int, int, str]:
        prefix = change.rule.split(".")[0]
        rank = {"compare": 0, "data": 1, "regression": 2, "change": 3, "improvement": 4}.get(prefix, 5)
        first = 0 if change.rule == "regression.run_failed" else 1
        return (SEVERITY_ORDER[change.severity], rank * 2 + first, change.title)

    return sorted(changes, key=order), notes


# --------------------------------------------------------------------------- loading and selection


@dataclass(frozen=True)
class Comparison:
    baseline: RunProfile
    current: RunProfile
    changes: list[Change]
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline.summary(),
            "current": self.current.summary(),
            "findings": [c.to_dict() for c in self.changes],
            "notes": self.notes,
        }


@dataclass(frozen=True)
class ComparisonReport:
    comparisons: list[Comparison]
    problems: tuple[InputProblem, ...]

    def worst_severity(self) -> str | None:
        found = {c.severity for comp in self.comparisons for c in comp.changes}
        return next((s for s in ("error", "warning", "info") if s in found), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "disclaimer": DISCLAIMER,
            "input_problems": [str(p) for p in self.problems],
            "comparisons": [c.to_dict() for c in self.comparisons],
        }


Loaded = tuple[dict[str, TraceView], dict[str, Any], tuple[InputProblem, ...]]


def _runs(path: Path, now: datetime, error_messages: bool) -> Loaded:
    telemetry = load([path], error_messages=error_messages)
    return assemble_events(telemetry.events, now, AssemblyConfig()), telemetry.resources, telemetry.problems


def select_run(traces: dict[str, TraceView], prefix: str | None, source: str) -> TraceView:
    candidates = [t for tid, t in traces.items() if prefix is None or tid.startswith(prefix)]
    if len(candidates) == 1:
        return candidates[0]
    if not traces:
        raise NoRunsError(f"{source}: no readable traces found")
    listing = "; ".join(
        f"{t.trace_id[:12]} {t.spans[t.roots[0]].name if t.roots else '(no root)'} "
        f"{t.started_at.isoformat() if t.started_at else ''}".strip()
        for t in sorted(traces.values(), key=lambda t: t.trace_id)
    )
    if not candidates:
        raise SelectionError(f"{source}: no trace id starts with {prefix!r}; runs in the file: {listing}")
    raise SelectionError(f"{source} holds {len(candidates)} runs; choose one with a trace id prefix: {listing}")


def build_comparisons(
    baseline_path: Path,
    current_paths: list[Path],
    now: datetime,
    config: CompareConfig | None = None,
    baseline_trace: str | None = None,
    current_trace: str | None = None,
    error_messages: bool = True,
) -> ComparisonReport:
    """Compare the selected baseline run with the selected run of each current file."""
    traces, resources, problems = _runs(baseline_path, now, error_messages)
    base_trace = select_run(traces, baseline_trace, str(baseline_path))
    base = profile(base_trace, resources.get(base_trace.trace_id, {}), str(baseline_path))
    all_problems = list(problems)
    comparisons = []
    for path in current_paths:
        traces, resources, problems = _runs(path, now, error_messages)
        all_problems.extend(problems)
        cur_trace = select_run(traces, current_trace, str(path))
        cur = profile(cur_trace, resources.get(cur_trace.trace_id, {}), str(path))
        changes, notes = compare_runs(base, cur, config)
        if cur.trace_id == base.trace_id:
            notes.insert(0, "both sides are the same trace id: this compares a run with itself")
        comparisons.append(Comparison(base, cur, changes, notes))
    return ComparisonReport(comparisons, tuple(all_problems))


# --------------------------------------------------------------------------- text rendering


def render_comparison_text(report: ComparisonReport) -> str:
    lines: list[str] = []
    add = lines.append
    add(f"aobs regression report: {len(report.comparisons)} comparison(s)")
    add(f"note: {DISCLAIMER}")
    add("privacy: content is never shown; error messages are secret-scrubbed")
    if report.problems:
        add("")
        add(f"Input problems ({len(report.problems)}):")
        for problem in report.problems[:20]:
            add(f"  - {problem}")
    for comp in report.comparisons:
        b, c = comp.baseline.summary(), comp.current.summary()
        add("")
        add("=" * 100)
        add(f"Baseline  {b['source']}  trace {b['trace_id'][:12]}  run '{b['run']}'")
        add(f"Current   {c['source']}  trace {c['trace_id'][:12]}  run '{c['run']}'")
        add("")
        add(f"  {'':<22} {'baseline':>22} {'current':>22}")
        rows = [
            ("status", b["status"], c["status"]),
            ("duration", _ms(b["duration_ms"]), _ms(c["duration_ms"])),
            ("spans", b["span_count"], c["span_count"]),
            ("failed spans", b["error_count"], c["error_count"]),
            ("retry attempts", b["retries"], c["retries"]),
            ("tool calls", sum(b["tool_calls"].values()), sum(c["tool_calls"].values())),
            ("input tokens", _tok(b["tokens"]["input_tokens"]), _tok(c["tokens"]["input_tokens"])),
            ("output tokens", _tok(b["tokens"]["output_tokens"]), _tok(c["tokens"]["output_tokens"])),
            ("models", ", ".join(b["models"]) or "-", ", ".join(c["models"]) or "-"),
            ("cost (provider)", _cost(b["cost"]), _cost(c["cost"])),
        ]
        for name, old, new in rows:
            add(f"  {name:<22} {old!s:>22} {new!s:>22}")
        add("")
        counts = {s: sum(1 for x in comp.changes if x.severity == s) for s in ("error", "warning", "info")}
        add(f"Findings: {counts['error']} error(s), {counts['warning']} warning(s), {counts['info']} info")
        if not comp.changes:
            add("  No regressions or changes detected.")
        for number, change in enumerate(comp.changes, start=1):
            add("")
            add(f"  {number}. [{change.severity.upper()}] {change.title}   ({change.rule})")
            add(f"     Measured: baseline {change.baseline}  ->  current {change.current}   [{change.difference}]")
            add("     Evidence:")
            for item in change.evidence:
                add(f"       - {item}")
            add(f"     Why it matters: {change.why}")
            if change.possible_causes:
                add("     Possible explanations (not verified): " + "; ".join(change.possible_causes))
            add(f"     Next step: {change.next_step}")
        if comp.notes:
            add("")
            add("  Limits of this comparison:")
            for note in comp.notes:
                add(f"    - {note}")
    add("")
    return "\n".join(lines)


def _tok(t: dict[str, Any]) -> str:
    if t["value"] is None:
        return "unknown"
    return f"{t['value']:,}" if t["complete"] else f">= {t['value']:,} (partial)"


def _cost(cost: dict[str, Any] | None) -> str:
    return "not reported" if cost is None else f"{cost['amount']} {cost['currency']}"
