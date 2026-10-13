"""Offline trace diagnosis: load saved telemetry, assemble traces, apply deterministic rules, and
report findings with evidence and suggestions. No database, server or LLM is needed.

    report = build_report([Path("traces.jsonl")], now=datetime.now(UTC))
    print(render_text(report))

``compare.py`` compares a baseline run with later runs (``build_comparisons``); ``repeated.py``
compares groups of repeated runs with small-sample statistics (``build_group_report``).

The rules (``rules.py``) take an assembled ``TraceView``, so they can later run on traces read
from storage as well as on files.
"""

from .compare import (
    CompareConfig,
    ComparisonReport,
    NoRunsError,
    SelectionError,
    build_comparisons,
    render_comparison_text,
)
from .loader import InputProblem, Telemetry, load
from .repeated import GroupConfig, GroupReport, analyze_groups, build_group_report, render_group_text
from .report import Report, TraceReport, build_report, render_text
from .rules import RULES, Finding, RuleConfig, diagnose

__all__ = [
    "RULES",
    "CompareConfig",
    "ComparisonReport",
    "Finding",
    "GroupConfig",
    "GroupReport",
    "InputProblem",
    "NoRunsError",
    "Report",
    "RuleConfig",
    "SelectionError",
    "Telemetry",
    "TraceReport",
    "analyze_groups",
    "build_comparisons",
    "build_group_report",
    "build_report",
    "diagnose",
    "load",
    "render_comparison_text",
    "render_group_text",
    "render_text",
]
