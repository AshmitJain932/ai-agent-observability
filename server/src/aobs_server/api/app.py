"""The collector HTTP API.

Routes:

* ``POST /v1/ingest``: one schema-v1 batch (docs/ingestion.md). Authenticated with a project API
  key that has the ``ingest`` scope; the project is always the key's project.
* ``GET /v1/projects/{project_id}/traces/{trace_id}`` and ``…/events``: read one stored trace
  (docs/traces-api.md). Needs a key with the ``read`` scope for that project; any other project,
  and any trace that is not stored in it, is the same 404.
* ``GET /healthz``: the process is up (no database access).
* ``GET /readyz``: the database answers.

Request handling for ``/v1/ingest``, cheapest checks first: media type and declared length, then
the API key (one indexed lookup), then the body is read with a hard cap, parsed strictly, the
event count checked, and only then validated, privacy-filtered and stored in one transaction.

Errors are RFC 9457 problem+json. Responses never include stack traces, SQL, database or file
system details; database errors are logged by type and SQLSTATE only, because driver messages
can contain statement parameters (trace data).
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, Any

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..auth import Principal, authorize, require_scope
from ..auth.service import authenticate
from ..clock import Clock, SystemClock
from ..config import Settings
from ..errors import ApiError, BadRequest, NotFound, PayloadTooLarge, ServiceUnavailable, UnsupportedMediaType
from ..ingest.pipeline import ingest, parse_body, validate
from ..query.traces import DEFAULT_PAGE, MAX_PAGE, get_span_events, get_trace
from ..storage.traces import ConcurrentConflict

logger = logging.getLogger("aobs.api")

LOCK_TIMEOUT = "10s"  # waiting longer for a trace lock than this answers 503 (the SDK retries)
STATEMENT_TIMEOUT = "30s"
TRANSIENT_SQLSTATE_CLASSES = ("08", "40", "53", "57")  # connection, rollback (deadlock), resources, operator
TRANSIENT_SQLSTATES = ("55P03",)  # lock_not_available (lock_timeout)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


class StorageUnavailable(ServiceUnavailable):
    def __init__(self, retry_after_s: int = 5) -> None:
        super().__init__(
            "Storage is temporarily unavailable; retrying the request is safe",
            headers={"Retry-After": str(retry_after_s)},
        )


def _problem(error: ApiError) -> JSONResponse:
    body = {
        "type": f"urn:aobs:problem:{error.slug}",
        "title": error.title,
        "status": error.status,
        "detail": error.detail,
        **error.extensions,
    }
    return JSONResponse(body, status_code=error.status, headers=error.headers, media_type="application/problem+json")


def _sqlstate(exc: BaseException) -> str:
    orig = getattr(exc, "orig", None)
    return str(getattr(orig, "sqlstate", None) or "")


def storage_error(exc: SQLAlchemyError, what: str, failed: str = "The batch could not be stored") -> ApiError:
    """Map a database error to a response without exposing it; log only its type and SQLSTATE."""
    state = _sqlstate(exc)
    transient = (
        not isinstance(exc, DBAPIError)
        or exc.connection_invalidated
        or state.startswith(TRANSIENT_SQLSTATE_CLASSES)
        or state in TRANSIENT_SQLSTATES
    )
    logger.warning("%s failed: %s sqlstate=%s", what, type(exc).__name__, state or "-")
    return StorageUnavailable() if transient else ApiError(failed)


def _check_media(request: Request, max_body_bytes: int) -> None:
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise UnsupportedMediaType("Send the batch as Content-Type: application/json")
    encoding = request.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in ("", "identity"):
        raise UnsupportedMediaType("Content-Encoding is not supported; send the batch uncompressed")
    declared = request.headers.get("content-length")
    if declared is not None:
        if not (declared.strip().isascii() and declared.strip().isdigit()):
            raise BadRequest("invalid Content-Length header")
        if int(declared) > max_body_bytes:
            raise _too_large(max_body_bytes)


def _too_large(max_body_bytes: int) -> PayloadTooLarge:
    return PayloadTooLarge(
        f"The request body is larger than {max_body_bytes} bytes; send smaller batches",
        extensions={"limit": "body_bytes", "max": max_body_bytes},
    )


async def _read_body(request: Request, max_body_bytes: int) -> bytes:
    """The body, read in chunks and abandoned as soon as it exceeds the limit (also without a
    Content-Length header, e.g. chunked uploads)."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_body_bytes:
            raise _too_large(max_body_bytes)
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(settings: Settings, engine: Engine, clock: Clock | None = None) -> FastAPI:
    """The collector app. ``engine`` must point at a database migrated to the latest revision."""
    now = clock or SystemClock()
    app = FastAPI(title="aobs collector", docs_url=None, redoc_url=None, openapi_url=None)

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
        if exc.headers:
            error.headers.update(exc.headers)
        return _problem(error)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p not in ("query", "path"))
        return _problem(BadRequest(f"invalid parameter {where}: {first.get('msg', 'invalid value')}"[:300]))

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unexpected error on %s %s: %s", request.method, request.url.path, type(exc).__name__)
        return _problem(ApiError())

    def _authenticate(header: str | None) -> Principal:
        try:
            with engine.connect() as conn:
                return authenticate(conn, header)
        except SQLAlchemyError as exc:
            raise storage_error(exc, "authentication") from None

    def _ingest(principal: Principal, data: Any) -> dict[str, Any]:
        received_at = now.now()
        validated = validate(data, principal)  # CPU only: no database connection held meanwhile
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
                conn.exec_driver_sql(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
                return ingest(conn, principal, validated, received_at, settings)
        except ConcurrentConflict:
            logger.info(
                "ingest project=%s: concurrent span event conflict, asked client to retry", principal.project_id
            )
            raise StorageUnavailable(retry_after_s=1) from None
        except SQLAlchemyError as exc:
            raise storage_error(exc, "ingest") from None

    def _read(header: str | None, project_id: str, query: Any) -> dict[str, Any]:
        """Authenticate, authorize the project for reading, then run ``query(conn, project)`` in one
        read-only REPEATABLE READ transaction (summary and spans come from the same snapshot)."""
        principal = _authenticate(header)
        project = authorize(principal, project_id, "read")  # another project: 404; no read scope: 403
        try:
            with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn, conn.begin():
                conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                conn.exec_driver_sql(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
                result: dict[str, Any] = query(conn, project)
        except SQLAlchemyError as exc:
            raise storage_error(exc, "trace read", "The trace could not be read") from None
        logger.info("read project=%s key=%s %s", project, principal.key_id, query.__name__)
        return result

    @app.get("/v1/projects/{project_id}/traces/{trace_id}")
    def read_trace(
        request: Request,
        project_id: str,
        trace_id: str,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
        cursor: Annotated[str | None, Query(max_length=64)] = None,
        include_content: bool = False,
    ) -> dict[str, Any]:
        def trace(conn: Any, project: Any) -> dict[str, Any]:
            return get_trace(conn, project, trace_id, now=now.now(), limit=limit, cursor=cursor,
                             include_content=include_content)  # fmt: skip

        return _read(request.headers.get("authorization"), project_id, trace)

    @app.get("/v1/projects/{project_id}/traces/{trace_id}/events")
    def read_span_events(
        request: Request,
        project_id: str,
        trace_id: str,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
        cursor: Annotated[str | None, Query(max_length=256)] = None,
        span_id: Annotated[str | None, Query(max_length=64)] = None,
    ) -> dict[str, Any]:
        def span_events(conn: Any, project: Any) -> dict[str, Any]:
            return get_span_events(conn, project, trace_id, span_id=span_id, limit=limit, cursor=cursor)

        return _read(request.headers.get("authorization"), project_id, span_events)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, str]:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            raise storage_error(exc, "readiness check") from None
        return {"status": "ready"}

    @app.post("/v1/ingest")
    async def ingest_batch(request: Request) -> JSONResponse:
        started = time.monotonic()
        _check_media(request, settings.max_body_bytes)
        principal = await run_in_threadpool(_authenticate, request.headers.get("authorization"))
        require_scope(principal, "ingest")
        body = await _read_body(request, settings.max_body_bytes)
        data = parse_body(body, settings.max_events_per_batch)
        result = await run_in_threadpool(_ingest, principal, data)
        logger.debug("ingest took %.1f ms", (time.monotonic() - started) * 1000)
        return JSONResponse(result)

    return app
