"""Deterministic diagnosis rules over an assembled trace (``aobs_server.model.TraceView``).

Each rule is a plain function ``(trace, config) -> list[Finding]``. Findings cite the spans that
prove them and the facts read from those spans; nothing is inferred from content (the loader
never passes content through). The same functions can later run on traces read from storage.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from ..model.assembly import SpanNode, TraceView

Severity = Literal["error", "warning", "info"]
SEVERITY_ORDER: dict[str, int] = {"error": 0, "warning": 1, "info": 2}


@dataclass(frozen=True)
class RuleConfig:
    slow_share: float = 0.5  # a span taking at least this share of the run ...
    slow_min_ms: float = 100.0  # ... and at least this long is reported as the bottleneck


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: Severity
    title: str
    span_ids: tuple[str, ...]
    evidence: tuple[str, ...]
    suggestion: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "title": self.title,
            "span_ids": list(self.span_ids),
            "evidence": list(self.evidence),
            "suggestion": self.suggestion,
        }


# --------------------------------------------------------------------------- span helpers


def fields(node: SpanNode) -> Mapping[str, Any]:
    return node.record.merged


def label(node: SpanNode) -> str:
    """``tool issue_refund``, ``llm chat (gpt-x)``, ``agent answer_ticket``."""
    f = fields(node)
    name = f.get("tool", {}).get("name") or node.name
    model = f.get("llm", {}).get("response_model") or f.get("llm", {}).get("request_model")
    return f"{node.kind} {name}" + (f" ({model})" if model else "")


def ms(value: float | None) -> str:
    return "unknown" if value is None else f"{value:,.1f} ms"


def short(span_id: str) -> str:
    return span_id[:8]


def ancestors(trace: TraceView, node: SpanNode) -> list[SpanNode]:
    """Parents from the nearest up to the root (stops at orphans and unknown parents)."""
    chain: list[SpanNode] = []
    seen = {node.span_id}
    current = node
    while current.parent_span_id and current.orphan_reason is None:
        parent = trace.spans.get(current.parent_span_id)
        if parent is None or parent.span_id in seen:
            break
        chain.append(parent)
        seen.add(parent.span_id)
        current = parent
    return chain


def descendants(trace: TraceView, node: SpanNode) -> list[SpanNode]:
    out: list[SpanNode] = []
    stack = list(node.children)
    seen: set[str] = set()
    while stack:
        span_id = stack.pop()
        if span_id in seen or span_id not in trace.spans:
            continue
        seen.add(span_id)
        child = trace.spans[span_id]
        out.append(child)
        stack.extend(child.children)
    return out


def _ordered(trace: TraceView, nodes: list[SpanNode]) -> list[SpanNode]:
    return sorted(nodes, key=lambda n: (n.start_time, n.span_id))


def _error_key(node: SpanNode) -> tuple[str, str, str]:
    f = fields(node)
    return (node.kind, f.get("tool", {}).get("name") or node.name, f.get("error", {}).get("type", "unknown error"))


def _error_facts(node: SpanNode) -> list[str]:
    error = fields(node).get("error", {})
    facts = [f"error type {error.get('type', 'not reported')}"]
    for key in ("origin", "code", "retryable"):
        if key in error:
            facts.append(f"{key}={str(error[key]).lower() if isinstance(error[key], bool) else error[key]}")
    return facts


def _error_suggestion(node: SpanNode) -> str:
    error = fields(node).get("error", {})
    kind, origin, code = node.kind, error.get("origin"), str(error.get("code", ""))
    error_type = str(error.get("type", ""))
    if origin == "timeout" or error_type.endswith("TimeoutError") or "Timeout" in error_type:
        return (
            "The call timed out. Check the downstream service's health and latency at that time, make the "
            "timeout explicit, and handle it (retry with backoff, or degrade gracefully) instead of failing the run."
        )
    if code == "429":
        return (
            "The provider rate-limited the request. Back off (honour Retry-After), reduce concurrency, or raise limits."
        )
    if code in ("401", "403"):
        return "The provider refused the credentials. Check the API key, its permissions and the organisation/project."
    if code.startswith("5"):
        return "The provider failed server-side. Retry with backoff and consider a fallback model or provider."
    if code.startswith("4"):
        return "The provider rejected the request. Check the model name and request parameters for this call."
    if "Connection" in error_type:
        return "The connection failed. Verify the endpoint, DNS/network and credentials; retry with backoff."
    if kind == "tool" or origin == "tool":
        return (
            "The tool raised an exception. Reproduce it with the same arguments (the input fingerprint identifies "
            "identical calls), and validate model-generated arguments before calling the tool."
        )
    if kind == "llm" or origin == "provider":
        return "The model call failed. Check the provider status and the request parameters for this call."
    return (
        "Exception in application code. Enable capture_stack_traces=True in the SDK (and allow stack traces in "
        "the project) to record where it was raised."
    )


# --------------------------------------------------------------------------- rules


def root_cause_errors(trace: TraceView, config: RuleConfig) -> list[Finding]:
    """Error spans with no failing descendant are where things broke; group identical ones."""
    groups: dict[tuple[str, str, str], list[SpanNode]] = defaultdict(list)
    for node in trace.spans.values():
        if node.status == "error" and not any(d.status == "error" for d in descendants(trace, node)):
            groups[_error_key(node)].append(node)
    findings = []
    for (kind, name, error_type), nodes in groups.items():
        nodes = _ordered(trace, nodes)
        first = nodes[0]
        evidence = [
            f"span {short(n.span_id)} ({label(n)}) ended with status error after {ms(n.duration_ms)}" for n in nodes
        ]
        evidence.append("; ".join(_error_facts(first)))
        message = fields(first).get("error", {}).get("message")
        if message:
            evidence.append(f"message (secret-scrubbed): {message[:300]}")
        failed_parents = [a for a in ancestors(trace, first) if a.status == "error"]
        suggestion = _error_suggestion(first)
        same = [a for a in failed_parents if fields(a).get("error", {}).get("type") == error_type]
        if failed_parents:
            evidence.append(
                "parents that also failed: "
                + " > ".join(
                    f"{label(a)} ({fields(a).get('error', {}).get('type', 'no error type')})" for a in failed_parents
                )
            )
            run = failed_parents[-1]
            if run.parent_span_id is None and run in same:
                suggestion += (
                    f" The same {error_type} reached the run '{run.name}' unhandled and failed it; catch it in the "
                    "agent loop and let the agent recover or return a partial answer."
                )
            elif run.parent_span_id is None:
                suggestion += (
                    f" The run '{run.name}' then failed with "
                    f"{fields(run).get('error', {}).get('type', 'an error')}: it gave up after this failure."
                )
        times = f" ({len(nodes)} times)" if len(nodes) > 1 else ""
        findings.append(
            Finding(
                rule="error.root_cause",
                severity="error",
                title=f"{kind} '{name}' failed with {error_type}{times}",
                span_ids=tuple(n.span_id for n in nodes),
                evidence=tuple(evidence),
                suggestion=suggestion,
            )
        )
    return findings


def repeated_failures(trace: TraceView, config: RuleConfig) -> list[Finding]:
    """The same operation failed with the same error more than once in one run."""
    groups: dict[tuple[str, str, str], list[SpanNode]] = defaultdict(list)
    for node in trace.spans.values():
        if node.status == "error" and node.kind != "agent":
            groups[_error_key(node)].append(node)
    findings = []
    for (kind, name, error_type), nodes in groups.items():
        if len(nodes) < 2:
            continue
        nodes = _ordered(trace, nodes)
        retries = [fields(n).get("retry") for n in nodes]
        fingerprints = {fields(n).get("input_fingerprint") for n in nodes}
        evidence = [f"{len(nodes)} failures: " + ", ".join(short(n.span_id) for n in nodes)]
        if any(retries):
            attempts = [str(r["attempt"]) for r in retries if r]
            maximum = next((r.get("max_attempts") for r in retries if r and r.get("max_attempts")), None)
            evidence.append(f"retry attempts {', '.join(attempts)}" + (f" of {maximum}" if maximum else ""))
        if len(fingerprints) == 1 and None not in fingerprints:
            evidence.append("every attempt had the same input fingerprint (identical input)")
        total = sum(n.duration_ms or 0.0 for n in nodes)
        evidence.append(f"time spent in failed attempts: {ms(total)}")
        retryable = {fields(n).get("error", {}).get("retryable") for n in nodes}
        if retryable == {False}:
            suggestion = "The error is marked non-retryable, so repeating the call cannot succeed: stop retrying it."
        elif any(retries):
            suggestion = (
                "Retries did not help. Add exponential backoff with jitter, cap the attempts, and add a fallback or "
                "circuit breaker so a dependency outage does not consume the whole run."
            )
        else:
            suggestion = (
                "The agent called the same operation again after it failed. Feed the error back to the model "
                "explicitly, or stop after the first failure for identical input, to avoid a failure loop."
            )
        findings.append(
            Finding(
                rule="error.repeated",
                severity="error",
                title=f"{kind} '{name}' failed {len(nodes)} times with {error_type}",
                span_ids=tuple(n.span_id for n in nodes),
                evidence=tuple(evidence),
                suggestion=suggestion,
            )
        )
    return findings


def repeated_identical_calls(trace: TraceView, config: RuleConfig) -> list[Finding]:
    """Successful calls with identical input (same keyed fingerprint) repeated within one run."""
    groups: dict[tuple[str, str, str], list[SpanNode]] = defaultdict(list)
    for node in trace.spans.values():
        fingerprint = fields(node).get("input_fingerprint")
        if fingerprint and node.status == "ok":
            groups[(node.kind, fields(node).get("tool", {}).get("name") or node.name, fingerprint)].append(node)
    findings = []
    for (kind, name, fingerprint), nodes in groups.items():
        if len(nodes) < 2:
            continue
        nodes = _ordered(trace, nodes)
        total = sum(n.duration_ms or 0.0 for n in nodes)
        findings.append(
            Finding(
                rule="waste.repeated_call",
                severity="warning",
                title=f"{kind} '{name}' was called {len(nodes)} times with identical input",
                span_ids=tuple(n.span_id for n in nodes),
                evidence=(
                    f"same input fingerprint {fingerprint.rsplit(':', 1)[-1][:12]}... on spans "
                    + ", ".join(short(n.span_id) for n in nodes),
                    f"all {len(nodes)} calls succeeded; {len(nodes) - 1} were redundant",
                    f"total time {ms(total)}",
                ),
                suggestion=(
                    "Reuse the first result within the run (cache by input), or check why the agent loop asks for "
                    "the same data again (e.g. the result is not added to the conversation)."
                ),
            )
        )
    return findings


def _run_duration(trace: TraceView) -> float | None:
    if len(trace.roots) == 1:
        return trace.spans[trace.roots[0]].duration_ms
    if trace.started_at and trace.ended_at:
        return (trace.ended_at - trace.started_at).total_seconds() * 1000
    return None


def slow_spans(trace: TraceView, config: RuleConfig) -> list[Finding]:
    """The deepest span that takes most of the run's time."""
    total = _run_duration(trace)
    if not total:
        return []
    roots = set(trace.roots)

    def is_slow(node: SpanNode) -> bool:
        d = node.duration_ms
        return (
            node.span_id not in roots and d is not None and d >= config.slow_min_ms and d / total >= config.slow_share
        )

    findings = []
    for node in _ordered(trace, [n for n in trace.spans.values() if is_slow(n)]):
        if any(is_slow(trace.spans[c]) for c in node.children if c in trace.spans):
            continue  # a child explains it better
        assert node.duration_ms is not None
        evidence = [
            f"span {short(node.span_id)} ({label(node)}) took {ms(node.duration_ms)}",
            f"{node.duration_ms / total:.0%} of the run's {ms(total)}",
        ]
        streaming = fields(node).get("llm", {}).get("streaming", {})
        if "time_to_first_token_ms" in streaming:
            evidence.append(f"time to first token {ms(streaming['time_to_first_token_ms'])}")
        if node.kind == "llm":
            suggestion = (
                "The model call dominates. Consider fewer input tokens, a faster model for this step, streaming, "
                "or caching repeated prompts."
            )
        else:
            suggestion = (
                "This call dominates the run. Profile the dependency, cache its result if inputs repeat, and run "
                "independent calls in parallel instead of in sequence."
            )
        findings.append(
            Finding(
                rule="latency.bottleneck",
                severity="warning",
                title=f"{label(node)} took {node.duration_ms / total:.0%} of the run",
                span_ids=(node.span_id,),
                evidence=tuple(evidence),
                suggestion=suggestion,
            )
        )
    return findings


