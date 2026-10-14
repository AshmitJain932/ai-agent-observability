"""Repeated-run regression analysis: is a group of current runs meaningfully different from a group
of baseline runs of the same task, beyond ordinary run-to-run variation?

Each run is loaded and profiled exactly like ``compare`` (strict parsing, schema validation, the
ingest privacy step with a metadata-only ceiling, trace assembly, ``compare.profile``). Per run,
a set of measurements is taken; each measurement is then compared between the groups:

* numbers (durations, tokens, cost): median and IQR per group, Mann-Whitney rank test,
  Hodges-Lehmann shift with a ~95% confidence interval;
* counts per run (retries, tool calls): the same rank test; practical size judged on the mean;
* proportions (failed runs, runs with repeated identical calls, an operation's failures): Fisher's
  exact test, Wilson intervals.

p-values are Holm-adjusted within two families: run-level metrics, and per-operation metrics
(exploratory). A verdict needs **both** statistical evidence and a practically relevant size; see
``Verdict``. Nothing is measured from runs that lack the data: unfinished runs, and runs with
incomplete usage or cost, are excluded from the affected metrics and counted as exclusions.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from ..model import assemble_events
from ..model.assembly import AssemblyConfig
from .compare import SUCCESS, NoRunsError, OpKey, RunProfile, profile
from .loader import InputProblem, Source, load
from .rules import RuleConfig, repeated_identical_calls
from .stats import (
    far_outliers,
    fisher_exact,
    hodges_lehmann,
    holm,
    mann_whitney,
    summarize,
    wilson,
)

Verdict = Literal[
    "likely_regression", "possible_improvement", "minor_change", "weak_signal", "no_change", "insufficient_data"
]
Kind = Literal["continuous", "count", "rate"]
VERDICT_LABEL = {
    "likely_regression": "LIKELY REGRESSION",
    "possible_improvement": "POSSIBLE IMPROVEMENT",
    "minor_change": "MINOR CHANGE",
    "weak_signal": "WEAK SIGNAL",
    "no_change": "no change",
    "insufficient_data": "INSUFFICIENT DATA",
}
VERDICT_ORDER = {v: i for i, v in enumerate(VERDICT_LABEL)}
RATE_MIN_PP = 0.10  # a proportion must change by at least 10 percentage points to matter
COUNT_MIN_PER_RUN = 0.5  # a per-run count must change by at least 0.5 on average to matter
DISCLAIMER = (
    "Medians, intervals and p-values describe these recordings only. A 'likely regression' needs both "
    "statistical evidence (Holm-adjusted p below alpha) and a change larger than the practical threshold; it "
    "says the groups differ, not why. Explanations are possibilities to check."
)
METHOD = (
    "numbers: Mann-Whitney U (exact permutation p-value up to 60 runs, else normal approximation), "
    "Hodges-Lehmann shift with ~95% CI; proportions: Fisher's exact test, Wilson 95% intervals; "
    "Holm-Bonferroni adjustment within the run-level and the per-operation families"
)


@dataclass(frozen=True)
class GroupConfig:
    min_runs: int = 5  # per group, for any statistical conclusion
    alpha: float = 0.05
    min_change_pct: float = 20.0
    min_run_ms: float = 50.0
    min_op_ms: float = 20.0


# --------------------------------------------------------------------------- loading


@dataclass
class Group:
    label: str
    sources: list[str]
    runs: list[RunProfile] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # runs left out by --task, with why
    problems: list[InputProblem] = field(default_factory=list)

    @property
    def finished(self) -> list[RunProfile]:
        return [r for r in self.runs if r.finished]

    def tasks(self) -> Counter[tuple[str | None, str | None]]:
        return Counter((r.run_name, r.agent) for r in self.runs)


def load_group(label: str, paths: Sequence[Source], now: datetime, task: str | None, error_messages: bool) -> Group:
    group = Group(label, [str(p) for p in paths])
    for path in paths:
        telemetry = load([path], error_messages=error_messages)
        group.problems.extend(telemetry.problems)
        traces = assemble_events(telemetry.events, now, AssemblyConfig())
        for trace in sorted(traces.values(), key=lambda t: (t.started_at is None, t.started_at, t.trace_id)):
            run = profile(trace, telemetry.resources.get(trace.trace_id, {}), str(path))
            if task is not None and run.run_name != task:
                group.skipped.append(f"{run.trace_id[:12]} (task '{run.run_name}')")
                continue
            group.runs.append(run)
    if not group.runs:
        raise NoRunsError(
            f"{label} group: no runs found in {', '.join(group.sources)}"
            + (f" for task '{task}'" if task is not None else "")
        )
    return group


# --------------------------------------------------------------------------- measurements


Extract = Callable[[RunProfile], "tuple[float | None, str | None]"]


@dataclass(frozen=True)
class Metric:
    key: str
    name: str
    family: Literal["run", "operation"]
    kind: Kind
    unit: str
    extract: Extract
    min_abs: float  # practical threshold in the metric's unit (continuous: median difference)
    why: str
    next_step: str


def _duration(run: RunProfile) -> tuple[float | None, str | None]:
    return (run.duration_ms, None) if run.finished and run.duration_ms is not None else (None, "unfinished run")


def _failed(run: RunProfile) -> tuple[float | None, str | None]:
    if not run.finished:
        return None, "unfinished run"
    return (0.0 if run.trace.status in SUCCESS else 1.0), None


def _retries(run: RunProfile) -> tuple[float | None, str | None]:
    return (None, "unfinished run") if not run.finished else (float(len(run.retries())), None)


def _tool_calls(run: RunProfile) -> tuple[float | None, str | None]:
    if not run.finished:
        return None, "unfinished run"
    return float(sum(op.calls for op in run.ops.values() if op.kind == "tool")), None


def _repeated(run: RunProfile) -> tuple[float | None, str | None]:
    if not run.finished:
        return None, "unfinished run"
    return (1.0 if repeated_identical_calls(run.trace, RuleConfig()) else 0.0), None


def _tokens(name: str) -> Extract:
    def extract(run: RunProfile) -> tuple[float | None, str | None]:
        if not run.finished:
            return None, "unfinished run"
        value, complete, _ = run.tokens(name)
        if value is None and run.trace.tokens[name].expected_spans == 0:
            return None, "no model calls"
        if not complete or value is None:
            return None, "usage not reported by every model call"
        return float(value), None

    return extract


def _cost(run: RunProfile) -> tuple[float | None, str | None]:
    if not run.finished:
        return None, "unfinished run"
    cost = run.cost()
    if cost.amount is None:
        return None, f"cost incomplete ({cost.reason})"
    return float(cost.amount), None


def run_metrics(cfg: GroupConfig) -> list[Metric]:
    return [
        Metric("run.duration", "run duration", "run", "continuous", "ms", _duration, cfg.min_run_ms,
               "Users wait longer; timeouts become more likely.",
               "Look at the operation metrics below to see which step got slower."),
        Metric("run.failed", "failed runs", "run", "rate", "runs", _failed, RATE_MIN_PP,
               "More runs of the same task end in failure.",
               "Run `aobs-server analyze` on a failed current run to see its root cause."),
        Metric("run.retries", "retry attempts per run", "run", "count", "attempts", _retries, COUNT_MIN_PER_RUN,
               "Retries hide failures from the final status but cost time and money.",
               "Check the error of the first attempt in the current runs."),
        Metric("run.tool_calls", "tool calls per run", "run", "count", "calls", _tool_calls, COUNT_MIN_PER_RUN,
               "Extra tool calls add latency and cost, and can be the start of a loop.",
               "See which operation's 'calls per run' changed below."),
        Metric("run.repeated", "runs with repeated identical calls", "run", "rate", "runs", _repeated, RATE_MIN_PP,
               "Identical calls repeated in a run are usually wasted work.",
               "Run `aobs-server analyze` on an affected run (rule waste.repeated_call)."),
        Metric("run.input_tokens", "input tokens per run", "run", "continuous", "tokens", _tokens("input_tokens"), 1,
               "More tokens usually means higher cost and latency.",
               "Compare prompt sizes per model call between the groups."),
        Metric("run.output_tokens", "output tokens per run", "run", "continuous", "tokens", _tokens("output_tokens"),
               1, "More output tokens usually means higher cost and latency.",
               "Check whether responses got longer or more model calls are made."),
        Metric("run.cost", "provider-reported cost per run", "run", "continuous", "", _cost, 0.0,
               "Per-run cost multiplies across every run in production.",
               "See the token and model changes in this report."),
    ]  # fmt: skip


def _op_duration(key: OpKey) -> Extract:
    def extract(run: RunProfile) -> tuple[float | None, str | None]:
        if not run.finished:
            return None, "unfinished run"
        op = run.ops.get(key)
        if op is None or op.median_ms is None:
            return None, "operation not called"
        return op.median_ms, None

    return extract


def _op_calls(key: OpKey) -> Extract:
    def extract(run: RunProfile) -> tuple[float | None, str | None]:
        if not run.finished:
            return None, "unfinished run"
        op = run.ops.get(key)
        return float(op.calls if op else 0), None  # a finished run that never called it made 0 calls

    return extract


def _op_failed(key: OpKey) -> Extract:
    def extract(run: RunProfile) -> tuple[float | None, str | None]:
        if not run.finished:
            return None, "unfinished run"
        op = run.ops.get(key)
        if op is None:
            return None, "operation not called"
        return (1.0 if op.failed else 0.0), None

    return extract


def operation_metrics(base: Group, cur: Group, cfg: GroupConfig) -> list[Metric]:
    keys_b = {k for r in base.finished for k in r.ops}
    keys_c = {k for r in cur.finished for k in r.ops}
    metrics = []
    for key in sorted(keys_b & keys_c):
        label = f"{key[0]} '{key[1]}'"
        metrics += [
            Metric(f"op.{key[0]}.{key[1]}.duration", f"{label} duration per call", "operation", "continuous", "ms",
                   _op_duration(key), cfg.min_op_ms,
                   "This step contributes more time to every run that uses it.",
                   "Check the dependency behind this step and its inputs in the slower runs."),
            Metric(f"op.{key[0]}.{key[1]}.calls", f"{label} calls per run", "operation", "count", "calls",
                   _op_calls(key), COUNT_MIN_PER_RUN,
                   "More calls of the same step per run add latency and cost.",
                   "Check whether the extra calls are retries or repeated requests."),
            Metric(f"op.{key[0]}.{key[1]}.failed", f"{label} failing in a run", "operation", "rate", "runs",
                   _op_failed(key), RATE_MIN_PP,
                   "This step fails in more of the runs that use it.",
                   "Inspect a failing current run with `aobs-server analyze`."),
        ]  # fmt: skip
    return metrics


# --------------------------------------------------------------------------- evaluation


@dataclass(frozen=True)
class Side:
    n: int
    excluded: dict[str, int]  # reason -> number of runs
    values: list[tuple[float, str]]  # (value, trace id)

    def numbers(self) -> list[float]:
        return [v for v, _ in self.values]


@dataclass
class MetricResult:
    metric: Metric
    baseline: Side
    current: Side
    verdict: Verdict = "insufficient_data"
    p_value: float | None = None
    p_adjusted: float | None = None
    family_size: int = 0
    exact: bool | None = None
    shift: tuple[float, float | None, float | None] | None = None  # HL estimate, CI low, CI high
    abs_change: float | None = None
    rel_change: float | None = None  # percent; None when the baseline value is 0
    notes: list[str] = field(default_factory=list)
    currency: str | None = None

    @property
    def tested(self) -> bool:
        return self.p_value is not None

    def unit(self) -> str:
        return self.currency or self.metric.unit

    def fmt(self, value: float | None, signed: bool = False) -> str:
        if value is None:
            return "unknown"
        sign = "+" if signed and value > 0 else ""
        unit = self.unit()
        if self.metric.kind == "rate":
            return f"{sign}{value * 100:.0f} pp" if signed else f"{value * 100:.0f}%"
        if self.currency:
            return f"{sign}{value:.6f}".rstrip("0").rstrip(".") + f" {unit}"
        if unit == "tokens":
            return f"{sign}{value:,.0f} tokens"
        if unit == "ms":
            return f"{sign}{value:,.1f} ms"
        return f"{sign}{value:,.1f} {unit}".rstrip()

    def describe(self, side: Side) -> str:
        if side.n == 0:
            return "no usable runs"
        numbers = side.numbers()
        if self.metric.kind == "rate":
            hits = int(sum(numbers))
            low, high = wilson(hits, side.n)
            return f"{hits}/{side.n} ({hits / side.n:.0%}, 95% CI {low:.0%}-{high:.0%})"
        s = summarize(numbers)
        text = f"median {self.fmt(s.median)} (IQR {self.fmt(s.q1)} to {self.fmt(s.q3)}, n={side.n})"
        if self.metric.kind == "count":
            text += f", mean {s.mean:.2f}"
        return text

    def to_dict(self) -> dict[str, Any]:
        def side_dict(side: Side) -> dict[str, Any]:
            out: dict[str, Any] = {"n": side.n, "excluded": side.excluded, "description": self.describe(side)}
            if side.n:
                numbers = side.numbers()
                if self.metric.kind == "rate":
                    out["proportion"] = sum(numbers) / side.n
                else:
                    s = summarize(numbers)
                    out.update(median=s.median, q1=s.q1, q3=s.q3, min=s.minimum, max=s.maximum, mean=s.mean)
            return out

        return {
            "key": self.metric.key,
            "metric": self.metric.name,
            "family": self.metric.family,
            "kind": self.metric.kind,
            "unit": self.unit(),
            "baseline": side_dict(self.baseline),
            "current": side_dict(self.current),
            "absolute_change": self.abs_change,
            "relative_change_pct": self.rel_change,
            "shift_estimate": None if self.shift is None else {"estimate": self.shift[0], "ci95": list(self.shift[1:])},
            "test": None if not self.tested else ("fisher_exact" if self.metric.kind == "rate" else "mann_whitney"),
            "exact": self.exact,
            "p_value": self.p_value,
            "p_adjusted": self.p_adjusted,
            "family_size": self.family_size,
            "verdict": self.verdict,
            "evidence": self.evidence(),
            "why_it_matters": self.metric.why,
            "next_step": self.metric.next_step,
            "notes": self.notes,
        }

    def evidence(self) -> list[str]:
        out = []
        for label, side in (("baseline", self.baseline), ("current", self.current)):
            if not side.n:
                continue
            numbers = side.numbers()
            if self.metric.kind == "rate":
                hits = [t[:12] for v, t in side.values if v]
                if hits:
                    out.append(f"{label} runs counted: " + ", ".join(hits[:8]) + (" ..." if len(hits) > 8 else ""))
                continue
            for index in far_outliers(numbers):
                value, trace = side.values[index]
                out.append(f"{label} outlier: run {trace[:12]} at {self.fmt(value)} (beyond 3 IQR from the quartiles)")
            if label == "current" and self.verdict in ("likely_regression", "weak_signal"):
                worst = sorted(side.values, reverse=True)[:3]
                out.append("highest current runs: " + ", ".join(f"{t[:12]} {self.fmt(v)}" for v, t in worst))
        for label, side in (("baseline", self.baseline), ("current", self.current)):
            for reason, count in sorted(side.excluded.items()):
                out.append(f"{label}: {count} run(s) excluded ({reason})")
        return out


def _side(metric: Metric, runs: list[RunProfile]) -> Side:
    values: list[tuple[float, str]] = []
    excluded: Counter[str] = Counter()
    for run in runs:
        value, reason = metric.extract(run)
        if value is None:
            excluded[reason or "no value"] += 1
        else:
            values.append((value, run.trace_id))
    return Side(len(values), dict(excluded), values)


def _evaluate(result: MetricResult, cfg: GroupConfig) -> None:
    b, c, metric = result.baseline, result.current, result.metric
    if b.n == 0 or c.n == 0:
        result.notes.append("no usable runs in at least one group")
        return
    xb, xc = b.numbers(), c.numbers()
    if metric.kind == "rate":
        pb, pc = sum(xb) / b.n, sum(xc) / c.n
        result.abs_change, result.rel_change = pc - pb, None if pb == 0 else (pc - pb) / pb * 100
    elif metric.kind == "count":
        mb, mc = sum(xb) / b.n, sum(xc) / c.n
        result.abs_change, result.rel_change = mc - mb, None if mb == 0 else (mc - mb) / mb * 100
    else:
        mb, mc = summarize(xb).median, summarize(xc).median
        result.abs_change, result.rel_change = mc - mb, None if mb == 0 else (mc - mb) / mb * 100
    if metric.kind != "rate":
        shift = hodges_lehmann(xb, xc)
        result.shift = (shift.estimate, shift.low, shift.high)
    if b.n < cfg.min_runs or c.n < cfg.min_runs:
        result.notes.append(f"fewer than {cfg.min_runs} usable runs in a group: descriptive only, no test")
        if result.shift is not None:
            result.shift = (result.shift[0], None, None)  # no interval without enough runs
        return
    if metric.kind == "rate":
        result.p_value = fisher_exact(int(sum(xb)), b.n, int(sum(xc)), c.n)
        result.exact = True
    else:
        test = mann_whitney(xb, xc)
        result.p_value, result.exact = test.p_value, test.exact


def _practical(result: MetricResult, cfg: GroupConfig) -> bool:
    change = result.abs_change
    if change is None:
        return False
    if abs(change) < result.metric.min_abs or change == 0:
        return False
    if result.metric.kind == "rate":
        return True  # the absolute threshold (percentage points) is the practical criterion
    return result.rel_change is None or abs(result.rel_change) >= cfg.min_change_pct


def _verdict(result: MetricResult, cfg: GroupConfig) -> Verdict:
    if not result.tested or result.p_adjusted is None:
        return "insufficient_data"
    significant = result.p_adjusted < cfg.alpha
    practical = _practical(result, cfg)
    worse = (result.abs_change or 0) > 0
    if significant and practical:
        return "likely_regression" if worse else "possible_improvement"
    if significant:
        return "minor_change"
    if practical:
        return "weak_signal"
    return "no_change"


# --------------------------------------------------------------------------- report


@dataclass
class GroupReport:
    baseline: Group
    current: Group
    config: GroupConfig
    results: list[MetricResult]
    warnings: list[str]
    changes: list[str]
    notes: list[str]
    outliers: list[str] = field(default_factory=list)

    def severity(self) -> str | None:
        severities = set()
        for r in self.results:
            if r.verdict == "likely_regression":
                severities.add("error")
            elif r.verdict == "weak_signal" and (r.abs_change or 0) > 0:
                severities.add("warning")
        if self.warnings:
            severities.add("warning")
        if self.changes:
            severities.add("info")
        return next((s for s in ("error", "warning", "info") if s in severities), None)

    def to_dict(self) -> dict[str, Any]:
        def group(g: Group) -> dict[str, Any]:
            return {
                "sources": g.sources,
                "runs": [
                    {"trace_id": r.trace_id, "run": r.run_name, "status": r.trace.status, "duration_ms": r.duration_ms}
                    for r in g.runs
                ],
                "finished_runs": len(g.finished),
                "skipped": g.skipped,
            }

        return {
            "disclaimer": DISCLAIMER,
            "method": METHOD,
            "config": {
                "min_runs": self.config.min_runs,
                "alpha": self.config.alpha,
                "min_change_pct": self.config.min_change_pct,
                "min_run_ms": self.config.min_run_ms,
                "min_op_ms": self.config.min_op_ms,
                "rate_min_pp": RATE_MIN_PP * 100,
                "count_min_per_run": COUNT_MIN_PER_RUN,
            },
            "baseline": group(self.baseline),
            "current": group(self.current),
            "warnings": self.warnings,
            "metrics": [r.to_dict() for r in self.results],
            "changes": self.changes,
            "outliers": self.outliers,
            "notes": self.notes,
            "input_problems": [str(p) for p in [*self.baseline.problems, *self.current.problems]],
        }


def _comparability(base: Group, cur: Group, cfg: GroupConfig) -> list[str]:
    warnings = []
    for g in (base, cur):
        if len(g.tasks()) > 1:
            tasks = ", ".join(f"'{t}' ({a}) x{n}" for (t, a), n in sorted(g.tasks().items(), key=str))
            warnings.append(f"the {g.label} group mixes different tasks: {tasks}; use --task to pick one")
    if set(base.tasks()) != set(cur.tasks()):
        warnings.append(
            "the groups contain different tasks (run name / agent): "
            f"baseline {sorted(map(str, base.tasks()))}, current {sorted(map(str, cur.tasks()))}; "
            "differences may reflect the task, not a regression"
        )
    for attr, what in (("environment", "environment"), ("service_name", "service"), ("sdk", "SDK version")):
        vb = {r.resource.get(attr) for r in base.runs}
        vc = {r.resource.get(attr) for r in cur.runs}
        if vb != vc:
            warnings.append(
                f"{what} differs between the groups (baseline {sorted(map(str, vb))}, current {sorted(map(str, vc))}): "
                "timings may not be comparable"
            )
    for g in (base, cur):
        unfinished = len(g.runs) - len(g.finished)
        if unfinished:
            warnings.append(f"{unfinished} {g.label} run(s) never finished and are excluded from every metric")
        if len(g.finished) < cfg.min_runs:
            warnings.append(
                f"the {g.label} group has {len(g.finished)} finished run(s); at least {cfg.min_runs} are needed "
                "for any statistical conclusion"
            )
    return warnings


def _outliers(results: list[MetricResult]) -> list[str]:
    """Far-out values in the run-level numeric metrics, whatever the verdict (data quality)."""
    out = []
    for r in results:
        if r.metric.family != "run" or r.metric.kind == "rate":
            continue
        for label, side in (("baseline", r.baseline), ("current", r.current)):
            numbers = side.numbers()
            for index in far_outliers(numbers):
                value, trace = side.values[index]
                out.append(
                    f"{r.metric.name}: {label} run {trace[:12]} at {r.fmt(value)} vs {label} median "
                    f"{r.fmt(summarize(numbers).median)}; medians and rank tests limit its influence"
                )
    return out


def _counts(counter: Counter[str]) -> str:
    return ", ".join(f"{name} x{n}" for name, n in sorted(counter.items()))


def _changes(base: Group, cur: Group) -> list[str]:
    out = []
    keys_b = Counter(k for r in base.finished for k in r.ops)
    keys_c = Counter(k for r in cur.finished for k in r.ops)
    nb, nc = len(base.finished), len(cur.finished)
    for key in sorted(set(keys_c) - set(keys_b)):
        out.append(f"operation added: {key[0]} '{key[1]}' appears in {keys_c[key]} of {nc} current run(s), "
                   f"in none of {nb} baseline run(s)")  # fmt: skip
    for key in sorted(set(keys_b) - set(keys_c)):
        out.append(
            f"operation removed: {key[0]} '{key[1]}' appears in {keys_b[key]} of {nb} baseline run(s), in none of "
            f"{nc} current run(s) (a rename shows as one removed and one added operation)"
        )
    models_b: dict[OpKey, Counter[str]] = defaultdict(Counter)
    models_c: dict[OpKey, Counter[str]] = defaultdict(Counter)
    for runs, models in ((base.finished, models_b), (cur.finished, models_c)):
        for run in runs:
            for key, op in run.ops.items():
                models[key].update(op.models)
    for key in sorted(set(models_b) & set(models_c)):
        if set(models_b[key]) != set(models_c[key]):
            out.append(
                f"model changed for {key[0]} '{key[1]}': baseline {_counts(models_b[key])} -> "
                f"current {_counts(models_c[key])}"
            )
    errors_b: Counter[str] = Counter()
    errors_c: Counter[str] = Counter()
    for runs, errors in ((base.finished, errors_b), (cur.finished, errors_c)):
        for run in runs:
            errors.update({f"{op.label} {t}" for op in run.ops.values() for t in op.errors})
    for error in sorted(set(errors_c) - set(errors_b)):
        out.append(f"new error: {error} in {errors_c[error]} of {nc} current run(s), never in the baseline")
    return out


def analyze_groups(base: Group, cur: Group, config: GroupConfig | None = None) -> GroupReport:
    cfg = config or GroupConfig()
    results: list[MetricResult] = []
    notes: list[str] = []
    currencies = {cost.currency for r in [*base.finished, *cur.finished] if (cost := r.cost()).amount is not None}
    for metric in [*run_metrics(cfg), *operation_metrics(base, cur, cfg)]:
        result = MetricResult(metric, _side(metric, base.finished), _side(metric, cur.finished))
        if metric.key == "run.cost":
            if len(currencies) > 1:
                notes.append(f"cost not compared: several currencies ({', '.join(sorted(map(str, currencies)))})")
                continue
            result.currency = next(iter(currencies), None)
            if result.currency is None:
                if any(r.trace.kind_counts.get("llm") for r in [*base.finished, *cur.finished]):
                    notes.append("cost not compared: no run has provider-reported cost on every model call "
                                 "(aobs never estimates prices)")  # fmt: skip
                continue
        no_calls = all("no model calls" in side.excluded for side in (result.baseline, result.current))
        if "token" in metric.key and result.baseline.n == result.current.n == 0 and no_calls:
            continue  # neither group made model calls: nothing to compare
        _evaluate(result, cfg)
        results.append(result)
    for family in ("run", "operation"):
        tested = [r for r in results if r.metric.family == family and r.tested]
        for r, adjusted in zip(tested, holm([r.p_value for r in tested if r.p_value is not None]), strict=True):
            r.p_adjusted = adjusted
            r.family_size = len(tested)
    for r in results:
        r.verdict = _verdict(r, cfg)
    notes += [
        "operations are matched by kind and tool/span name across runs; span ids are never compared",
        "recordings are assumed independent and recorded under comparable conditions",
        f"practical thresholds: {cfg.min_change_pct:g}% and {cfg.min_run_ms:g} ms for the run, {cfg.min_op_ms:g} ms "
        f"per operation call, {RATE_MIN_PP * 100:.0f} percentage points for proportions, "
        f"{COUNT_MIN_PER_RUN} per run for counts",
    ]
    return GroupReport(
        base, cur, cfg, results, _comparability(base, cur, cfg), _changes(base, cur), notes, _outliers(results)
    )


def build_group_report(
    baseline_paths: Sequence[Source],
    current_paths: Sequence[Source],
    now: datetime,
    config: GroupConfig | None = None,
    task: str | None = None,
    error_messages: bool = True,
) -> GroupReport:
    base = load_group("baseline", baseline_paths, now, task, error_messages)
    cur = load_group("current", current_paths, now, task, error_messages)
    return analyze_groups(base, cur, config)


# --------------------------------------------------------------------------- text


def _center(r: MetricResult, side: Side) -> str:
    if not side.n:
        return "-"
    if r.metric.kind == "rate":
        return f"{sum(side.numbers()) / side.n:.0%}"
    return r.fmt(summarize(side.numbers()).median)


def _p(value: float | None) -> str:
    if value is None:
        return "-"
    return "<0.0001" if value < 0.0001 else f"{value:.4f}"


def render_group_text(report: GroupReport) -> str:
    lines: list[str] = []
    add = lines.append
    b, c = report.baseline, report.current
    add("aobs repeated-run report")
    add(f"note: {DISCLAIMER}")
    add(f"method: {METHOD}")
    add("privacy: content is never shown; error messages are secret-scrubbed")
    for g in (b, c):
        tasks = ", ".join(f"'{t}'" for t, _ in sorted(g.tasks(), key=str))
        add(f"{g.label:<9} {len(g.runs)} run(s), {len(g.finished)} finished, task {tasks}, from {', '.join(g.sources)}")
        if g.skipped:
            add(f"{'':<9} skipped by --task: {len(g.skipped)} run(s)")
    problems = [*b.problems, *c.problems]
    if problems:
        add("")
        add(f"Input problems ({len(problems)}):")
        for problem in problems[:20]:
            add(f"  - {problem}")
    if report.warnings:
        add("")
        add("Warnings:")
        for w in report.warnings:
            add(f"  - {w}")

    for family, title in (("run", "Run-level metrics"), ("operation", "Per-operation metrics (exploratory)")):
        rows = [r for r in report.results if r.metric.family == family]
        if not rows:
            continue
        tested = sum(r.tested for r in rows)
        add("")
        add(f"{title}: {tested} tested, p-values Holm-adjusted over {tested}")
        add(f"  {'metric':<44} {'n b/c':>7}  {'baseline':>16} {'current':>16} {'change':>20}  {'p adj':>8}  verdict")
        for r in rows:
            change = "-" if r.abs_change is None else r.fmt(r.abs_change, signed=True)
            if r.rel_change is not None and r.metric.kind != "rate":
                change += f" ({r.rel_change:+.0f}%)"
            counts = f"{r.baseline.n}/{r.current.n}"
            add(
                f"  {r.metric.name[:44]:<44} {counts:>7}  {_center(r, r.baseline):>16} {_center(r, r.current):>16} "
                f"{change:>20}  {_p(r.p_adjusted):>8}  {VERDICT_LABEL[r.verdict]}"
            )

    detailed = sorted(
        (
            r
            for r in report.results
            if r.verdict != "no_change"
            # untested metrics are detailed when their descriptive change would matter or runs were excluded
            and (
                r.verdict != "insufficient_data"
                or _practical(r, report.config)
                or bool(r.baseline.excluded or r.current.excluded)
            )
        ),
        key=lambda r: (VERDICT_ORDER[r.verdict], r.metric.family != "run", r.metric.name),
    )
    add("")
    add(f"Findings ({len(detailed)})")
    if not detailed:
        add("  No meaningful change detected in any measured metric.")
    for number, r in enumerate(detailed, start=1):
        add("")
        add(f"  {number}. [{VERDICT_LABEL[r.verdict]}] {r.metric.name} ({r.metric.family} metric)")
        add(f"     baseline: {r.describe(r.baseline)}")
        add(f"     current:  {r.describe(r.current)}")
        what = "proportion" if r.metric.kind == "rate" else "mean per run" if r.metric.kind == "count" else "median"
        change = f"{r.fmt(r.abs_change, signed=True)}" + (
            f" ({r.rel_change:+.0f}%)" if r.rel_change is not None else ""
        )
        line = f"     change:   {change} in the {what}"
        if r.shift is not None:
            est, low, high = r.shift
            interval = (f", 95% CI {r.fmt(low, signed=True)} to {r.fmt(high, signed=True)}" if low is not None
                        else ", no confidence interval (too few runs)")  # fmt: skip
            line += f"; shift estimate {r.fmt(est, signed=True)}{interval}"
        add(line)
        if r.tested:
            test = (
                "Fisher's exact test"
                if r.metric.kind == "rate"
                else ("Mann-Whitney U, exact" if r.exact else "Mann-Whitney U, normal approximation")
            )
            add(f"     test:     {test}: p={_p(r.p_value)}, Holm-adjusted p={_p(r.p_adjusted)} over {r.family_size} "
                f"{r.metric.family}-level test(s), alpha={report.config.alpha:g}")  # fmt: skip
        add(f"     meaning:  {_meaning(r)}")
        for item in r.evidence():
            add(f"     evidence: {item}")
        for note in r.notes:
            add(f"     note:     {note}")
        if r.verdict in ("likely_regression", "weak_signal", "insufficient_data"):
            add(f"     why it matters: {r.metric.why}")
            more = f"record more runs (at least {report.config.min_runs} usable per group) and compare again"
            if r.baseline.excluded or r.current.excluded:
                more = "close the data gap listed under evidence (excluded runs) or " + more
            add(f"     next step: {r.metric.next_step if r.verdict != 'insufficient_data' else more}")
    if report.outliers:
        add("")
        add("Data quality: outliers (beyond 3 IQR from the quartiles)")
        for outlier in report.outliers:
            add(f"  - {outlier}")
    if report.changes:
        add("")
        add("Other changes (not tested)")
        for change_line in report.changes:
            add(f"  - {change_line}")
    add("")
    add("Limits of this analysis:")
    for note in report.notes:
        add(f"  - {note}")
    add("")
    return "\n".join(lines)


def _meaning(r: MetricResult) -> str:
    return {
        "likely_regression": "the current group is worse, both statistically and by more than the practical threshold",
        "possible_improvement": "the current group is better, statistically and by more than the practical threshold",
        "minor_change": "the groups differ statistically, but by less than the practical threshold",
        "weak_signal": "the change is large enough to matter but the evidence is not conclusive "
        "(could be run-to-run variation); record more runs",
        "no_change": "no meaningful difference",
        "insufficient_data": "too few usable runs for a statistical conclusion; the numbers above are descriptive only",
    }[r.verdict]
