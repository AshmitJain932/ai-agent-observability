"""Datasets the dashboard can show: named, server-side collections of saved trace files, or of
traces stored in PostgreSQL.

A file dataset is a directory of JSONL files (what the SDK's ``JsonlFileExporter`` writes). Kinds:

* **sample**: the example recordings of ``examples/diagnose/`` (public repository data). A copy is
  installed with the package (``aobs_server/dashboard/samples``, kept identical by a test), so an
  installed ``aobs-server`` shows them without a clone of the repository;
* **local**: a directory the operator points the dashboard at (``--data-dir``); access needs the
  dashboard token.
* **stored**: the most recently started traces of one project in the collector's database
  (``--project``); access needs the dashboard token. Its "files" are trace ids, and each run is
  read through ``diagnosis.stored`` (project-scoped queries, the project's current privacy policy,
  content never read). The storage layer is only imported when such a dataset is configured.

Files are only ever read from the dataset's own directory, chosen by the server from a directory
listing: clients name a file by its listed name, never by a path. Every file goes through the same
steps as ``aobs-server analyze`` (strict parsing, schema validation, the ingest privacy step with a
metadata-only ceiling, trace assembly, diagnosis rules), so content is never loaded into a report
and error messages are secret-scrubbed.

Only a stored dataset reads the database, and only its own project's traces.
"""

from __future__ import annotations

import statistics
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from ..diagnosis.compare import SUCCESS, RunProfile, money, profile
from ..diagnosis.loader import InputProblem, Source, load
from ..diagnosis.report import TraceReport
from ..diagnosis.rules import RuleConfig, diagnose
from ..model import assemble_events
from ..model.assembly import AssemblyConfig

Kind = Literal["sample", "local", "stored"]
MAX_FILES = 200
MAX_FILE_BYTES = 50 * 1024 * 1024
REPO_EXAMPLES = Path(__file__).resolve().parents[4] / "examples" / "diagnose"  # only exists in a clone


def _packaged_samples() -> Path | None:
    """The sample recordings installed with the package, or None when the package is not installed as
    plain files (e.g. imported from a zip archive), in which case there are no sample datasets."""
    root = resources.files("aobs_server.dashboard") / "samples"
    return root if isinstance(root, Path) and root.is_dir() else None


PACKAGED_SAMPLES = _packaged_samples()


@dataclass(frozen=True)
class Preset:
    """A ready-made comparison of sample files, offered as one click in the dashboard."""

    id: str
    title: str
    mode: Literal["runs", "groups"]
    baseline: tuple[str, ...]
    current: tuple[str, ...]
    expect: str  # what the sample is meant to show (a description of the recording, not a result)


@dataclass(frozen=True)
class DatasetSpec:
    id: str
    title: str
    kind: Kind
    directory: Path | None  # None for a stored dataset
    description: str
    presets: tuple[Preset, ...] = ()
    project_id: UUID | None = None  # a stored dataset's project


@dataclass
class Run:
    file: str
    report: TraceReport
    profile: RunProfile

    @property
    def trace_id(self) -> str:
        return self.report.trace.trace_id


@dataclass
class LoadedDataset:
    spec: DatasetSpec
    files: list[str]
    runs: list[Run]
    problems: list[InputProblem] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)
    inputs: dict[str, Source] = field(default_factory=dict)  # a stored dataset's trace id -> its input

    def run(self, trace_id: str) -> Run | None:
        return next((r for r in self.runs if r.trace_id == trace_id), None)

    def path(self, name: str) -> Source | None:
        """The input behind ``name`` if it is one of this dataset's listed files, else None."""
        if name not in self.files:
            return None
        if self.spec.directory is None:
            return self.inputs.get(name)
        return self.spec.directory / name


