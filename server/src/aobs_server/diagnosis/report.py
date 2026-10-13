"""Build and render the trace report (plain ASCII text, or JSON)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..model import assemble_events
from ..model.assembly import AssemblyConfig, TraceView
from .loader import InputProblem, Telemetry, load
from .rules import Finding, RuleConfig, diagnose, fields, label, ms, short

PRIVACY_NOTE = (
    "content (prompts, responses, tool arguments and results) is never shown; error messages are "
    "secret-scrubbed; input fingerprints are keyed hashes"
)


@dataclass(frozen=True)
class TraceReport:
    trace: TraceView
    resource: dict[str, Any]
    findings: list[Finding]

    def tools(self) -> list[dict[str, Any]]:
        stats: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"calls": 0, "failed": 0, "total_ms": 0.0, "max_ms": 0.0}
        )
        for node in self.trace.spans.values():
            if node.kind != "tool":
                continue
            row = stats[fields(node).get("tool", {}).get("name") or node.name]
            row["calls"] += 1
            row["failed"] += node.status == "error"
            row["total_ms"] += node.duration_ms or 0.0
            row["max_ms"] = max(row["max_ms"], node.duration_ms or 0.0)
        return [{"tool": name, **row} for name, row in sorted(stats.items())]

    def summary(self) -> dict[str, Any]:
        t = self.trace
        roots = [t.spans[r] for r in t.roots]
        tokens = {
            name: {"value": total.value, "reported_spans": total.reported_spans, "expected_spans": total.expected_spans}
            for name, total in t.tokens.items()
            if name in ("input_tokens", "output_tokens") and total.expected_spans
        }
        return {
            "trace_id": t.trace_id,
            "run": roots[0].name if roots else None,
            "agent": fields(roots[0]).get("agent", {}).get("name") if roots else None,
            "status": t.status,
            "started_at": None if t.started_at is None else t.started_at.isoformat(),
            "duration_ms": roots[0].duration_ms if len(roots) == 1 else None,
            "span_count": t.span_count,
            "error_count": t.error_count,
            "open_span_count": t.open_span_count,
            "kinds": dict(sorted(t.kind_counts.items())),
            "tokens": tokens,
            "resource": self.resource,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "spans": [_span_row(self.trace, sid, depth) for sid, depth in _walk(self.trace)],
            "tools": self.tools(),
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass(frozen=True)
class Report:
    sources: list[str]
    batches: int
    problems: tuple[InputProblem, ...]
    traces: list[TraceReport]
    redactions: dict[str, int]

    def worst_severity(self) -> str | None:
        severities = {f.severity for t in self.traces for f in t.findings}
        return next((s for s in ("error", "warning", "info") if s in severities), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sources": self.sources,
            "batches": self.batches,
            "privacy": {"note": PRIVACY_NOTE, "removed_or_scrubbed": self.redactions},
            "input_problems": [str(p) for p in self.problems],
            "traces": [t.to_dict() for t in self.traces],
        }


def _walk(trace: TraceView) -> list[tuple[str, int]]:
    """Depth-first order: roots, then orphans (their own subtrees), children by start time."""
    out: list[tuple[str, int]] = []
    seen: set[str] = set()

    def visit(span_id: str, depth: int) -> None:
        if span_id in seen or span_id not in trace.spans:
            return
        seen.add(span_id)
        out.append((span_id, depth))
        for child in trace.spans[span_id].children:
            visit(child, depth + 1)

    for root in trace.roots:
        visit(root, 0)
    for orphan, _ in trace.orphans:
        visit(orphan, 0)
    return out


def _span_row(trace: TraceView, span_id: str, depth: int) -> dict[str, Any]:
    node = trace.spans[span_id]
    f = fields(node)
    row: dict[str, Any] = {
        "span_id": span_id,
        "depth": depth,
        "kind": node.kind,
        "label": label(node),
        "status": node.status or node.lifecycle,
        "duration_ms": node.duration_ms,
    }
    if node.orphan_reason:
        row["orphan_reason"] = node.orphan_reason
    if "error" in f:
        row["error_type"] = f["error"].get("type")
    if "usage" in f:
        row["usage"] = {k: v for k, v in f["usage"].items() if k in ("input_tokens", "output_tokens")}
    if "retry" in f:
        row["attempt"] = f["retry"].get("attempt")
    return row


def build_report(
    paths: list[Path],
    now: datetime,
    rules: RuleConfig | None = None,
    error_messages: bool = True,
    trace_id: str | None = None,
) -> Report:
    telemetry: Telemetry = load(paths, error_messages=error_messages)
    traces = assemble_events(telemetry.events, now, AssemblyConfig())
    selected = [t for tid, t in traces.items() if trace_id is None or tid.startswith(trace_id)]
    selected.sort(key=lambda t: (t.started_at is None, t.started_at, t.trace_id))
    reports = [TraceReport(t, telemetry.resources.get(t.trace_id, {}), diagnose(t, rules)) for t in selected]
    privacy_counters = {
        k: v
        for k, v in sorted(telemetry.counters.items())
        if v
        and (k.endswith("_policy") or k in ("secrets_scrubbed", "content_dropped_oversize", "nul_characters_replaced"))
    }
    return Report([str(p) for p in paths], telemetry.batches, telemetry.problems, reports, privacy_counters)


# --------------------------------------------------------------------------- text rendering


def _tokens_line(summary: dict[str, Any]) -> str | None:
    parts = []
    for name, short_name in (("input_tokens", "in"), ("output_tokens", "out")):
        t = summary["tokens"].get(name)
        if not t:
            continue
        if t["value"] is None:
            parts.append(f"{short_name}: unknown")
        elif t["reported_spans"] < t["expected_spans"]:
            parts.append(
                f"{short_name}: >= {t['value']} ({t['reported_spans']} of {t['expected_spans']} calls reported)"
            )
        else:
            parts.append(f"{short_name}: {t['value']}")
    return ", ".join(parts) if parts else None


def render_text(report: Report) -> str:
    lines: list[str] = []
    add = lines.append
    sources = ", ".join(report.sources)
    add(f"aobs trace report: {len(report.traces)} trace(s) from {report.batches} batch(es) in {sources}")
    add(f"privacy: {PRIVACY_NOTE}")
    if report.redactions:
        add(
            "         removed or scrubbed while loading: " + ", ".join(f"{k}={v}" for k, v in report.redactions.items())
        )
    if report.problems:
        add("")
        add(f"Input problems ({len(report.problems)}):")
        for problem in report.problems[:20]:
            add(f"  - {problem}")
        if len(report.problems) > 20:
            add(f"  ... and {len(report.problems) - 20} more")
    if not report.traces:
        add("")
        add("No traces found.")
    for tr in report.traces:
        s = tr.summary()
        res = s["resource"]
        add("")
        add("=" * 100)
        add(f"Trace {s['trace_id']}  run '{s['run']}'  status={s['status'].upper()}")
        add(
            f"  {s['span_count']} spans ({', '.join(f'{v} {k}' for k, v in s['kinds'].items())}), "
            f"{s['error_count']} failed, {s['open_span_count']} unfinished, duration {ms(s['duration_ms'])}"
        )
        if res:
            add(
                f"  service {res.get('service_name')} ({res.get('environment')}), {res.get('sdk')}, "
                f"source capture mode {res.get('capture_mode')}"
            )
        tokens = _tokens_line(s)
        if tokens:
            add(f"  tokens {tokens}")

        add("")
        add("Execution")
        for row in tr.to_dict()["spans"]:
            extra = []
            if row.get("error_type"):
                extra.append(row["error_type"])
            if row.get("usage"):
                extra.append(f"tokens {row['usage'].get('input_tokens', '?')}/{row['usage'].get('output_tokens', '?')}")
            if row.get("attempt"):
                extra.append(f"attempt {row['attempt']}")
            if row.get("orphan_reason"):
                extra.append(f"ORPHAN ({row['orphan_reason']})")
            status = str(row["status"]).upper() if row["status"] != "ok" else "ok"
            name = f"{'  ' * row['depth']}{'+- ' if row['depth'] else ''}{row['label']}"
            add(
                f"  {name:<52} {ms(row['duration_ms']):>12}  {status:<11} {'  '.join(extra)}  [{short(row['span_id'])}]"
            )

        tools = tr.tools()
        add("")
        if tools:
            add("Tool calls")
            add(f"  {'tool':<28} {'calls':>5} {'failed':>6} {'total':>12} {'slowest':>12}")
            for t in tools:
                add(f"  {t['tool']:<28} {t['calls']:>5} {t['failed']:>6} {ms(t['total_ms']):>12} {ms(t['max_ms']):>12}")
        else:
            add("Tool calls: none recorded")

        add("")
        counts = {sev: sum(1 for f in tr.findings if f.severity == sev) for sev in ("error", "warning", "info")}
        add(f"Findings: {counts['error']} error(s), {counts['warning']} warning(s), {counts['info']} info")
        if not tr.findings:
            add("  No issues detected by the rules.")
        for number, finding in enumerate(tr.findings, start=1):
            add("")
            add(f"  {number}. [{finding.severity.upper()}] {finding.title}   ({finding.rule})")
            add("     Evidence:")
            for item in finding.evidence:
                add(f"       - {item}")
            add(f"     Suggestion: {finding.suggestion}")
    add("")
    return "\n".join(lines)