def unfinished_spans(trace: TraceView, config: RuleConfig) -> list[Finding]:
    open_nodes = _ordered(trace, [n for n in trace.spans.values() if n.lifecycle != "complete"])
    if not open_nodes:
        return []
    roots_open = [n for n in open_nodes if n.parent_span_id is None]
    evidence = [
        f"span {short(n.span_id)} ({label(n)}) started {n.start_time.isoformat()} and has no end ({n.lifecycle})"
        for n in open_nodes
    ]
    if roots_open:
        evidence.append("the run itself never finished, so its outcome is unknown")
    return [
        Finding(
            rule="trace.unfinished",
            severity="warning",
            title=f"{len(open_nodes)} span(s) never finished" + (" (including the run)" if roots_open else ""),
            span_ids=tuple(n.span_id for n in open_nodes),
            evidence=tuple(evidence),
            suggestion=(
                "If the process crashed or was killed, the end was never sent: check the logs around the start time. "
                "If it is still running it may be hung: add a timeout. Call aobs.shutdown() (or flush()) before "
                "short-lived processes exit."
            ),
        )
    ]


def orphan_spans(trace: TraceView, config: RuleConfig) -> list[Finding]:
    findings = []
    missing = [trace.spans[s] for s, reason in trace.orphans if reason == "missing_parent"]
    cycles = [trace.spans[s] for s, reason in trace.orphans if reason == "cycle"]
    if missing:
        findings.append(
            Finding(
                rule="trace.missing_parent",
                severity="warning",
                title=f"{len(missing)} span(s) point to a parent that was never received",
                span_ids=tuple(n.span_id for n in missing),
                evidence=tuple(
                    f"span {short(n.span_id)} ({label(n)}) has parent {short(n.parent_span_id or '')}, "
                    "which is not in the trace"
                    for n in missing
                ),
                suggestion=(
                    "The parent's telemetry was lost (process exited before flushing, or a batch was dropped), or the "
                    "context was not carried into a thread: wrap thread work with aobs.with_current_context(fn)."
                ),
            )
        )
    if cycles:
        findings.append(
            Finding(
                rule="trace.parent_cycle",
                severity="warning",
                title=f"{len(cycles)} span(s) form a parent cycle",
                span_ids=tuple(n.span_id for n in cycles),
                evidence=tuple(f"span {short(n.span_id)} ({label(n)}) is part of a parent loop" for n in cycles),
                suggestion="Parent ids loop back on themselves; this is an instrumentation bug in the emitting code.",
            )
        )
    return findings