def sample_datasets(base: Path | None = None) -> list[DatasetSpec]:
    """The sample datasets found under ``base`` (default: the copy installed with the package); a
    directory laid out like ``examples/diagnose`` works too."""
    root = base or PACKAGED_SAMPLES
    if root is None:
        return []
    specs = [
        DatasetSpec(
            "sample-repeated",
            "Sample: repeated runs (inventory agent)",
            "sample",
            root / "groups",
            "Groups of 3 to 10 recordings of one task: healthy, slow, flaky, an outlier, too few runs and "
            "incomplete usage. Recorded with the SDK by examples/diagnose/make_group_samples.py.",
            (
                Preset("healthy-vs-slow", "Healthy vs slow (10 + 10 runs)", "groups",
                       ("baseline.jsonl",), ("slow.jsonl",), "the inventory search was made ~95 ms slower"),
                Preset("healthy-vs-flaky", "Healthy vs flaky (10 + 10 runs)", "groups",
                       ("baseline.jsonl",), ("flaky.jsonl",), "the inventory database refuses connections"),
                Preset("healthy-vs-rerun", "Healthy vs healthy rerun", "groups",
                       ("baseline.jsonl",), ("healthy_rerun.jsonl",), "the same healthy task recorded twice"),
                Preset("healthy-vs-outlier", "Healthy vs one cold start", "groups",
                       ("baseline.jsonl",), ("outlier.jsonl",), "one of ten runs hit a 1.5 s cold start"),
                Preset("healthy-vs-few", "Healthy vs only 3 runs", "groups",
                       ("baseline.jsonl",), ("few_runs.jsonl",), "too few current runs for a conclusion"),
            ),
        ),
        DatasetSpec(
            "sample-pairs",
            "Sample: before/after runs",
            "sample",
            root / "compare",
            "Single runs before and after a change: a refund that starts timing out, and an inventory agent "
            "release. Recorded by examples/diagnose/make_compare_samples.py.",
            (
                Preset("refund-ok-vs-outage", "Refund works vs refund outage", "runs",
                       ("refund_ok.jsonl",), ("refund_outage.jsonl",), "the payments API starts timing out"),
                Preset("inventory-v1-vs-v2", "Inventory agent v1 vs v2", "runs",
                       ("inventory_v1.jsonl",), ("inventory_v2.jsonl",),
                       "a release with a bigger model, a flaky call and a new slow step"),
                Preset("refund-ok-vs-rerun", "Refund works vs the same task again", "runs",
                       ("refund_ok.jsonl",), ("refund_ok_rerun.jsonl",), "the same healthy task recorded twice"),
            ),
        ),
        DatasetSpec(
            "sample-failures",
            "Sample: single-run failures",
            "sample",
            root / "traces",
            "One run per file: a refund outage, a retry storm, wasted repeated calls with a slow search, a "
            "crashed run and a healthy run. Recorded by examples/diagnose/make_samples.py.",
        ),
    ]  # fmt: skip
    return [s for s in specs if s.directory is not None and s.directory.is_dir()]


def local_dataset(directory: Path) -> DatasetSpec:
    # Title and description are shown without the token: they must not reveal the directory's path.
    return DatasetSpec(
        "local",
        "Local trace files",
        "local",
        directory,
        "JSONL trace files from the directory given with --data-dir, on the machine running the dashboard.",
    )


def stored_dataset(project_id: UUID) -> DatasetSpec:
    # Title and description are shown without the token: they do not name the project or database.
    return DatasetSpec(
        "stored",
        "Stored traces",
        "stored",
        None,
        f"The {MAX_FILES} most recently started traces the collector stored for the project given with --project.",
        project_id=project_id,
    )


def _list_files(directory: Path) -> tuple[list[str], list[str]]:
    files, skipped = [], []
    for path in sorted(directory.glob("*.jsonl"))[: MAX_FILES + 1]:
        if not path.is_file() or path.is_symlink():
            skipped.append(f"{path.name}: not a regular file")
        elif path.stat().st_size > MAX_FILE_BYTES:
            skipped.append(f"{path.name}: larger than {MAX_FILE_BYTES // (1024 * 1024)} MiB")
        else:
            files.append(path.name)
    if len(files) > MAX_FILES:
        skipped.append(f"only the first {MAX_FILES} files are shown")
        files = files[:MAX_FILES]
    return files, skipped


