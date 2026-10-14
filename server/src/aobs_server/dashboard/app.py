"""The dashboard web app: a static single-page UI plus a small read-only JSON API.

Routes (all GET):

* ``/`` and ``/static/*``: the UI (plain HTML, CSS and JavaScript; no build step).
* ``/api/health``.
* ``/api/datasets``: the datasets on offer, with their kind (``sample``, ``local`` or ``stored``)
  and presets.
* ``/api/datasets/{id}/overview``, ``/runs``, ``/runs/{trace_id}``, ``/files``.
* ``/api/datasets/{id}/compare``: one baseline run vs one current run (``aobs-server compare``).
* ``/api/datasets/{id}/compare-groups``: groups of runs (``aobs-server compare-groups``).

Sample datasets are public repository data and need no credentials. The local dataset
(``--data-dir``) and the stored dataset (``--project``: one project's traces in PostgreSQL) require
the dashboard token on every request (``Authorization: Bearer <token>``), compared in constant time.
Only the stored dataset reads the database, through ``diagnosis.stored`` (project-scoped, the
project's current privacy policy, content never read).
"""

from __future__ import annotations

import hmac
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..diagnosis.compare import NoRunsError, SelectionError, build_comparisons
from ..diagnosis.loader import Source
from ..diagnosis.repeated import GroupConfig, build_group_report
from ..errors import ApiError, BadRequest, NotFound
from .datasets import (
    REPO_EXAMPLES,
    DatasetSpec,
    DatasetStore,
    LoadedDataset,
    local_dataset,
    outcome,
    overview,
    run_detail,
    run_summary,
    sample_datasets,
    stored_dataset,
)

logger = logging.getLogger("aobs.dashboard")
STATIC = Path(__file__).resolve().parent / "static"
TRACE_ID = re.compile(r"^[0-9a-f]{32}\Z")
TRACE_PREFIX = re.compile(r"^[0-9a-f]{4,32}\Z")
OUTCOMES = ("succeeded", "failed", "cancelled", "unfinished")
MAX_GROUP_FILES = 20
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


class DashboardUnauthorized(ApiError):
    status, slug, title = 401, "unauthorized", "Dashboard token required"

    def __init__(self) -> None:
        super().__init__(
            "This dataset holds local trace files; send the dashboard token printed at start-up",
            headers={"WWW-Authenticate": 'Bearer realm="aobs-dashboard"'},
        )


class DatasetUnavailable(ApiError):
    status, slug, title = 503, "dataset-unavailable", "Dataset unavailable"


@dataclass(frozen=True)
class DashboardConfig:
    samples_dir: Path | None = REPO_EXAMPLES  # None: no sample datasets
    data_dir: Path | None = None  # a directory of the operator's own JSONL files
    token: str | None = None  # required for the local and stored datasets
    project_id: UUID | None = None  # show this project's stored traces (needs engine)
    engine: Any = None  # sqlalchemy Engine of the collector's database

    def __post_init__(self) -> None:
        if (self.data_dir is not None or self.project_id is not None) and not self.token:
            raise ValueError("a token is required when data_dir or project_id is set")
        if (self.project_id is None) != (self.engine is None):
            raise ValueError("project_id and engine go together")


def _problem(error: ApiError) -> JSONResponse:
    body = {
        "type": f"urn:aobs:problem:{error.slug}",
        "title": error.title,
        "status": error.status,
        "detail": error.detail,
        **error.extensions,
    }
    return JSONResponse(body, status_code=error.status, headers=error.headers, media_type="application/problem+json")


def without_paths(value: Any, directory: Path | None) -> Any:
    """Replace the dataset directory in every string with nothing, so responses name files by their
    listed names and never reveal where the server keeps them."""
    if directory is None:  # a stored dataset names its inputs by trace id
        return value
    prefixes = sorted({f"{directory}/", f"{directory.resolve()}/"}, key=len, reverse=True)
    if isinstance(value, str):
        for prefix in prefixes:
            value = value.replace(prefix, "")
        return value
    if isinstance(value, dict):
        return {k: without_paths(v, directory) for k, v in value.items()}
    if isinstance(value, list):
        return [without_paths(v, directory) for v in value]
    return value


def get_dataset(dataset_id: str, request: Request) -> LoadedDataset:
    """Resolve a dataset id; local datasets require the dashboard token (constant-time compare)."""
    store: DatasetStore = request.app.state.store
    cfg: DashboardConfig = request.app.state.config
    spec = store.specs.get(dataset_id)
    if spec is None:
        raise NotFound()
    if spec.kind != "sample":
        header = request.headers.get("authorization", "")
        supplied = header[7:] if header[:7].lower() == "bearer " else ""
        if not cfg.token or not hmac.compare_digest(supplied.encode(), cfg.token.encode()):
            raise DashboardUnauthorized()
    try:
        loaded = store.get(dataset_id)
    except SQLAlchemyError:
        logger.warning("dashboard: the database could not be read", exc_info=True)
        raise DatasetUnavailable("The collector's database could not be read; try again later") from None
    if loaded is None:  # pragma: no cover - spec checked above
        raise NotFound()
    return loaded


Dataset = Annotated[LoadedDataset, Depends(get_dataset)]
FileList = Annotated[list[str], Query(min_length=1, max_length=MAX_GROUP_FILES)]