def missing_usage(trace: TraceView, config: RuleConfig) -> list[Finding]:
    calls = [n for n in trace.spans.values() if n.kind == "llm" and n.lifecycle == "complete"]
    unknown = _ordered(trace, [n for n in calls if "usage" not in fields(n)])
    if not unknown:
        return []
    return [
        Finding(
            rule="data.missing_usage",
            severity="info",
            title=f"token usage unknown for {len(unknown)} of {len(calls)} model call(s)",
            span_ids=tuple(n.span_id for n in unknown),
            evidence=(
                "no usage reported on spans " + ", ".join(short(n.span_id) for n in unknown),
                "token totals for this run are therefore partial (shown as a lower bound, never as zero)",
            ),
            suggestion=(
                "For streaming OpenAI-compatible requests set stream_options={'include_usage': True}; otherwise "
                "the provider did not report usage for these calls."
            ),
        )
    ]


def clock_skew(trace: TraceView, config: RuleConfig) -> list[Finding]:
    skewed = _ordered(trace, [n for n in trace.spans.values() if "clock_skew_suspected" in n.flags])
    if not skewed:
        return []
    return [
        Finding(
            rule="data.clock_skew",
            severity="info",
            title=f"{len(skewed)} span(s) start before or end after their parent",
            span_ids=tuple(n.span_id for n in skewed),
            evidence=tuple(
                f"span {short(n.span_id)} ({label(n)}) lies outside its parent's time range" for n in skewed
            ),
            suggestion="Clocks on different hosts disagree; timings across hosts may be off. Check NTP on the hosts.",
        )
    ]


RULES: tuple[Callable[[TraceView, RuleConfig], list[Finding]], ...] = (
    root_cause_errors,
    repeated_failures,
    repeated_identical_calls,
    slow_spans,
    unfinished_spans,
    orphan_spans,
    missing_usage,
    clock_skew,
)


# Every rule id, in the order findings that tie are listed.
RULE_IDS = (
    "error.root_cause",
    "error.repeated",
    "waste.repeated_call",
    "latency.bottleneck",
    "trace.unfinished",
    "trace.missing_parent",
    "trace.parent_cycle",
    "data.missing_usage",
    "data.clock_skew",
)


def diagnose(trace: TraceView, config: RuleConfig | None = None) -> list[Finding]:
    cfg = config or RuleConfig()
    findings = [f for rule in RULES for f in rule(trace, cfg)]

    def first_start(f: Finding) -> tuple[Any, ...]:
        starts = [trace.spans[s].start_time for s in f.span_ids if s in trace.spans]
        return (min(starts),) if starts else ()

    rule_order = {name: i for i, name in enumerate(RULE_IDS)}
    return sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], first_start(f), rule_order[f.rule], f.span_ids))