def _signature(directory: Path, files: list[str]) -> tuple[tuple[str, int, int], ...]:
    out = []
    for name in files:
        stat = (directory / name).stat()
        out.append((name, stat.st_mtime_ns, stat.st_size))
    return tuple(out)


class DatasetStore:
    """Loads datasets on demand and caches them until a file (or a stored trace) changes."""

    def __init__(self, specs: list[DatasetSpec], engine: Any = None) -> None:
        self.specs = {s.id: s for s in specs}
        self.engine = engine  # sqlalchemy Engine for stored datasets
        self._cache: dict[str, tuple[Any, LoadedDataset]] = {}
        self._lock = threading.Lock()

    def get(self, dataset_id: str) -> LoadedDataset | None:
        spec = self.specs.get(dataset_id)
        if spec is None:
            return None
        if spec.directory is None:
            return self._get_stored(spec)
        if not spec.directory.is_dir():
            return LoadedDataset(spec, [], [], skipped_files=[f"directory {spec.directory} does not exist"])
        files, skipped = _list_files(spec.directory)
        signature = _signature(spec.directory, files)
        with self._lock:
            cached = self._cache.get(dataset_id)
            if cached and cached[0] == signature:
                return cached[1]
        loaded = _load(spec, files, skipped)
        with self._lock:
            self._cache[dataset_id] = (signature, loaded)
        return loaded

    def _get_stored(self, spec: DatasetSpec) -> LoadedDataset:
        """Raises sqlalchemy errors when the database cannot be read (the app reports them)."""
        from ..diagnosis.stored import Selection, StoredTraces, select_rows

        assert spec.project_id is not None and self.engine is not None
        with self.engine.connect() as conn:
            rows = select_rows(conn, spec.project_id, Selection(limit=MAX_FILES))
        signature = tuple((row.trace_id, row.last_received_at) for row in rows)
        with self._lock:
            cached = self._cache.get(spec.id)
            if cached and cached[0] == signature:
                return cached[1]
        trace_ids = [row.trace_id for row in rows]
        everything = StoredTraces(
            self.engine, spec.project_id, Selection(tuple(trace_ids), limit=max(len(trace_ids), 1)), "stored traces"
        )
        inputs: dict[str, Source] = {
            t: StoredTraces(self.engine, spec.project_id, Selection((t,), limit=1), t) for t in trace_ids
        }
        loaded = _runs_of(spec, trace_ids, [], [(everything, None)])
        loaded.inputs = inputs
        with self._lock:
            self._cache[spec.id] = (signature, loaded)
        return loaded


def _runs_of(
    spec: DatasetSpec, files: list[str], skipped: list[str], inputs: list[tuple[Source, str | None]]
) -> LoadedDataset:
    """Load each input and diagnose its runs; a run's "file" is its input's name, or its trace id."""
    now = datetime.now(UTC)
    dataset = LoadedDataset(spec, files, [], skipped_files=skipped)
    for source, name in inputs:
        telemetry = load([source])
        dataset.problems.extend(telemetry.problems)
        traces = assemble_events(telemetry.events, now, AssemblyConfig())
        for trace in traces.values():
            resource = telemetry.resources.get(trace.trace_id, {})
            report = TraceReport(trace, resource, diagnose(trace, RuleConfig()))
            file = name or trace.trace_id
            dataset.runs.append(Run(file, report, profile(trace, resource, file)))
    dataset.runs.sort(key=lambda r: (r.report.trace.started_at or now, r.trace_id), reverse=True)
    return dataset


def _load(spec: DatasetSpec, files: list[str], skipped: list[str]) -> LoadedDataset:
    assert spec.directory is not None
    return _runs_of(spec, files, skipped, [(spec.directory / name, name) for name in files])