def create_app(config: DashboardConfig | None = None) -> FastAPI:
    cfg = config or DashboardConfig()
    specs: list[DatasetSpec] = sample_datasets(cfg.samples_dir) if cfg.samples_dir else []
    if cfg.data_dir is not None:
        specs.append(local_dataset(cfg.data_dir))
    if cfg.project_id is not None:
        specs.append(stored_dataset(cfg.project_id))
    store = DatasetStore(specs, cfg.engine)

    app = FastAPI(title="aobs dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.config = cfg

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _problem(exc)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return _problem(NotFound())
        error = ApiError(str(exc.detail))
        error.status, error.slug, error.title = exc.status_code, "http-error", str(exc.detail)
        return _problem(error)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p not in ("query", "path"))
        return _problem(BadRequest(f"invalid parameter {where}: {first.get('msg', 'invalid value')}"))

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover - defensive
        logger.exception("dashboard request failed")
        return _problem(ApiError("The dashboard could not process this request"))

    notes = {
        "sample": "Sample recordings shipped with the repository, not live production traffic.",
        "local": "Local trace files on the machine running the dashboard (not live ingestion).",
        "stored": "Traces stored by the collector (PostgreSQL), read with the project's current privacy policy.",
    }

    def source(ds: LoadedDataset) -> dict[str, Any]:
        return {"kind": ds.spec.kind, "dataset": ds.spec.id, "title": ds.spec.title, "note": notes[ds.spec.kind]}

    def path_of(ds: LoadedDataset, name: str) -> Source:
        path = ds.path(name)
        if path is None:
            raise BadRequest(f"unknown file {name[:80]!r} in this dataset")
        return path

    def prefix(value: str | None, what: str) -> str | None:
        if value is None or value == "":
            return None
        if not TRACE_PREFIX.match(value):
            raise BadRequest(f"{what} must be 4 to 32 lowercase hex characters")
        return value

    # ------------------------------------------------------------------ UI

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ------------------------------------------------------------------ API

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/datasets")
    def datasets() -> dict[str, Any]:
        return {
            "datasets": [
                {
                    "id": s.id,
                    "title": s.title,
                    "kind": s.kind,
                    "description": s.description,
                    "requires_token": s.kind != "sample",
                    "presets": [
                        {
                            "id": p.id,
                            "title": p.title,
                            "mode": p.mode,
                            "baseline": list(p.baseline),
                            "current": list(p.current),
                            "recording": p.expect,
                        }
                        for p in s.presets
                    ],
                }
                for s in store.specs.values()
            ]
        }

    @app.get("/api/datasets/{dataset_id}/files")
    def files(ds: Dataset) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for run in ds.runs:
            counts[run.file] = counts.get(run.file, 0) + 1
        return {
            "source": source(ds),
            "files": [{"name": name, "runs": counts.get(name, 0)} for name in ds.files],
            "skipped": ds.skipped_files,
            "input_problems": without_paths([str(p) for p in ds.problems][:50], ds.spec.directory),
        }

    @app.get("/api/datasets/{dataset_id}/overview")
    def dataset_overview(ds: Dataset) -> dict[str, Any]:
        problems = without_paths([str(p) for p in ds.problems][:50], ds.spec.directory)
        return {"source": source(ds), **overview(ds), "input_problems": problems}

    @app.get("/api/datasets/{dataset_id}/runs")
    def runs(
        ds: Dataset,
        status: Annotated[str | None, Query(max_length=16)] = None,
        task: Annotated[str | None, Query(max_length=256)] = None,
        file: Annotated[str | None, Query(max_length=256)] = None,
    ) -> dict[str, Any]:
        if status and status not in OUTCOMES:
            raise BadRequest(f"status must be one of {', '.join(OUTCOMES)}")
        if file:
            path_of(ds, file)
        selected = [
            r
            for r in ds.runs
            if (not status or outcome(r) == status)
            and (not task or r.report.summary()["run"] == task)
            and (not file or r.file == file)
        ]
        return {"source": source(ds), "total": len(ds.runs), "runs": [run_summary(r) for r in selected]}

    @app.get("/api/datasets/{dataset_id}/runs/{trace_id}")
    def run(ds: Dataset, trace_id: str) -> dict[str, Any]:
        if not TRACE_ID.match(trace_id):
            raise NotFound()
        found = ds.run(trace_id)
        if found is None:
            raise NotFound()
        return {"source": source(ds), **run_detail(found)}

    @app.get("/api/datasets/{dataset_id}/compare")
    def compare(
        ds: Dataset,
        baseline: Annotated[str, Query(max_length=256)],
        current: Annotated[str, Query(max_length=256)],
        baseline_trace: Annotated[str | None, Query(max_length=32)] = None,
        current_trace: Annotated[str | None, Query(max_length=32)] = None,
    ) -> dict[str, Any]:
        try:
            report = build_comparisons(
                path_of(ds, baseline),
                [path_of(ds, current)],
                datetime.now(UTC),
                baseline_trace=prefix(baseline_trace, "baseline_trace"),
                current_trace=prefix(current_trace, "current_trace"),
            )
        except SelectionError as exc:  # includes NoRunsError
            raise BadRequest(without_paths(str(exc), ds.spec.directory)) from None
        except SQLAlchemyError:
            raise DatasetUnavailable("The collector's database could not be read; try again later") from None
        return {"source": source(ds), **without_paths(report.to_dict(), ds.spec.directory)}

    @app.get("/api/datasets/{dataset_id}/compare-groups")
    def compare_groups(
        ds: Dataset,
        baseline: FileList,
        current: FileList,
        task: Annotated[str | None, Query(max_length=256)] = None,
    ) -> dict[str, Any]:
        try:
            report = build_group_report(
                [path_of(ds, f) for f in baseline],
                [path_of(ds, f) for f in current],
                datetime.now(UTC),
                GroupConfig(),
                task=task or None,
            )
        except NoRunsError as exc:
            raise BadRequest(without_paths(str(exc), ds.spec.directory)) from None
        except SQLAlchemyError:
            raise DatasetUnavailable("The collector's database could not be read; try again later") from None
        return {"source": source(ds), **without_paths(report.to_dict(), ds.spec.directory)}

    return app