# --------------------------------------------------------------------------- summaries


def outcome(run: Run) -> str:
    """succeeded | failed | unfinished | cancelled (for counting; the exact status is shown too)."""
    status = run.report.trace.status
    if status in SUCCESS:
        return "succeeded"
    if status == "error":
        return "failed"
    if status == "cancelled":
        return "cancelled"
    return "unfinished"


def run_summary(run: Run) -> dict[str, Any]:
    trace = run.report.trace
    summary = run.report.summary()
    counts = Counter(f.severity for f in run.report.findings)
    tokens_in = run.profile.tokens("input_tokens")
    cost = run.profile.cost()
    return {
        "trace_id": run.trace_id,
        "file": run.file,
        "run": summary["run"],
        "agent": summary["agent"],
        "status": trace.status,
        "outcome": outcome(run),
        "started_at": summary["started_at"],
        "duration_ms": run.profile.duration_ms,
        "span_count": trace.span_count,
        "error_count": trace.error_count,
        "findings": {s: counts.get(s, 0) for s in ("error", "warning", "info")},
        "input_tokens": tokens_in[0] if tokens_in[1] else None,
        "cost": None if cost.amount is None else {"amount": money(cost.amount), "currency": cost.currency},
    }


def overview(dataset: LoadedDataset) -> dict[str, Any]:
    runs = dataset.runs
    outcomes = Counter(outcome(r) for r in runs)
    finished = outcomes["succeeded"] + outcomes["failed"] + outcomes["cancelled"]
    durations = [r.profile.duration_ms for r in runs if r.profile.duration_ms is not None]
    token_totals: dict[str, Any] = {}
    for name in ("input_tokens", "output_tokens"):
        complete = [v for r in runs if (t := r.profile.tokens(name))[1] and (v := t[0]) is not None]
        with_calls = sum(1 for r in runs if r.report.trace.tokens[name].expected_spans)
        token_totals[name] = {
            "total": sum(complete) if complete else None,
            "runs_counted": len(complete),
            "runs_with_model_calls": with_calls,
        }
    costs = [c for r in runs if (c := r.profile.cost()).amount is not None]
    currencies = {c.currency for c in costs}
    cost: dict[str, Any]
    if not costs:
        cost = {"total": None, "currency": None, "runs_counted": 0, "note": "no run reports cost on every model call"}
    elif len(currencies) > 1:
        cost = {"total": None, "currency": None, "runs_counted": 0, "note": "several currencies; not summed"}
    else:
        total = sum((c.amount for c in costs if c.amount is not None), Decimal(0))
        cost = {"total": money(total), "currency": currencies.pop(), "runs_counted": len(costs), "note": None}
    return {
        "runs": len(runs),
        "succeeded": outcomes["succeeded"],
        "failed": outcomes["failed"],
        "cancelled": outcomes["cancelled"],
        "unfinished": outcomes["unfinished"],
        "finished": finished,
        "failure_rate": outcomes["failed"] / finished if finished else None,
        "median_duration_ms": statistics.median(durations) if durations else None,
        "durations_counted": len(durations),
        "tokens": token_totals,
        "cost": cost,
        "recent": [run_summary(r) for r in runs[:8]],
        "tasks": sorted({r.report.summary()["run"] or "(no root span)" for r in runs}),
    }


def run_detail(run: Run) -> dict[str, Any]:
    data = run.report.to_dict()
    trace = run.report.trace
    start = trace.started_at
    for row in data["spans"]:
        node = trace.spans[row["span_id"]]
        row["offset_ms"] = None if start is None else (node.start_time - start).total_seconds() * 1000
        row["lifecycle"] = node.lifecycle
    cost = run.profile.cost()
    data["file"] = run.file
    data["outcome"] = outcome(run)
    data["cost"] = None if cost.amount is None else {"amount": money(cost.amount), "currency": cost.currency}
    data["cost_note"] = cost.reason
    return data
